"""First-run commissioning: the only audited path from ``draft`` to ``measured``.

A draft profile refuses every write step, but its measured values only exist
*after* one real run on the bench: the tool's WRITE SIZE, the device MD5, the
boot banner the firmware really prints. Used alone, the two rules form a
circle - nothing may be written until the bench is measured, and the bench
cannot be measured without one write.

This module closes that circle without weakening the default guard:

* the profile must carry a ``commissioning`` block that a human wrote
  (``approved_by`` + ``approval_ref`` + the exact steps allowed + how many
  writes), never something the driver infers;
* the run must pass ``--commission <approval_ref>`` matching that block, plus
  the usual ``--confirm-write``;
* the block expires (``expires_at``, Beijing time) and only covers the listed
  steps, with the write count capped;
* the run writes ``commissioning.receipt.json`` with the observed values;
* ``promote_profile`` rebuilds the profile as ``measured`` from that receipt
  and refuses if any graded value is missing - a status flip with no evidence
  cannot pass through here.

Everything else (measured profiles, profiles with no block) keeps the original
behaviour: draft writes are refused, exit code 3.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .contract import now_bj, sha256_file
from .profile import MD5, Profile, ProfileError


class CommissionError(ProfileError):
    """A write was refused by the commissioning guard (CLI exit code 3)."""
from .state import write_steps

COMMISSION_SCHEMA = "burner-commissioning/v1"
REQUIRED_KEYS = ("approved_by", "approval_ref", "allowed_steps", "max_write_steps")
OPTIONAL_KEYS = ("expires_at", "note")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
TIMING_KEYS = ("cut_to_strap_s", "strap_to_restore_s", "restore_to_strap_off_s", "cycle_hold_s")


# ------------------------------------------------------------------ block


def validate_block(raw: dict, status: str) -> dict | None:
    """Validate ``profile.commissioning``; a measured profile may not carry one."""
    block = raw.get("commissioning")
    if block is None:
        return None
    if status == "measured":
        raise ProfileError(
            "profile.commissioning is only allowed on a draft profile; "
            "promote the profile instead of re-opening commissioning")
    if not isinstance(block, dict):
        raise ProfileError("profile.commissioning must be an object")
    unknown = set(block) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
    if unknown:
        raise ProfileError(f"profile.commissioning has unknown keys: {sorted(unknown)}")
    for key in ("approved_by", "approval_ref"):
        if not str(block.get(key) or "").strip():
            raise ProfileError(f"profile.commissioning.{key} must name the approver / approval reference")
    steps = block.get("allowed_steps")
    if not (isinstance(steps, list) and steps and all(isinstance(item, str) and item for item in steps)):
        raise ProfileError("profile.commissioning.allowed_steps must be a non-empty list of step ids")
    if len(set(steps)) != len(steps):
        raise ProfileError("profile.commissioning.allowed_steps must not repeat a step")
    count = block.get("max_write_steps")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 2:
        raise ProfileError("profile.commissioning.max_write_steps must be 1 or 2")
    expires = block.get("expires_at")
    if expires is not None:
        if not (isinstance(expires, str) and TIMESTAMP.match(expires)):
            raise ProfileError("profile.commissioning.expires_at must be 'YYYY-MM-DD HH:MM:SS' (北京时间)")
        if now_bj() > expires:
            raise ProfileError(
                f"profile.commissioning.expires_at {expires} has passed; a new approval is required")
    return block


# ------------------------------------------------------------------ guard


def guard_write(profile: Profile, steps: list[str], plan: list[str], approval_ref: str | None,
                confirm_write: bool) -> list[str]:
    """Refuse write steps unless the normal guard passes or commissioning does.

    Returns the write steps this run is about to execute (empty for read-only
    runs). Raises :class:`ProfileError` for every refusal, which the CLI maps
    to exit code 3.
    """
    writes = [step for step in steps if step in write_steps(plan)]
    if not writes:
        return []

    block = getattr(profile, "commissioning", None)

    if profile.measured:
        if approval_ref:
            raise CommissionError(
                f"profile {profile.name} is already measured; drop --commission and use --confirm-write")
        if not confirm_write:
            raise CommissionError(f"steps {writes} change power/flash state; re-run with --confirm-write")
        return writes

    # draft: the only way through is an explicit, approved, bounded commissioning
    if not approval_ref:
        missing = profile.raw.get("measurement_required") or []
        raise CommissionError(
            f"profile {profile.name} is draft; write steps {writes} refused until these are measured: {missing}. "
            "A first bring-up run needs an approved profile.commissioning block and --commission <approval_ref>")
    if not block:
        raise CommissionError(
            f"profile {profile.name} is draft and has no commissioning block; "
            "add approved_by / approval_ref / allowed_steps / max_write_steps before a first run")
    if approval_ref.strip() != str(block["approval_ref"]).strip():
        raise CommissionError(
            f"--commission {approval_ref!r} does not match the profile's approval_ref "
            f"{block['approval_ref']!r}")
    unknown_steps = [step for step in steps if step not in plan]
    if unknown_steps:
        raise CommissionError(f"steps {unknown_steps} are not part of this profile's plan {plan}")
    outside = [step for step in steps if step not in ("preflight", "receipt") and step not in block["allowed_steps"]]
    if outside:
        raise CommissionError(f"commissioning block does not allow steps {outside}; allowed {block['allowed_steps']}")
    if len(writes) > int(block["max_write_steps"]):
        raise CommissionError(
            f"commissioning allows at most {block['max_write_steps']} write step(s), this run would run {writes}")
    if not confirm_write:
        raise CommissionError(f"steps {writes} change power/flash state; re-run with --confirm-write")
    return writes


# ------------------------------------------------------------------ receipt


def _read_steps(run_dir: Path) -> list[dict]:
    out: list[dict] = []
    steps_dir = Path(run_dir) / "steps"
    if not steps_dir.is_dir():
        return out
    for path in sorted(steps_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _control_hold(run_dir: Path) -> dict[str, Any]:
    """Longest gap between a power_cut and the next power_restore, in seconds."""
    path = Path(run_dir) / "reset_log.jsonl"
    if not path.is_file():
        return {"cycle_hold_s": None, "samples": 0}
    from datetime import datetime

    cuts: list[datetime] = []
    holds: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        command = str(entry.get("command") or "")
        stamp = entry.get("at") or entry.get("sent_at") or ""
        try:
            when = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if command.endswith(".on"):
            cuts.append(when)
        elif command.endswith(".off") and cuts:
            holds.append((when - cuts.pop()).total_seconds())
    return {"cycle_hold_s": round(min(holds), 3) if holds else None, "samples": len(holds)}


def measurement_from_run(run_dir: Path, profile: Profile, block: dict) -> dict[str, Any]:
    """Collect one commissioning run into its measured-candidate document."""
    burns: list[dict] = []
    verifies: list[dict] = []
    for payload in _read_steps(run_dir):
        step = str(payload.get("step") or "")
        if step.startswith("burn:"):
            burns.append({
                "chip": payload.get("chip"), "driver": payload.get("driver"),
                "verdict": payload.get("verdict"), "checks": payload.get("checks") or {},
                "markers": payload.get("markers") or {},
                "write_size": payload.get("write_size"), "device_md5": payload.get("device_md5"),
                "tool": payload.get("tool"), "tool_sha256": payload.get("tool_sha256"),
                "image": payload.get("image"), "image_sha256": payload.get("image_sha256"),
                "image_addr": payload.get("image_addr"),
                "burn_port": payload.get("burn_port"), "burn_baud": payload.get("burn_baud"),
                "cmd": payload.get("cmd") or (payload.get("tool_result") or {}).get("cmd"),
                "tool_log": payload.get("tool_log"),
            })
        elif step.startswith("verify:"):
            versions = payload.get("versions") or {}
            entry = {
                "chip": payload.get("chip"), "verdict": payload.get("verdict"),
                "versions": versions, "expectation": payload.get("expectation"),
                "checks": payload.get("checks") or {},
                "window_s": payload.get("window_s"),
                "measurement_mode": payload.get("measurement_mode"),
                "captured": payload.get("captured") or {},
            }
            entry["version_key"] = (payload.get("expectation") or {}).get("version_key")
            entry["observed"] = versions.get(str(entry["version_key"] or "")) if entry["version_key"] else None
            verifies.append(entry)

    missing: list[str] = []
    for chip in profile.chips:
        burn = next((item for item in burns if item.get("chip") == chip.id), None)
        if burn is None:
            missing.append(f"burn:{chip.id} (no step receipt)")
            continue
        checks = burn.get("checks") or {}
        if checks.get("exit_zero") is not True:
            missing.append(f"burn:{chip.id} exit_zero")
        if checks.get("markers") is not True:
            missing.append(f"burn:{chip.id} success markers")
        if not isinstance(burn.get("write_size"), int) or burn["write_size"] <= 0:
            missing.append(f"burn:{chip.id} WRITE SIZE")
        if not MD5.match(str(burn.get("device_md5") or "")):
            missing.append(f"burn:{chip.id} device MD5")
        verify = next((item for item in verifies if item.get("chip") == chip.id), None)
        if verify is None:
            missing.append(f"verify:{chip.id} (no step receipt)")
        elif not str(verify.get("observed") or "").strip():
            missing.append(f"verify:{chip.id} {verify.get('version_key')} banner")

    hold = _control_hold(run_dir)
    blocks = hold.get("cycle_hold_s")
    return {
        "schema": COMMISSION_SCHEMA,
        "created_at": now_bj(),
        "node": profile.node,
        "profile": {"name": profile.name, "path": str(profile.path),
                    "sha256": sha256_file(profile.path), "status": profile.status},
        "commissioning": {key: block.get(key) for key in REQUIRED_KEYS},
        "burns": burns,
        "verifies": verifies,
        "control": {"cycle_hold_s": blocks, "samples": hold.get("samples", 0),
                    "source": str(Path(run_dir) / "reset_log.jsonl")},
        "complete": not missing,
        "missing": missing,
    }


def write_measurement(run_dir: Path, profile: Profile, block: dict) -> Path:
    payload = measurement_from_run(run_dir, profile, block)
    path = Path(run_dir) / "commissioning.receipt.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


# ------------------------------------------------------------------ promote


def _cmd_flags(cmd: Any) -> dict[str, list[str]]:
    """Parse ``-name:value`` flags out of the argv the run really executed."""
    flags: dict[str, list[str]] = {}
    items = cmd if isinstance(cmd, list) else []
    for item in items:
        text = str(item)
        if not text.startswith("-") or ":" not in text:
            continue
        name, value = text[1:].split(":", 1)
        flags.setdefault(name.lower(), []).append(value)
    return flags


def promote_profile(profile_path: Path, measurement_path: Path, out_path: Path) -> dict[str, Any]:
    """Build the measured profile from one approved commissioning receipt.

    Every value written here comes from the receipt; nothing is guessed and no
    field is defaulted except ``verify.window_s`` (the window the measuring run
    actually used, else the driver default).
    """
    profile = Profile(Path(profile_path))
    if profile.measured:
        raise ProfileError(f"profile {profile.name} is already measured; nothing to promote")
    block = getattr(profile, "commissioning", None)
    if not block:
        raise ProfileError(f"profile {profile.name} has no commissioning block; nothing to promote")
    measurement = json.loads(Path(measurement_path).read_text(encoding="utf-8"))
    if measurement.get("schema") != COMMISSION_SCHEMA:
        raise ProfileError(f"measurement schema must be {COMMISSION_SCHEMA}, got {measurement.get('schema')!r}")
    recorded = (measurement.get("commissioning") or {}).get("approval_ref")
    if str(recorded or "").strip() != str(block["approval_ref"]).strip():
        raise ProfileError(
            f"measurement approval_ref {recorded!r} does not match the profile's {block['approval_ref']!r}")
    measured_sha = str((measurement.get("profile") or {}).get("sha256") or "")
    if measured_sha != sha256_file(Path(profile_path)):
        raise ProfileError("measurement was taken from a different profile file (sha256 mismatch); re-measure")

    raw = copy.deepcopy(profile.raw)
    missing: list[str] = []
    for chip_raw in raw["chips"]:
        chip_id = chip_raw["id"]
        burn = next((item for item in measurement.get("burns") or [] if item.get("chip") == chip_id), None)
        if burn is None:
            missing.append(f"burn:{chip_id}")
            continue
        checks = burn.get("checks") or {}
        if checks.get("exit_zero") is not True or checks.get("markers") is not True:
            missing.append(f"burn:{chip_id} graded PASS evidence")
            continue
        write_size = burn.get("write_size")
        device_md5 = str(burn.get("device_md5") or "")
        if not isinstance(write_size, int) or write_size <= 0:
            missing.append(f"burn:{chip_id} WRITE SIZE")
        if not MD5.match(device_md5):
            missing.append(f"burn:{chip_id} device MD5")
        chip_raw["burn"]["expected_write_bytes"] = write_size
        chip_raw["burn"]["expected_md5"] = device_md5.lower()
        if isinstance(burn.get("burn_baud"), int) and burn["burn_baud"] > 0:
            chip_raw["transport"]["burn_baud"] = burn["burn_baud"]
        if burn.get("burn_port"):
            chip_raw["transport"]["burn_port"] = str(burn["burn_port"])
        if not str(chip_raw["burn"].get("image_addr") or "").strip() and burn.get("image_addr"):
            chip_raw["burn"]["image_addr"] = str(burn["image_addr"])
        flags = _cmd_flags(burn.get("cmd"))
        if "signalbaud" in flags and not chip_raw["burn"].get("signal_baud"):
            chip_raw["burn"]["signal_baud"] = int(flags["signalbaud"][0])
        if "chiptype" in flags and not str(chip_raw["burn"].get("chip_type") or "").strip():
            chip_raw["burn"]["chip_type"] = flags["chiptype"][0]
        if "onlyburn" in flags and not chip_raw["burn"].get("onlyburn_parts"):
            chip_raw["burn"]["onlyburn_parts"] = list(flags["onlyburn"])

        verify = next((item for item in measurement.get("verifies") or [] if item.get("chip") == chip_id), None)
        if verify is None:
            missing.append(f"verify:{chip_id}")
            continue
        version_key = str(chip_raw["verify"].get("version_key"))
        observed = str((verify.get("versions") or {}).get(version_key) or "").strip()
        if not observed:
            missing.append(f"verify:{chip_id} {version_key} banner")
            continue
        chip_raw["verify"]["expect"] = observed
        window = chip_raw["verify"].get("window_s")
        if not isinstance(window, (int, float)) or window <= 0:
            chip_raw["verify"]["window_s"] = verify.get("window_s") or 120

    hold = (measurement.get("control") or {}).get("cycle_hold_s")
    timing = raw["control"].setdefault("timing", {})
    if isinstance(hold, (int, float)) and hold > 0:
        timing["cycle_hold_s"] = float(hold)
        timing.pop("timing_note", None)

    if missing:
        raise ProfileError(
            "commissioning receipt is incomplete, refusing to flip status: " + "; ".join(missing))

    raw["status"] = "measured"
    raw["verified"] = {
        "on": now_bj()[:10],
        "by": str(block["approved_by"]),
        "evidence": f"{Path(measurement_path).name} sha256={sha256_file(Path(measurement_path))}",
        "approval_ref": str(block["approval_ref"]),
    }
    raw.pop("commissioning", None)
    raw.pop("measurement_required", None)

    dest = Path(out_path)
    staged = dest.with_name(dest.name + ".staged")
    staged.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    try:
        promoted = Profile(staged)  # the emitted file must pass every measured gate
    except ProfileError:
        staged.unlink(missing_ok=True)
        raise
    staged.replace(dest)
    return {
        "profile": str(dest), "status": promoted.status,
        "sha256": sha256_file(dest),
        "expect": {chip.id: chip.verify.get("expect") for chip in promoted.chips},
        "expected_write_bytes": {chip.id: chip.burn.get("expected_write_bytes") for chip in promoted.chips},
    }


__all__ = ["COMMISSION_SCHEMA", "CommissionError", "validate_block", "guard_write", "measurement_from_run",
           "write_measurement", "promote_profile", "TIMING_KEYS"]

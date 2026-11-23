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
REQUIRED_KEYS = ("approved_by", "approval_ref", "pins", "allowed_steps", "max_write_steps", "max_runs")
#: one approval covers one bench: either the legacy single ``device`` object or the
#: multi-bench ``benches`` list, never both (a half-migrated block is ambiguous).
SINGLE_KEYS = ("node", "device")
BENCH_LIST_KEY = "benches"
OPTIONAL_KEYS = ("expires_at", "approved_at", "note")
BENCH_ENTRY_KEYS = {"bench_id", "node", "serial", "topology", "control_port",
                    "burn_ports", "log_ports", "spare_port", "approval_ref", "note"}
BENCH_ID = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
TIMING_KEYS = ("cut_to_strap_s", "strap_to_restore_s", "restore_to_strap_off_s", "cycle_hold_s")
#: drivers that grade a run on the flash numbers (WRITE SIZE + device MD5).
FLASH_NUMBER_DRIVERS = {"csk6-uart", "venusa-uart"}
PIN_KINDS = ("tool", "image")


def _port_digits(value: Any) -> str:
    """``COM34`` / ``34`` / ``int(34)`` all name the same port."""
    text = str(value if value is not None else "").strip().upper()
    if text.startswith("COM"):
        text = text[3:]
    return text if text.isdigit() else ""


def _pin(block: dict, chip) -> dict:
    pins = block.get("pins") or {}
    pin = pins.get(chip.id)
    return pin if isinstance(pin, dict) else {}


# ---------------------------------------------------------------- block


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
    unknown = set(block) - set(REQUIRED_KEYS) - set(SINGLE_KEYS) - {BENCH_LIST_KEY} - set(OPTIONAL_KEYS)
    if unknown:
        raise ProfileError(f"profile.commissioning has unknown keys: {sorted(unknown)}")
    missing = [key for key in REQUIRED_KEYS if key not in block]
    if missing:
        raise ProfileError(
            f"profile.commissioning is missing {missing}; a first run must pin the approver, "
            "the approval reference, every tool/image sha256 and the write budget")
    for key in ("approved_by", "approval_ref"):
        if not str(block.get(key) or "").strip():
            raise ProfileError(f"profile.commissioning.{key} must name the approver / approval reference")
    if ("device" in block) == (BENCH_LIST_KEY in block):
        raise ProfileError(
            "profile.commissioning must declare exactly one of 'device' (one bench, legacy) or "
            "'benches' (one entry per bench, each with its own approval_ref)")

    declared_nodes = [str(item) for item in (raw.get("nodes") or []) if str(item).strip()]
    if "device" in block:
        if not str(block.get("node") or "").strip():
            raise ProfileError("profile.commissioning.node must name the one approved host")
        if declared_nodes and str(block["node"]) not in declared_nodes:
            raise ProfileError(
                f"profile.commissioning.node {block['node']!r} is not one of the profile's nodes {declared_nodes}")
        _validate_device(block["device"])
    else:
        _validate_benches(block, declared_nodes)

    _validate_pins(block["pins"])
    steps = block.get("allowed_steps")
    if not (isinstance(steps, list) and steps and all(isinstance(item, str) and item for item in steps)):
        raise ProfileError("profile.commissioning.allowed_steps must be a non-empty list of step ids")
    if len(set(steps)) != len(steps):
        raise ProfileError("profile.commissioning.allowed_steps must not repeat a step")
    count = block.get("max_write_steps")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 2:
        raise ProfileError("profile.commissioning.max_write_steps must be 1 or 2")
    runs = block.get("max_runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs != 1:
        raise ProfileError("profile.commissioning.max_runs must be 1: a first run is never repeated")
    expires = block.get("expires_at")
    if expires is not None and not (isinstance(expires, str) and TIMESTAMP.match(expires)):
        raise ProfileError("profile.commissioning.expires_at must be 'YYYY-MM-DD HH:MM:SS' (北京时间)")
    approved = block.get("approved_at")
    if approved is not None and not (isinstance(approved, str) and TIMESTAMP.match(approved)):
        raise ProfileError("profile.commissioning.approved_at must be 'YYYY-MM-DD HH:MM:SS' (北京时间)")
    return block


def expiry_state(block: dict) -> dict:
    """Whether this approval is still inside its validity window.

    Expiry is a *write* gate, not a load gate: an expired block must still be
    readable so ``plan`` / ``preflight`` can explain what lapsed and so lint and
    the unit suite keep working after the date passes (a repo that cannot be
    read once an approval expires is a trap, not a guard).
    """
    expires = block.get("expires_at")
    if not isinstance(expires, str) or not TIMESTAMP.match(expires):
        return {"expires_at": None, "expired": False, "checked_at": now_bj()}
    return {"expires_at": expires, "expired": now_bj() > expires, "checked_at": now_bj()}


def check_expiry(block: dict, bench: dict | None = None) -> None:
    """Refuse to write on an approval whose window has passed."""
    state = expiry_state(block)
    if not state["expired"]:
        return
    where = f" for bench {bench['bench_id']!r}" if bench and bench.get("bench_id") else ""
    raise CommissionError(
        f"profile.commissioning.expires_at {state['expires_at']} has passed{where}; "
        f"now is {now_bj()}. The approval lapsed - a new approval (new expires_at, and for a spent "
        "bench a human decision) is required before any write")


def _validate_device(device: Any) -> None:
    if not isinstance(device, dict):
        raise ProfileError("profile.commissioning.device must be an object naming the one bench")
    unknown = set(device) - {"serial", "topology", "control_port", "burn_ports", "note", "bench_id", "log_ports"}
    if unknown:
        raise ProfileError(f"profile.commissioning.device has unknown keys: {sorted(unknown)}")
    if not str(device.get("serial") or "").strip():
        raise ProfileError("profile.commissioning.device.serial must pin the one device (USB serial / hwid)")
    if not _port_digits(device.get("control_port")):
        raise ProfileError("profile.commissioning.device.control_port must be COMx")
    ports = device.get("burn_ports")
    if not isinstance(ports, dict) or not ports:
        raise ProfileError("profile.commissioning.device.burn_ports must map every chip id to its port")
    for chip_id, port in ports.items():
        if not _port_digits(port):
            raise ProfileError(f"profile.commissioning.device.burn_ports.{chip_id} must be COMx")
    _validate_log_ports(device, "commissioning.device")


def _validate_log_ports(entry: dict, where: str) -> None:
    logs = entry.get("log_ports")
    if logs is None:
        return
    if not isinstance(logs, dict) or not logs:
        raise ProfileError(f"profile.{where}.log_ports must map every chip id to its log port")
    for chip_id, port in logs.items():
        if not _port_digits(port):
            raise ProfileError(f"profile.{where}.log_ports.{chip_id} must be COMx")


def _validate_benches(block: dict, declared_nodes: list[str]) -> None:
    """Each bench is a separate approval: own id, own node, own ports, own approval_ref."""
    benches = block.get("benches")
    if not isinstance(benches, list) or not benches:
        raise ProfileError("profile.commissioning.benches must be a non-empty list of bench entries")
    ids: list[str] = []
    refs: list[str] = []
    for index, entry in enumerate(benches):
        where = f"profile.commissioning.benches[{index}]"
        if not isinstance(entry, dict):
            raise ProfileError(f"{where} must be an object")
        unknown = set(entry) - BENCH_ENTRY_KEYS
        if unknown:
            raise ProfileError(f"{where} has unknown keys: {sorted(unknown)}")
        missing = [key for key in ("bench_id", "node", "serial", "control_port", "burn_ports", "approval_ref")
                   if key not in entry]
        if missing:
            raise ProfileError(
                f"{where} is missing {missing}; every bench pins its id, host, device identity, "
                "control/burn ports and its own approval_ref")
        bench_id = str(entry.get("bench_id") or "").strip()
        if not BENCH_ID.match(bench_id):
            raise ProfileError(f"{where}.bench_id must match {BENCH_ID.pattern}, got {bench_id!r}")
        ids.append(bench_id)
        node = str(entry.get("node") or "").strip()
        if not node:
            raise ProfileError(f"{where}.node must name the host this bench is wired to")
        if declared_nodes and node not in declared_nodes:
            raise ProfileError(f"{where}.node {node!r} is not one of the profile's nodes {declared_nodes}")
        if not str(entry.get("serial") or "").strip():
            raise ProfileError(
                f"{where}.serial must pin the device identity (usb_parent / USB serial); "
                "COM numbers are not an identity")
        if not _port_digits(entry.get("control_port")):
            raise ProfileError(f"{where}.control_port must be COMx")
        ports = entry.get("burn_ports")
        if not isinstance(ports, dict) or not ports:
            raise ProfileError(f"{where}.burn_ports must map every written chip id to its port")
        for chip_id, port in ports.items():
            if not _port_digits(port):
                raise ProfileError(f"{where}.burn_ports.{chip_id} must be COMx")
        _validate_log_ports(entry, f"commissioning.benches[{index}]")
        spare = entry.get("spare_port")
        if spare is not None and not _port_digits(spare):
            raise ProfileError(f"{where}.spare_port must be COMx when it is declared")
        ref = str(entry.get("approval_ref") or "").strip()
        if not ref:
            raise ProfileError(f"{where}.approval_ref must name this bench's own approval reference")
        refs.append(ref)
    for label, values in (("bench_id", ids), ("approval_ref", refs)):
        duplicated = sorted({value for value in values if values.count(value) > 1})
        if duplicated:
            raise ProfileError(
                f"profile.commissioning.benches repeats {label} {duplicated}; every bench needs its own")


def _validate_pins(pins: Any) -> None:
    if not isinstance(pins, dict) or not pins:
        raise ProfileError("profile.commissioning.pins must map every written chip to its tool/image sha256")
    for chip_id, pin in pins.items():
        if not isinstance(pin, dict) or set(pin) - set(PIN_KINDS):
            raise ProfileError(f"profile.commissioning.pins.{chip_id} must only hold 'tool' and 'image'")
        for kind in PIN_KINDS:
            item = pin.get(kind)
            if not isinstance(item, dict):
                raise ProfileError(f"profile.commissioning.pins.{chip_id}.{kind} must be an object")
            unknown = set(item) - {"asset", "sha256"}
            if unknown:
                raise ProfileError(
                    f"profile.commissioning.pins.{chip_id}.{kind} has unknown keys: {sorted(unknown)}")
            if not str(item.get("asset") or "").strip():
                raise ProfileError(f"profile.commissioning.pins.{chip_id}.{kind}.asset is required")
            sha = str(item.get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ProfileError(
                    f"profile.commissioning.pins.{chip_id}.{kind}.sha256 must be 64 lowercase hex chars")


# ---------------------------------------------------------------- guard


def select_bench(block: dict, bench_id: str | None) -> dict:
    """Return the one bench this run may drive, or refuse.

    A legacy single-``device`` block stays exactly as it was: one bench, no
    selector. A ``benches`` block demands an explicit ``--bench`` because one
    approval covers one bench and nothing may be inferred from port numbers.
    """
    benches = block.get(BENCH_LIST_KEY)
    if benches:
        known = [str(item["bench_id"]) for item in benches]
        wanted = str(bench_id or "").strip()
        if not wanted:
            raise CommissionError(
                f"this profile commissions {len(benches)} benches ({known}); "
                "--bench <bench_id> picks exactly one (one approval per bench)")
        for entry in benches:
            if str(entry["bench_id"]) == wanted:
                return {**entry, "multi": True}
        raise CommissionError(f"--bench {wanted!r} is not one of this profile's benches {known}")
    if bench_id:
        raise CommissionError(
            f"this profile's commissioning block covers one device only; --bench {bench_id!r} does not apply")
    device = block["device"]
    return {
        "bench_id": str(device.get("bench_id") or "dev01"),
        "node": str(block["node"]),
        "serial": str(device.get("serial")),
        "topology": device.get("topology"),
        "control_port": str(device.get("control_port")),
        "burn_ports": dict(device.get("burn_ports") or {}),
        "log_ports": dict(device.get("log_ports") or {}),
        "approval_ref": str(block["approval_ref"]),
        "raw_device": dict(device),
        "multi": False,
    }


def apply_bench(profile, bench: dict) -> None:
    """Point this run at the selected bench's declared ports.

    Only a multi-bench entry is applied: for a legacy single-``device`` block the
    profile already declares the ports and the guard compares the approval
    against them (applying it would make that comparison vacuous). For a real
    bench entry the ports *are* the approval, so the run drives exactly them.
    """
    if not bench.get("multi"):
        return
    profile.control["port"] = str(bench["control_port"])
    for chip in profile.chips:
        port = (bench.get("burn_ports") or {}).get(chip.id)
        if port:
            chip.transport["burn_port"] = str(port)
        log_port = (bench.get("log_ports") or {}).get(chip.id)
        if log_port:
            chip.transport["log_port"] = str(log_port)


def guard_write(profile: Profile, steps: list[str], plan: list[str], approval_ref: str | None,
                confirm_write: bool, bench_id: str | None = None,
                snapshot: dict | None = None) -> list[str]:
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
        if bench_id:
            raise CommissionError("--bench only selects a bench of a draft profile's commissioning block")
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
            "add approved_by / approval_ref / pins / allowed_steps / max_write_steps / max_runs "
            "and either one device or one benches[] entry per bench before a first run")
    bench = select_bench(block, bench_id)
    if approval_ref.strip() != str(bench["approval_ref"]).strip():
        raise CommissionError(
            f"--commission {approval_ref!r} does not match bench {bench['bench_id']!r}'s approval_ref "
            f"{bench['approval_ref']!r}")
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
    check_expiry(block, bench)
    check_bench(profile, block, bench, snapshot)
    return writes


def _norm_identity(value: Any) -> str:
    return " ".join(str(value or "").replace("\t", " ").split()).upper()


def _ident(snapshot: dict | None) -> dict:
    raw = (snapshot or {}).get("identity") or {}
    return {str(key): str(value).strip() for key, value in raw.items()
            if isinstance(value, str) and value.strip()}


def check_bench(profile: Profile, block: dict, bench: dict | None = None,
                snapshot: dict | None = None) -> dict:
    """The approval covers one bench - prove this run still drives that bench.

    Ports are compared against the approval. The one exception is an explicit
    ``--device-snapshot`` that names the *same physical device* (bench_id or
    usb_parent): Windows renumbers COM ports on re-plug, and NOAH-268/269 both
    recorded that only ``usb_parent``/``LocationPath`` is a stable identity, so a
    matching snapshot is accepted as a port refresh and recorded as such. A
    snapshot for another bench, or one carrying no identity, is refused.
    """
    if bench is None:
        bench = select_bench(block, None)
    node = str(bench.get("node") or "")
    declared = profile.raw.get("nodes")
    if isinstance(declared, list) and declared and node not in [str(item) for item in declared]:
        raise CommissionError(
            f"commissioning was approved for node {node!r}, but this bench declares {declared}")

    drift: list[tuple[str, str, str]] = []
    approved_control = _port_digits(bench.get("control_port"))
    actual_control = _port_digits(profile.control.get("port"))
    if approved_control != actual_control:
        drift.append(("control", approved_control, actual_control))
    for chip in profile.chips:
        approved = _port_digits((bench.get("burn_ports") or {}).get(chip.id))
        actual = _port_digits(chip.transport.get("burn_port"))
        if not approved:
            raise CommissionError(
                f"commissioning does not pin a burn port for chip {chip.id}; one bench means every port is named")
        if approved != actual:
            drift.append((f"{chip.id} burn", approved, actual))
    ports: dict[str, str] = {"control": str(profile.control.get("port"))}
    for chip in profile.chips:
        ports[f"{chip.id}:burn"] = str(chip.transport.get("burn_port"))

    refreshed: dict | None = None
    if drift:
        what, approved, actual = drift[0]
        if not bench.get("multi"):
            # legacy single bench: the approval is compared, not refreshed
            raise CommissionError(
                f"commissioning pinned {what} port COM{approved}, this run would drive COM{actual}")
        refreshed = _snapshot_refresh(bench, snapshot, drift)

    for chip in profile.chips:
        pin = _pin(block, chip)
        if not pin:
            if any(step == f"burn:{chip.id}" for step in block["allowed_steps"]):
                raise CommissionError(f"commissioning pins no tool/image for chip {chip.id}")
            continue
        for kind in PIN_KINDS:
            want = str((pin.get(kind) or {}).get("asset") or "")
            have = chip.tool_asset if kind == "tool" else chip.image_asset
            if want != str(have):
                raise CommissionError(
                    f"commissioning pins {chip.id} {kind} {want!r}, the profile would use {have!r}")
    return {"bench_id": str(bench["bench_id"]), "node": node,
            "approval_ref": str(bench["approval_ref"]), "ports": ports,
            "roles": bench_roles(bench),
            "pinned_control_port": str(bench.get("control_port")),
            "ports_refreshed_from_snapshot": refreshed is not None, **(refreshed or {})}


def _snapshot_refresh(bench: dict, snapshot: dict | None, drift: list[tuple[str, str, str]]) -> dict:
    """Accept renumbered COM ports only for the same physical bench.

    Windows renumbers COM ports on re-plug and only dev01 has a real USB serial
    (NOAH-268 A3), so the port numbers cannot be the pin. A snapshot that names
    the same ``bench_id`` (or the same ``usb_parent``) is accepted as a refresh
    and recorded; anything else - another bench, or no identity at all - is
    refused instead of silently writing to whatever COM happened to move.
    """
    what, approved, actual = drift[0]
    if not snapshot:
        raise CommissionError(
            f"bench {bench['bench_id']} pinned {what} port COM{approved}, this run would drive COM{actual}; "
            "re-enumerate this bench and pass a --device-snapshot that names the same bench_id/usb_parent")
    ident = _ident(snapshot)
    bench_id = ident.get("bench_id", "")
    serial = _norm_identity(bench.get("serial"))
    seen = _norm_identity(ident.get("usb_parent") or ident.get("device_id"))
    if not bench_id and not seen:
        raise CommissionError(
            "the device snapshot names no bench identity (bench_id / usb_parent); refusing to use it "
            "to renumber the approved ports - re-enumerate and update this bench's profile entry instead")
    if bench_id and bench_id != str(bench["bench_id"]):
        raise CommissionError(
            f"the device snapshot is for bench {bench_id!r}, this approval covers {bench['bench_id']!r}")
    if seen and serial and seen != serial:
        raise CommissionError(
            f"the device snapshot says usb_parent {seen}, this bench's approval pins {serial}")
    return {"snapshot": str(snapshot.get("source") or ""), "identity": ident,
            "ported_from": {name: {"pinned": f"COM{pin}", "snapshot": f"COM{got}"}
                            for name, pin, got in drift}}



def check_manifest_pins(profile: Profile, block: dict, assets) -> None:
    """Every pinned asset must exist in the manifest with exactly that sha256."""
    for chip in profile.chips:
        pin = _pin(block, chip)
        for kind in PIN_KINDS:
            item = pin.get(kind) or {}
            asset_id = str(item.get("asset") or "")
            if not asset_id:
                continue
            entry = assets.get(asset_id)
            want = str(item.get("sha256") or "").lower()
            if entry["sha256"].lower() != want:
                raise CommissionError(
                    f"commissioning pins {chip.id} {kind} {asset_id} sha256 {want}, "
                    f"but the manifest declares {entry['sha256']}")



# ------------------------------------------------------------------ one shot


def _bench_key(bench: dict | None) -> str:
    """``-dev02`` for a multi-bench profile; empty for the legacy single block.

    The legacy name is kept byte-for-byte so an approval already spent before
    this change (NOAH-263 / NOAH-266) is still seen as spent.
    """
    if not bench or not bench.get("multi"):
        return ""
    bench_id = str(bench.get("bench_id") or "").strip()
    return f"-{bench_id}" if bench_id else ""


def ledger_path(profile: Profile, approved_node: str | None = None, bench: dict | None = None) -> Path:
    """Per-bench record of the commissioning attempt.

    Same directory as the bench lock (``profile.lock_path`` when the bench
    declares one, otherwise the per-node temp dir), keyed by the approved host
    and - for a multi-bench profile - the bench id, so four benches on one node
    never share one ledger.
    """
    lock = profile.raw.get("lock_path")
    import tempfile  # noqa: PLC0415 - only needed for the default

    base = Path(lock).parent if lock else Path(tempfile.gettempdir())
    node = approved_node or profile.node
    return base / f"burner-{profile.node}-{node}{_bench_key(bench)}-commissioning.json"


def ledger_state(profile: Profile, approved_node: str | None = None, bench: dict | None = None) -> dict:
    path = ledger_path(profile, approved_node, bench)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def assert_not_used(profile: Profile, block: dict, bench: dict | None = None) -> None:
    """A first run happens once per bench; the approval is not a retry licence."""
    if bench is None:
        bench = select_bench(block, None)
    state = ledger_state(profile, str(bench["node"]), bench)
    if not state.get("write_started"):
        return
    spent = str(state.get("approval_ref") or "")
    ref = str(bench["approval_ref"]).strip()
    which = f"{spent}" if spent.strip() == ref else f"{spent} (this profile now names {ref})"
    raise CommissionError(
        f"bench {bench['bench_id']} already started a write on {state['write_started']} under approval "
        f"{which} (run_dir {state.get('run_dir')!r}); a bench is commissioned once and no automatic retry is "
        "allowed - a new approval_ref cannot re-open it. Move to the next bench; re-running this one needs a "
        "human decision about why it failed")


def consume(profile: Profile, block: dict, run_dir: Path, stage: str = "write_started",
            extra: dict | None = None, bench: dict | None = None) -> Path:
    """Record that this bench's approval is now spent, before the write touches the device."""
    if bench is None:
        bench = select_bench(block, None)
    path = ledger_path(profile, str(bench["node"]), bench)
    state = ledger_state(profile, str(bench["node"]), bench)
    state["node"] = str(bench["node"])
    state["bench_id"] = str(bench["bench_id"])
    # Assigned, never setdefault: the record must name the approval this bench
    # actually spent, not the first ref that ever touched this file.
    state["approval_ref"] = str(bench["approval_ref"])
    state["approved_by"] = str(block["approved_by"])
    state["device"] = bench_device(bench)
    state["roles"] = bench_roles(bench)
    state[stage] = now_bj()
    state["run_dir"] = str(run_dir)
    for key, value in (extra or {}).items():
        state[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def check_run_pins(profile: Profile, block: dict | None, chip, tool: Path, image: Path) -> None:
    """Refuse to touch the device when the pinned file is not the one on disk.

    Called by the burn phases after asset resolution and before the tool is
    started (or the power is cycled), so a mismatch means zero device action.
    """
    if not block:
        return
    pin = _pin(block, chip)
    for kind, path in (("tool", Path(tool)), ("image", Path(image))):
        want = str((pin.get(kind) or {}).get("sha256") or "").lower()
        if not want:
            raise CommissionError(
                f"commissioning pins no {kind} sha256 for chip {chip.id}; refusing to start the tool")
        actual = sha256_file(path)
        if actual != want:
            raise CommissionError(
                f"{kind} for chip {chip.id} does not match the commissioning pin: "
                f"want {want} ({path.name}), got {actual}")


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


def bench_roles(bench: dict) -> dict[str, str]:
    """The bench's port roles in the vocabulary NOAH-267 fixed on 2026-11-19.

    ``csk6:log`` / ``csk6:burn`` are separate CH344 interfaces, while HISI logs
    and burns through the *same* physical port - it is recorded as one
    ``hisi:log_and_burn`` role so nobody reads two identical COM numbers as a
    copy-paste bug. A declared spare (interface D) is kept for re-enumeration
    checks.
    """
    roles: dict[str, str] = {"control": str(bench.get("control_port"))}
    for chip_id in ("csk6", "hisi"):
        log_port = (bench.get("log_ports") or {}).get(chip_id)
        burn_port = (bench.get("burn_ports") or {}).get(chip_id)
        if log_port and burn_port and str(log_port) == str(burn_port):
            roles[f"{chip_id}:log_and_burn"] = str(log_port)
            continue
        if log_port:
            roles[f"{chip_id}:log"] = str(log_port)
        if burn_port:
            roles[f"{chip_id}:burn"] = str(burn_port)
    for chip_id, burn_port in (bench.get("burn_ports") or {}).items():
        if chip_id not in ("csk6", "hisi"):
            roles[f"{chip_id}:burn"] = str(burn_port)
    if bench.get("spare_port"):
        roles["spare"] = str(bench["spare_port"])
    return roles


def bench_device(bench: dict) -> dict:
    """The device projection recorded in the ledger and the measurement receipt."""
    if not bench.get("multi") and isinstance(bench.get("raw_device"), dict):
        return dict(bench["raw_device"])
    return {"serial": str(bench.get("serial") or ""),
            "topology": bench.get("topology"),
            "control_port": bench.get("control_port"),
            "burn_ports": dict(bench.get("burn_ports") or {}),
            "log_ports": dict(bench.get("log_ports") or {})}


def measurement_from_run(run_dir: Path, profile: Profile, block: dict,
                         bench: dict | None = None) -> dict[str, Any]:
    """Collect one commissioning run into its measured-candidate document."""
    if bench is None:
        bench = select_bench(block, None)
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
                "tool_exit": payload.get("tool_exit"),
                "optlog": payload.get("optlog"), "optlog_sha256": payload.get("optlog_sha256"),
                "optlog_bytes": payload.get("optlog_bytes"),
                "power": payload.get("power"),
                "timing": payload.get("timing"),
                "tool_result": payload.get("tool_result"),
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
                "power_cycle": (payload.get("power_cycle") or {}).get("verdict"),
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
        missing.extend(_burn_missing(chip, burn))
        verify = next((item for item in verifies if item.get("chip") == chip.id), None)
        if verify is None:
            missing.append(f"verify:{chip.id} (no step receipt)")
        else:
            if not str(verify.get("observed") or "").strip():
                missing.append(f"verify:{chip.id} {verify.get('version_key')} banner")
            if verify.get("power_cycle") != "PASS":
                missing.append(f"verify:{chip.id} power-cycle receipts")

    hold = _control_hold(run_dir)
    blocks = hold.get("cycle_hold_s")
    return {
        "schema": COMMISSION_SCHEMA,
        "created_at": now_bj(),
        "node": bench.get("node") or block.get("node") or profile.node,
        "bench": {"bench_id": str(bench["bench_id"]), "serial": str(bench.get("serial") or ""),
                  "approval_ref": str(bench["approval_ref"]), "multi": bool(bench.get("multi"))},
        "device": bench_device(bench),
        "pins": block.get("pins"),
        "profile": {"name": profile.name, "path": str(profile.path),
                    "sha256": sha256_file(profile.path), "status": profile.status},
        "commissioning": {**{key: block.get(key) for key in REQUIRED_KEYS},
                          "approval_ref": str(bench["approval_ref"])},
        "burns": burns,
        "verifies": verifies,
        "control": {"cycle_hold_s": blocks, "samples": hold.get("samples", 0),
                    "power_sequence": str((profile.control.get("timing") or {}).get("power_sequence")
                                          or "power_then_connect"),
                    "timings": [burn.get("timing") for burn in burns if burn.get("timing")],
                    "source": str(Path(run_dir) / "reset_log.jsonl"),
                    "commands": _control_commands(run_dir)},
        "resources": _resources(run_dir, burns),
        "complete": not missing,
        "missing": missing,
    }


def _burn_missing(chip, burn: dict) -> list[str]:
    """Grade one burn receipt against the driver's own acceptance rule."""
    if str(burn.get("verdict")) != "PASS":
        return [f"burn:{chip.id} verdict {burn.get('verdict')!r}"]
    checks = burn.get("checks") or {}
    if chip.driver in FLASH_NUMBER_DRIVERS:
        missing = [f"burn:{chip.id} {name}" for name in ("exit_zero", "markers")
                   if checks.get(name) is not True]
        if not isinstance(burn.get("write_size"), int) or burn["write_size"] <= 0:
            missing.append(f"burn:{chip.id} WRITE SIZE")
        if not MD5.match(str(burn.get("device_md5") or "")):
            missing.append(f"burn:{chip.id} device MD5")
        return missing
    # HISI BurnTool: optLog success token + no failure token + power receipts;
    # the tool's exit code is recorded but not graded (the GUI build can stay open).
    missing = [f"burn:{chip.id} {name}" for name in
               ("success_tokens", "no_failure_tokens", "power_receipts")
               if checks.get(name) is not True]
    return missing


def _control_commands(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "reset_log.jsonl"
    if not path.is_file():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _resources(run_dir: Path, burns: list[dict]) -> dict[str, Any]:
    """Resource evidence for the run: disk, evidence size, tool outcomes."""
    import shutil  # noqa: PLC0415 - only needed here

    run_dir = Path(run_dir)
    total = 0
    serial_bytes: dict[str, int] = {}
    for path in run_dir.rglob("*"):
        if path.is_file():
            size = path.stat().st_size
            total += size
            if path.parent.name == "serial_logs":
                serial_bytes[path.stem] = size
    try:
        free = shutil.disk_usage(run_dir).free
    except OSError:
        free = None
    return {
        "at": now_bj(),
        "evidence_bytes": total,
        "disk_free_bytes": free,
        "serial_log_bytes": serial_bytes,
        "tool_timeouts": [item.get("chip") for item in burns if item.get("tool_exit") is None],
        "tool_exits": {str(item.get("chip")): item.get("tool_exit") for item in burns},
    }


def write_measurement(run_dir: Path, profile: Profile, block: dict, bench: dict | None = None) -> Path:
    payload = measurement_from_run(run_dir, profile, block, bench)
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
    benches = block.get(BENCH_LIST_KEY)
    if benches:
        matches = [item for item in benches
                   if str(item["approval_ref"]).strip() == str(recorded or "").strip()]
        if not matches:
            raise ProfileError(
                f"measurement approval_ref {recorded!r} is not one of this profile's bench approvals "
                f"{[str(item['approval_ref']) for item in benches]}")
        bench = {**matches[0], "multi": True}
        if str(measurement.get("node") or "") != str(bench["node"]):
            raise ProfileError(
                f"measurement was taken on node {measurement.get('node')!r}, "
                f"bench {bench['bench_id']!r} is wired to {bench['node']!r}")
        if (measurement.get("device") or {}) != bench_device(bench):
            raise ProfileError(
                f"measurement device does not match bench {bench['bench_id']!r} (serial/ports/topology)")
    else:
        bench = select_bench(block, None)
        if str(recorded or "").strip() != str(block["approval_ref"]).strip():
            raise ProfileError(
                f"measurement approval_ref {recorded!r} does not match the profile's {block['approval_ref']!r}")
        if str(measurement.get("node") or "") != str(block["node"]):
            raise ProfileError(
                f"measurement was taken on node {measurement.get('node')!r}, the approval names {block['node']!r}")
        if (measurement.get("device") or {}) != (block.get("device") or {}):
            raise ProfileError("measurement device does not match the device this approval pinned")
    if (measurement.get("pins") or {}) != (block.get("pins") or {}):
        raise ProfileError("measurement pins do not match the tool/image sha256 this approval pinned")
    measured_sha = str((measurement.get("profile") or {}).get("sha256") or "")
    if measured_sha != sha256_file(Path(profile_path)):
        raise ProfileError("measurement was taken from a different profile file (sha256 mismatch); re-measure")

    raw = copy.deepcopy(profile.raw)
    if bench.get("multi"):
        # One measured profile describes one bench: pin the measured bench's own
        # ports, drop the sibling approvals, and keep the per-node lock name so
        # two benches on one host never run at the same time.
        raw["name"] = f"{profile.name}-{bench['bench_id']}"
        raw["board"] = f"{raw.get('board')} · bench {bench['bench_id']} ({bench['node']})"
        raw["nodes"] = [str(bench["node"])]
        raw["control"]["port"] = str(bench["control_port"])
        for chip_raw in raw["chips"]:
            burn_port = (bench.get("burn_ports") or {}).get(chip_raw["id"])
            if burn_port:
                chip_raw["transport"]["burn_port"] = str(burn_port)
            log_port = (bench.get("log_ports") or {}).get(chip_raw["id"])
            if log_port:
                chip_raw["transport"]["log_port"] = str(log_port)
    missing: list[str] = []
    for chip_raw in raw["chips"]:
        chip_id = chip_raw["id"]
        chip = profile.chip(chip_id)
        burn = next((item for item in measurement.get("burns") or [] if item.get("chip") == chip_id), None)
        if burn is None:
            missing.append(f"burn:{chip_id}")
            continue
        graded = _burn_missing(chip, burn)
        if graded:
            missing.extend(graded)
            continue
        if chip.driver in FLASH_NUMBER_DRIVERS:
            write_size = burn.get("write_size")
            device_md5 = str(burn.get("device_md5") or "").lower()
            chip_raw["burn"]["expected_write_bytes"] = write_size
            chip_raw["burn"]["expected_md5"] = device_md5
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


__all__ = ["COMMISSION_SCHEMA", "CommissionError", "validate_block", "select_bench", "apply_bench",
           "guard_write", "check_bench", "bench_device", "bench_roles", "expiry_state", "check_expiry",
           "check_manifest_pins", "check_run_pins", "ledger_path", "ledger_state", "assert_not_used",
           "consume", "measurement_from_run", "write_measurement", "promote_profile", "TIMING_KEYS"]

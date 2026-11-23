#!/usr/bin/env python3
"""burner - one burn skill for VenusA/CSK and HISI/HI3863.

Commands:
  plan        print the profile plan; no hardware access
  preflight   enumerate ports, read current versions, verify asset hashes
  step <id>   run exactly one step of the profile plan
  all         run the whole state machine and write run.receipt.json
  receipt     rebuild run.receipt.json from the completed steps

Guards (exit code 3): no profile, draft profile on a write step, no
``--confirm-write``, bench locked by another live process, unimplemented
driver. Phase FAIL/BLOCKED is exit code 4. Usage/profile/asset errors are 2,
including an unreadable or unresolvable ``--device-snapshot`` document.

Exit codes: 0 ok, 2 usage/profile/asset/snapshot, 3 refused (guard),
4 step FAIL/BLOCKED.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.assets import AssetError, Assets, FetchBlocked  # noqa: E402
from core.bench import Audit, BenchError, BenchLock  # noqa: E402
from core.commission import (CommissionError, apply_bench, assert_not_used,  # noqa: E402
                             bench_roles, check_manifest_pins, check_run_pins, consume,
                             expiry_state, guard_write, promote_profile, select_bench,
                             write_measurement)  # noqa: E402
from core.contract import SCHEMAS, now_bj  # noqa: E402
from core.device_snapshot import (DeviceSnapshotError, apply_snapshot,  # noqa: E402
                                  resolve_snapshot)
from core.phases import (phase_boot, phase_burn, phase_preflight, phase_receipt,  # noqa: E402
                         phase_recover, phase_verify, safe_recover, _step_receipt)
from core.profile import Profile, ProfileError  # noqa: E402
from core.state import RECEIPT, RunState, StateError, plan_steps, write_steps  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_GUARD, EXIT_FAIL = 0, 2, 3, 4
REPO_ROOT = Path(__file__).resolve().parents[1]


def snapshot_lines(snapshot: dict) -> list[str]:
    lines = [
        "",
        f"device snapshot: {snapshot['source']}",
        f"  source: {snapshot['source']}",
        f"  schema: {snapshot['schema']}",
        f"  generated_at: {snapshot['generated_at'] or '-'}",
        f"  mode: {snapshot['mode']}",
        "  roles:",
    ]
    for role, item in snapshot["roles"].items():
        lines.append(f"    {role}: {item['port']} ({item['state']})")
    return lines


def plan_text(profile: Profile, assets: Assets, material_dir: Path | None, cache_dir: Path | None,
              snapshot: dict | None = None, bench: dict | None = None,
              benches: list[dict] | None = None) -> str:
    info = profile.describe()
    lines = [
        f"node: {info['node']}",
        f"profile: {info['name']} ({info['status']}, family={info['family']}) at {info['profile']}",
        f"board: {info['board']}",
        f"time contract: {info['time_contract']['format']} ({info['time_contract']['zone']}, no offset)",
        f"control: {info['control']['kind']} {info['control']['port']}@{info['control']['baud']}",
    ]
    timing = info["control"].get("timing") or {}
    if timing.get("power_sequence"):
        lines.append(f"power sequence: {timing['power_sequence']}"
                     + (f" (lead {timing.get('tool_connect_lead_s')} s, "
                        f"connect timeout {timing.get('tool_connect_timeout_s')} s)"
                        if timing.get("power_sequence") == "connect_then_power" else ""))
    if bench:
        lines.append(
            f"bench: {bench['bench_id']} (node {bench['node']}, usb {bench['serial']}, "
            f"approval {bench['approval_ref']}, control {bench['control_port']}, "
            f"burn {bench.get('burn_ports') or {}}, log {bench.get('log_ports') or {}})")
        lines.append("  roles: " + ", ".join(f"{role}={port}" for role, port in bench_roles(bench).items()))
    elif benches:
        lines.append(f"benches: {len(benches)} (pick one with --bench <bench_id>)")
        for entry in benches:
            roles = ", ".join(f"{role}={port}" for role, port in bench_roles(entry).items())
            lines.append(f"  - {entry['bench_id']}: node {entry['node']} usb {entry['serial']} "
                         f"approval {entry['approval_ref']} | {roles}")
    block = getattr(profile, "commissioning", None)
    if block:
        window = expiry_state(block)
        if window.get("expires_at"):
            lines.append(
                f"approval window: {window['expires_at']}"
                + ("  [EXPIRED - writes refused until a new approval]" if window["expired"] else "  (valid)"))
    lines += [
        f"material dir: {material_dir or '-'}",
        f"asset cache: {cache_dir or '-'}",
        "",
        "steps:",
    ]
    for index, step in enumerate(plan_steps(profile), 1):
        detail = ""
        if step.startswith(("boot:", "burn:", "verify:", "recover:")):
            chip_id = step.split(":", 1)[1]
            chip = profile.chip(chip_id)
            if step.startswith("burn:"):
                detail = f"{chip.driver}: {chip.tool_asset} -> {chip.image_asset}"
                if chip.package_asset:
                    detail += f"  (package {chip.package_asset})"
            elif step.startswith("verify:"):
                detail = f"{chip.verify.get('version_key')}={chip.verify.get('expect')}"
            elif step.startswith("boot:"):
                detail = " / ".join(f"{key}={value}" for key, value in profile.commands.items()
                                    if key.startswith("strap") or key.startswith("power"))
            else:
                detail = str(chip.recover.get("command") or chip.recover.get("driver"))
        lines.append(f"  {index}. {step}" + (f"  [{detail}]" if detail else ""))
    lines += ["", "assets:"]
    planned = list(profile.asset_ids())
    for chip in profile.chips:
        if chip.package_asset and chip.package_asset not in planned:
            planned.append(chip.package_asset)
    for asset_id in planned:
        entry = assets.describe(asset_id)
        if entry["oss"].get("download") == "local-only":
            where = "availability=local-only（未登记可下载位置）"
        else:
            where = f"oss://<bucket>/{entry['oss']['key']}"
        lines.append(f"  {asset_id}: {entry['target_chip']} {entry['version']} {entry['file']} "
                     f"sha256={entry['sha256'][:16]}... {where}")
    if snapshot:
        lines += snapshot_lines(snapshot)
    return "\n".join(lines)


def _execute_step(ctx: dict, state: RunState, step: str) -> dict:
    profile: Profile = ctx["profile"]
    if ":" in step:
        kind, chip_id = step.split(":", 1)
        chip = profile.chip(chip_id)
        if kind == "boot":
            return phase_boot(ctx, chip)
        if kind == "burn":
            return phase_burn(ctx, chip)
        if kind == "verify":
            return phase_verify(ctx, chip)
        if kind == "recover":
            return phase_recover(ctx, chip)
        raise StateError(f"unknown step kind {kind!r}")
    if step == "preflight":
        return phase_preflight(ctx)
    if step == RECEIPT:
        return phase_receipt(ctx, state)
    raise StateError(f"unknown step {step!r}")


def _write_receipt_file(ctx: dict, state: RunState, receipt: dict) -> Path:
    path = Path(ctx["run_dir"]) / "run.receipt.json"
    payload = {"schema": SCHEMAS["receipt"], **receipt}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _guard_write(profile: Profile, steps: list[str], plan: list[str], commission: str | None,
                 confirm_write: bool, bench_id: str | None = None,
                 snapshot: dict | None = None) -> list[str]:
    """Delegate to the commission guard: measured profiles work as before, a
    draft only writes through an approved, bounded commissioning block."""
    return guard_write(profile, steps, plan, commission, confirm_write, bench_id, snapshot)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - older consoles
        pass

    parser = argparse.ArgumentParser(description="VenusA/CSK + HISI/HI3863 unified burn driver")
    parser.add_argument("command", choices=["plan", "preflight", "step", "all", "receipt", "promote"])
    parser.add_argument("step_id", nargs="?", default=None, help="step id for 'step' (e.g. burn:venusa)")
    parser.add_argument("--profile", required=True, help="profile JSON path (no built-in default)")
    parser.add_argument("--run-dir", default=None, help="task run directory for receipts and logs")
    parser.add_argument("--from", dest="from_path", default=None,
                        help="promote: commissioning.receipt.json the measured profile is built from")
    parser.add_argument("--out", default=None, help="promote: path of the measured profile to write")
    parser.add_argument("--assets", default=str(REPO_ROOT / "assets" / "manifest.json"),
                        help="unified asset manifest (default: <repo>/assets/manifest.json)")
    parser.add_argument("--material-dir", default=None, help="directory holding this run's material tree")
    parser.add_argument("--asset-cache", default=None, help="node-level tool cache (shared across projects)")
    parser.add_argument("--issue", default="", help="issue id recorded in receipts and reset_log")
    parser.add_argument("--expect", action="append", default=[], metavar="CHIP=VERSION",
                        help="override the expected version of one chip for this run")
    parser.add_argument("--fetch-assets", action="store_true",
                        help="download missing assets through the manifest's OSS method (env credentials only)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only, touch no hardware")
    parser.add_argument("--device-snapshot", default=None, metavar="FILE",
                        help="device-io snapshot or role-map JSON (ark.device-io.v1 / "
                             "ark.device-role-map.v1); its ports override the profile's")
    parser.add_argument("--confirm-write", action="store_true", help="required for boot/burn/recover steps")
    parser.add_argument("--commission", default=None, metavar="APPROVAL_REF",
                        help="first bring-up on a draft profile: the approval reference from the profile's "
                             "commissioning block (plus --confirm-write and --node); writes "
                             "commissioning.receipt.json")
    parser.add_argument("--bench", default=None, metavar="BENCH_ID",
                        help="which bench of a multi-bench commissioning block this run may drive; "
                             "one bench = one approval_ref = one ledger")
    parser.add_argument("--node", default=None, metavar="HOST",
                        help="executing host; required with --commission and must equal the approved node")
    args = parser.parse_args(argv)

    if args.command != "promote" and not args.run_dir:
        print("burner: --run-dir is required for this command", file=sys.stderr)
        return EXIT_USAGE

    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else None
    material_dir = Path(args.material_dir).expanduser().resolve() if args.material_dir else None
    cache_dir = Path(args.asset_cache).expanduser().resolve() if args.asset_cache else None

    try:
        if args.command == "promote":
            if not args.from_path or not args.out:
                print("burner: 'promote' needs --from <commissioning.receipt.json> and --out <measured.json>",
                      file=sys.stderr)
                return EXIT_USAGE
            try:
                result = promote_profile(Path(args.profile).expanduser().resolve(),
                                         Path(args.from_path).expanduser().resolve(),
                                         Path(args.out).expanduser().resolve())
            except ProfileError as exc:
                print(f"burner: {exc}", file=sys.stderr)
                return EXIT_GUARD
            print(json.dumps(result, ensure_ascii=False))
            return EXIT_OK

        profile = Profile(Path(args.profile).expanduser().resolve())
        snapshot: dict | None = None
        if args.device_snapshot:
            snapshot = resolve_snapshot(Path(args.device_snapshot).expanduser().resolve(), profile)
        block = getattr(profile, "commissioning", None)
        benches = (block or {}).get("benches") or []
        bench: dict | None = None
        if block:
            if benches and not str(args.bench or "").strip() and (args.command == "plan" or args.dry_run):
                # Read-only preview: list every bench instead of silently racing one.
                assets = Assets(Path(args.assets).expanduser().resolve())
                print(plan_text(profile, assets, material_dir, cache_dir, snapshot, benches=benches))
                print("\n[dry-run] pick one bench with --bench <bench_id>; no serial port opened, "
                      "no burn tool started")
                return EXIT_OK
            bench = select_bench(block, args.bench)
            apply_bench(profile, bench)
        elif args.bench:
            raise ProfileError(
                f"--bench {args.bench!r} only applies to a profile with a commissioning block; "
                f"{profile.name} has none")
        if snapshot:
            apply_snapshot(profile, snapshot)
        assets = Assets(Path(args.assets).expanduser().resolve())
        assets.require(profile.asset_ids())
        plan = plan_steps(profile)

        if args.commission:
            if args.command not in ("plan", "preflight", "all"):
                raise CommissionError(
                    "--commission runs the whole approved plan in order (preflight first); "
                    "only 'all' may write, 'plan' and 'preflight' are read-only")
            if block:
                if benches and not bench:
                    raise CommissionError(
                        "this profile commissions several benches; --bench <bench_id> names the one "
                        "this approval covers")
                approved_node = str((bench or {}).get("node") or block.get("node") or "").strip()
                if not str(args.node or "").strip():
                    raise CommissionError(
                        "--commission needs --node <the approved host>; an approval covers exactly one node")
                if args.node.strip() != approved_node:
                    raise CommissionError(
                        f"--node {args.node!r} is not the node this approval covers ({approved_node!r})")
                check_manifest_pins(profile, block, assets)
                assert_not_used(profile, block, bench)

        if args.command == "plan" or args.dry_run:
            print(plan_text(profile, assets, material_dir, cache_dir, snapshot, bench=bench))
            print("\n[dry-run] no serial port opened, no burn tool started")
            return EXIT_OK

        expect: dict[str, str] = {}
        for item in args.expect:
            if "=" not in item:
                raise ProfileError(f"--expect needs CHIP=VERSION, got {item!r}")
            chip_id, version = item.split("=", 1)
            profile.chip(chip_id.strip())
            expect[chip_id.strip()] = version.strip()

        if args.fetch_assets:
            for asset_id in profile.asset_ids():
                report = assets.verify(asset_id, material_dir, cache_dir)
                if report["sha_ok"] and report["size_ok"]:
                    continue
                if not cache_dir:
                    print(f"asset {asset_id} needs --asset-cache to land the download", file=sys.stderr)
                    continue
                install = assets.get(asset_id)["install"]
                dest = cache_dir / str(install.get("cache_rel") or assets.get(asset_id)["file"])
                try:
                    print(json.dumps(assets.fetch(asset_id, dest), ensure_ascii=False))
                except FetchBlocked as exc:
                    print(f"fetch blocked for {asset_id}: {exc}", file=sys.stderr)

        ctx: dict[str, Any] = {
            "profile": profile, "run_dir": run_dir, "material_dir": material_dir,
            "cache_dir": cache_dir, "assets": assets, "expect": expect,
            "issue": args.issue or profile.node,
            "node": args.node or profile.node,
            "audit": Audit(run_dir, args.issue or profile.node),
            "device_snapshot": snapshot,
            "bench": bench,
            "commissioning": block if args.commission else None,
        }
        state = RunState(run_dir)

        if args.command == "step":
            if not args.step_id:
                raise ProfileError("command 'step' needs a step id, e.g. step burn:venusa")
            steps = [args.step_id]
        elif args.command == "receipt":
            steps = [RECEIPT]
        elif args.command == "preflight":
            steps = ["preflight"]
        else:
            steps = list(plan)

        _guard_write(profile, steps, plan, args.commission, args.confirm_write, args.bench, snapshot)
        state.require(steps[0], plan)

        if ctx.get("commissioning") and args.command == "all":
            # Zero device action when a pinned file is not the pinned bytes:
            # resolve and hash everything before the bench lock or any port.
            for chip in profile.chips:
                if not (chip.tool_asset and chip.image_asset):
                    continue
                tool = assets.resolve(chip.tool_asset, material_dir, cache_dir)
                image = assets.resolve(chip.image_asset, material_dir, cache_dir)
                check_run_pins(profile, ctx["commissioning"], chip, tool, image)

        worst = EXIT_OK
        receipt_path: Path | None = None
        ledger_consumed = False
        with BenchLock(profile.node, Path(profile.raw["lock_path"]) if profile.raw.get("lock_path") else None):
            for step in steps:
                state.require(step, plan)
                if ctx.get("commissioning") and step in write_steps(plan) and not ledger_consumed:
                    path = consume(profile, ctx["commissioning"], run_dir, bench=ctx.get("bench"))
                    ledger_consumed = True
                    print(json.dumps({"step": "commissioning-ledger", "consumed": str(path)}, ensure_ascii=False))
                receipt = _execute_step(ctx, state, step)
                verdict = str(receipt.get("verdict", "FAIL"))
                if step == RECEIPT:
                    receipt_path = _write_receipt_file(ctx, state, receipt)
                    state.complete(step, verdict, receipt_path)
                    print(json.dumps({"step": step, "verdict": verdict, "receipt": str(receipt_path)},
                                     ensure_ascii=False))
                    if verdict != "PASS":
                        worst = EXIT_FAIL
                    break
                path = _step_receipt(ctx, step, receipt)
                state.complete(step, verdict, path)
                if verdict != "PASS":
                    state.fail(step, verdict, str(receipt.get("reason", "")), path)
                    worst = EXIT_FAIL
                print(json.dumps({"step": step, "verdict": verdict, "receipt": str(path)}, ensure_ascii=False))
                if worst != EXIT_OK:
                    if args.command == "all":
                        if step == "preflight":
                            # A pre-gate failure touched no device: restoring power
                            # would itself be a relay command (NOAH-266: two
                            # uut-switch1.off were sent by a unit test on a live bench).
                            recovery = {"reason": f"{step} -> {verdict}", "skipped": True,
                                        "note": "preflight gates before any device action; nothing to restore"}
                        else:
                            recovery = safe_recover(ctx, f"{step} -> {verdict}")
                        print(json.dumps({"step": "safe-recover", "result": recovery}, ensure_ascii=False))
                        failed_receipt = phase_receipt(ctx, state)
                        receipt_path = _write_receipt_file(ctx, state, failed_receipt)
                        print(json.dumps({"step": "receipt", "verdict": failed_receipt.get("verdict"),
                                          "receipt": str(receipt_path)}, ensure_ascii=False))
                    break
        if ledger_consumed:
            consume(profile, ctx["commissioning"], run_dir, stage="finished", bench=ctx.get("bench"),
                    extra={"first_run_verdict": "PASS" if worst == EXIT_OK else "FAIL"})
        if ctx.get("commissioning") and args.command == "all" and worst == EXIT_OK:
            path = write_measurement(run_dir, profile, ctx["commissioning"], ctx.get("bench"))
            print(json.dumps({"step": "commissioning-receipt", "receipt": str(path),
                              "note": "promote this profile from the receipt once the bench evidence is archived"},
                             ensure_ascii=False))
        return worst
    except CommissionError as exc:
        print(f"burner: {exc}", file=sys.stderr)
        return EXIT_GUARD
    except ProfileError as exc:
        print(f"burner: {exc}", file=sys.stderr)
        text = str(exc)
        guard = any(token in text for token in ("draft", "confirm-write"))
        return EXIT_GUARD if guard else EXIT_USAGE
    except DeviceSnapshotError as exc:
        print(f"burner: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (BenchError, StateError) as exc:
        print(f"burner: {exc}", file=sys.stderr)
        return EXIT_GUARD if "locked" in str(exc) or "不抢占" in str(exc) else EXIT_USAGE
    except (AssetError, FetchBlocked) as exc:
        print(f"burner: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

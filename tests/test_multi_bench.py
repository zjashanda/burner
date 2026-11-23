"""Multi-bench commissioning and the power/connect sequence.

Two things this file pins down, both from NOAH-267 stage 2:

* one node with four benches: every bench carries its own approval_ref, its own
  ports and its own ledger, one run each, and a bench that already started a
  write can never be re-opened by a new approval_ref;
* ``control.timing.power_sequence=connect_then_power``: the burn tool holds the
  burn port *before* the relay cycles power, and the receipt records the whole
  timeline.

No hardware, no network: phases are faked, the "tool" is a local python script
and every port name stays a string.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core import bench as bench_module  # noqa: E402
from core import commission as commission_module  # noqa: E402
from core import phases as phases_module  # noqa: E402
from core.assets import Assets  # noqa: E402
from core.profile import Profile, ProfileError  # noqa: E402
from core.state import plan_steps  # noqa: E402

WIN003 = ROOT / "scripts" / "profiles" / "csk6-hisi-win003.json"
WIN005 = ROOT / "scripts" / "profiles" / "csk6-hisi-win005.json"
DEV01 = ROOT / "scripts" / "profiles" / "csk6-hisi-dev01.json"

WRITE_SIZE = 5685248
DEVICE_MD5 = "0xf6d6369435d52784efe1119ebd08f582"

BENCH_APPROVALS = {
    "win003": {f"dev0{i}": f"NOAH-267-win003-dev0{i}" for i in range(1, 5)},
    "win005": {f"dev0{i}": f"NOAH-267-win005-dev0{i}" for i in range(1, 5)},
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def draft_raw(tmp: Path, profile: Path = WIN003, **overrides) -> dict:
    raw = load(profile)
    raw["lock_path"] = str(tmp / "bench.lock")
    block = dict(raw["commissioning"])
    # Fixtures carry their own window: the shipped approvals must never make the
    # suite pass or fail because of today's date.
    block["expires_at"] = "2099-12-31 23:59:59"
    block.update(overrides)
    raw["commissioning"] = block
    return raw


def write_profile(tmp: Path, raw: dict, name: str = "profile.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_material(tmp: Path, raw: dict) -> Path:
    assets = Assets(ROOT / "assets" / "manifest.json")
    material = tmp / "material"
    for chip in raw["chips"]:
        for asset_id in (chip["burn"].get("tool"), chip["burn"].get("image")):
            if not asset_id:
                continue
            rel = assets.get(asset_id)["install"].get("material_rel")
            path = material / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
    return material


def no_serial():
    raise AssertionError("this path must not touch serial")


def fake_preflight(ctx):
    return {"started_at": "2026-11-20 10:00:00", "verdict": "PASS", "ports": [],
            "expected_ports": [], "missing_ports": [], "captured": {}, "versions_before": {},
            "expectations": {}, "assets": {"assets": {}, "all_ok": True}}


def fake_burn(ctx, chip):
    if chip.driver == "hisi-burntool":
        return {"chip": chip.id, "driver": chip.driver, "verdict": "PASS",
                "checks": {"success_tokens": True, "no_failure_tokens": True, "power_receipts": True},
                "markers": {}, "tool": "BurnTool.exe", "tool_sha256": "a" * 64,
                "image": "ws63.fwpkg", "image_sha256": "b" * 64,
                "cmd": ["BurnTool.exe", "-com:39", "-bin:ws63.fwpkg", "-signalbaud:1000000",
                        "-chiptype:13", "-onlyburn:root_loaderboot_sign.bin"],
                "tool_exit": 0, "optlog": "optLog_1.txt", "optlog_sha256": "c" * 64, "optlog_bytes": 100,
                "tool_log": str(Path(ctx["run_dir"]) / "tool_logs" / "tool_hisi.log")}
    return {"chip": chip.id, "driver": chip.driver, "verdict": "PASS",
            "checks": {"exit_zero": True, "markers": True, "write_size": True, "device_md5": True,
                       "power_receipts": True, "connect_marker": True},
            "markers": {marker: True for marker in chip.burn.get("success_markers") or ["ok"]},
            "write_size": WRITE_SIZE, "device_md5": DEVICE_MD5,
            "tool": "tool.exe", "tool_sha256": "a" * 64, "image": "fw.bin", "image_sha256": "b" * 64,
            "image_addr": "0x000000", "burn_port": str(chip.transport["burn_port"]), "burn_baud": 1500000,
            "cmd": ["Uart_Burn_Tool.exe", "-b", "1500000", "-p", str(chip.transport["burn_port"]),
                    "-f", "fw.bin", "-a", "0x000000"],
            "timing": {"sequence": "connect_then_power", "connect_marker_seen": True},
            "tool_log": str(Path(ctx["run_dir"]) / "tool_logs" / "tool_csk6.log")}


def fake_verify(ctx, chip):
    version = "V40.1.11"
    return {"started_at": "2026-11-20 10:05:00", "chip": chip.id, "captured": {},
            "versions": {chip.verify["version_key"]: version},
            "expectation": {"chip": chip.id, "version_key": chip.verify["version_key"],
                            "expected": "", "got": version},
            "window_s": 120, "measurement_mode": True,
            "power_cycle": {"verdict": "PASS"},
            "checks": {"version_keys_present": True, "expected_version": None, "power_cycle": "PASS"},
            "verdict": "PASS"}


def fake_receipt(ctx, state):
    return {"verdict": "PASS"}


def no_device_action(ctx, reason):
    return {"reason": reason, "skipped": True, "note": "unit test: no relay is driven"}


def run_bench(tmp: Path, raw: dict, bench_id: str | None, approval: str | None,
              node: str | None = "win003", extra: list[str] | None = None, **patches):
    path = write_profile(tmp, raw)
    material = make_material(tmp, raw)
    argv = ["all", "--profile", str(path), "--run-dir", str(tmp),
            "--material-dir", str(material), "--confirm-write"]
    if approval:
        argv += ["--commission", approval]
    if bench_id:
        argv += ["--bench", bench_id]
    if node:
        argv += ["--node", node]
    argv += extra or []
    with mock.patch.object(cli, "phase_preflight", patches.get("preflight", fake_preflight)), \
            mock.patch.object(cli, "phase_burn", patches.get("burn", fake_burn)), \
            mock.patch.object(cli, "phase_verify", patches.get("verify", fake_verify)), \
            mock.patch.object(cli, "phase_receipt", patches.get("receipt", fake_receipt)), \
            mock.patch.object(cli, "safe_recover", patches.get("safe_recover", no_device_action)), \
            mock.patch.object(cli, "check_run_pins", patches.get("pins", lambda *a, **k: None)):
        return cli.main(argv), path


class BenchBlockValidationTests(unittest.TestCase):
    def profile(self, raw: dict) -> Profile:
        with tempfile.TemporaryDirectory() as tmp:
            return Profile(write_profile(Path(tmp), raw))

    def test_device_and_benches_together_are_ambiguous(self):
        raw = draft_raw(Path(tempfile.mkdtemp()))
        raw["commissioning"]["device"] = load(DEV01)["commissioning"]["device"]
        with self.assertRaises(ProfileError):
            self.profile(raw)

    def test_every_bench_needs_its_own_id_and_approval(self):
        for field in ("bench_id", "approval_ref", "serial", "control_port", "burn_ports", "node"):
            raw = draft_raw(Path(tempfile.mkdtemp()))
            raw["commissioning"]["benches"][0].pop(field)
            with self.assertRaises(ProfileError, msg=field):
                self.profile(raw)

    def test_duplicate_bench_id_or_approval_is_refused(self):
        raw = draft_raw(Path(tempfile.mkdtemp()))
        raw["commissioning"]["benches"][1]["bench_id"] = "dev01"
        with self.assertRaises(ProfileError):
            self.profile(raw)
        raw = draft_raw(Path(tempfile.mkdtemp()))
        raw["commissioning"]["benches"][1]["approval_ref"] = raw["commissioning"]["benches"][0]["approval_ref"]
        with self.assertRaises(ProfileError):
            self.profile(raw)

    def test_bench_node_must_be_a_declared_node(self):
        raw = draft_raw(Path(tempfile.mkdtemp()))
        raw["commissioning"]["benches"][0]["node"] = "win005"
        with self.assertRaises(ProfileError):
            self.profile(raw)

    def test_unknown_bench_key_is_refused(self):
        raw = draft_raw(Path(tempfile.mkdtemp()))
        raw["commissioning"]["benches"][0]["extra"] = "nope"
        with self.assertRaises(ProfileError):
            self.profile(raw)

    def test_the_shipped_bench_profiles_are_coherent(self):
        for path, node, count in ((WIN003, "win003", 4), (WIN005, "win005", 4)):
            profile = Profile(path)
            benches = profile.commissioning["benches"]
            self.assertEqual(len(benches), count)
            self.assertEqual([item["bench_id"] for item in benches],
                             ["dev01", "dev02", "dev03", "dev04"])
            for item in benches:
                self.assertEqual(item["node"], node)
                self.assertEqual(item["approval_ref"], f"NOAH-267-{node}-{item['bench_id']}")
                self.assertIn("usb_parent", profile.raw["port_map_reference"]["identity"])
            # no COM table may be copied from dev01 to another bench
            self.assertEqual(len({item["control_port"] for item in benches}), count)
            self.assertEqual(len({item["burn_ports"]["csk6"] for item in benches}), count)

    def test_legacy_single_device_profile_still_loads(self):
        profile = Profile(DEV01)
        self.assertNotIn("benches", profile.commissioning)
        self.assertEqual(profile.commissioning["approval_ref"], "NOAH-266")
        self.assertEqual(profile.commissioning["device"]["control_port"], "COM36")


class BenchGuardTests(unittest.TestCase):
    def test_multi_bench_needs_the_bench_selector(self):
        app = BENCH_APPROVALS["win003"]["dev01"]
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_bench(Path(tmp), draft_raw(Path(tmp)), None, app)
        self.assertEqual(code, 3)

    def test_wrong_approval_ref_for_the_selected_bench_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_bench(Path(tmp), draft_raw(Path(tmp)), "dev02",
                                BENCH_APPROVALS["win003"]["dev03"])
        self.assertEqual(code, 3)

    def test_approval_does_not_cover_another_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_bench(Path(tmp), draft_raw(Path(tmp)), "dev02",
                                BENCH_APPROVALS["win003"]["dev02"], node="win005")
        self.assertEqual(code, 3)

    def test_unknown_bench_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_bench(Path(tmp), draft_raw(Path(tmp)), "dev09",
                                "NOAH-267-win003-dev09")
        self.assertEqual(code, 3)

    def test_plan_renders_the_selected_bench_without_touching_ports(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with mock.patch.object(bench_module, "serial_module", no_serial), \
                    mock.patch.object(phases_module, "serial_module", no_serial), \
                    contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "--profile", str(WIN003), "--run-dir", tmp, "--bench", "dev02"])
            text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("bench: dev02", text)
        self.assertIn("NOAH-267-win003-dev02", text)
        self.assertIn("COM40", text)          # dev02's burn port, not dev01's COM34
        self.assertIn("connect_then_power", text)
        self.assertIn("burn:csk6", text)

    def test_plan_without_a_bench_lists_every_bench(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "--profile", str(WIN003), "--run-dir", tmp])
            text = buffer.getvalue()
        self.assertEqual(code, 0)
        for bench_id in ("dev01", "dev02", "dev03", "dev04"):
            self.assertIn(bench_id, text)
        self.assertIn("--bench", text)

    def test_a_snapshot_for_another_bench_is_refused(self):
        role_map = {
            "schema": "ark.device-role-map.v1", "ok": True, "node": "win003", "bench_id": "dev03",
            "usb_parent": r"USB\VID_1A86&PID_55D5\6&2e71c5&0&1",
            "roles": {role: {"state": "resolved", "port": port} for role, port in (
                ("control", "COM51"), ("csk6:log", "COM47"), ("csk6:burn", "COM49"),
                ("hisi:log", "COM50"), ("hisi:burn", "COM50"))},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = tmp_path / "snapshot.json"
            snapshot.write_text(json.dumps(role_map), encoding="utf-8")
            code, _ = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                BENCH_APPROVALS["win003"]["dev02"],
                                extra=["--device-snapshot", str(snapshot)])
        self.assertEqual(code, 3)

    def test_a_snapshot_without_identity_cannot_renumber_the_approval(self):
        role_map = {
            "schema": "ark.device-role-map.v1", "ok": True,
            "roles": {role: {"state": "resolved", "port": port} for role, port in (
                ("control", "COM71"), ("csk6:log", "COM68"), ("csk6:burn", "COM70"),
                ("hisi:log", "COM69"), ("hisi:burn", "COM69"))},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = tmp_path / "snapshot.json"
            snapshot.write_text(json.dumps(role_map), encoding="utf-8")
            code, _ = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                BENCH_APPROVALS["win003"]["dev02"],
                                extra=["--device-snapshot", str(snapshot)])
        self.assertEqual(code, 3)

    def test_a_snapshot_of_the_same_bench_may_refresh_its_ports(self):
        role_map = {
            "schema": "ark.device-role-map.v1", "ok": True, "node": "win003", "bench_id": "dev02",
            "usb_parent": r"USB\VID_1A86&PID_55D5\6&1655da82&0&1",
            "roles": {role: {"state": "resolved", "port": port} for role, port in (
                ("control", "COM61"), ("csk6:log", "COM58"), ("csk6:burn", "COM60"),
                ("hisi:log", "COM59"), ("hisi:burn", "COM59"))},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = tmp_path / "snapshot.json"
            snapshot.write_text(json.dumps(role_map), encoding="utf-8")
            code, path = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                   BENCH_APPROVALS["win003"]["dev02"],
                                   extra=["--device-snapshot", str(snapshot)])
            ledger = commission_module.ledger_state(Profile(path), "win003",
                                                    {"bench_id": "dev02", "multi": True})
        self.assertEqual(code, 0)
        self.assertEqual(ledger["bench_id"], "dev02")

    def test_a_legacy_profile_keeps_the_old_snapshot_behaviour(self):
        """No drift, no complaint: the single-device block only compares ports."""
        dev01 = load(DEV01)
        roles = {"control": dev01["control"]["port"],
                 "csk6:log": dev01["chips"][0]["transport"]["log_port"],
                 "csk6:burn": dev01["chips"][0]["transport"]["burn_port"],
                 "hisi:log": dev01["chips"][1]["transport"]["log_port"],
                 "hisi:burn": dev01["chips"][1]["transport"]["burn_port"]}
        role_map = {"schema": "ark.device-role-map.v1", "ok": True,
                    "roles": {role: {"state": "resolved", "port": port} for role, port in roles.items()}}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = tmp_path / "snapshot.json"
            snapshot.write_text(json.dumps(role_map), encoding="utf-8")
            profile = Profile(DEV01)
            resolved = cli.resolve_snapshot(snapshot, profile)
            cli.apply_snapshot(profile, resolved)
            check = commission_module.check_bench(profile, profile.commissioning,
                                                  commission_module.select_bench(profile.commissioning, None),
                                                  resolved)
        self.assertFalse(check["ports_refreshed_from_snapshot"])

    def test_a_legacy_profile_refuses_a_renumbered_port(self):
        dev01 = load(DEV01)
        roles = {"control": dev01["control"]["port"],
                 "csk6:log": dev01["chips"][0]["transport"]["log_port"],
                 "csk6:burn": "COM99",
                 "hisi:log": dev01["chips"][1]["transport"]["log_port"],
                 "hisi:burn": dev01["chips"][1]["transport"]["burn_port"]}
        role_map = {"schema": "ark.device-role-map.v1", "ok": True, "bench_id": "dev01",
                    "roles": {role: {"state": "resolved", "port": port} for role, port in roles.items()}}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            snapshot = tmp_path / "snapshot.json"
            snapshot.write_text(json.dumps(role_map), encoding="utf-8")
            profile = Profile(DEV01)
            resolved = cli.resolve_snapshot(snapshot, profile)
            cli.apply_snapshot(profile, resolved)
            with self.assertRaises(commission_module.CommissionError):
                commission_module.check_bench(profile, profile.commissioning,
                                              commission_module.select_bench(profile.commissioning, None),
                                              resolved)


class BenchPortRoleTests(unittest.TestCase):
    """NOAH-267 fixed the role vocabulary: A=log, B=CSK6 burn, C=HISI log_and_burn, D=spare."""

    def test_roles_follow_the_confirmed_rule(self):
        for path, first in ((WIN003, {"control": "COM36", "csk6:log": "COM35",
                                      "csk6:burn": "COM34", "hisi:log_and_burn": "COM32",
                                      "spare": "COM33"}),
                            (WIN005, {"control": "COM15", "csk6:log": "COM36",
                                      "csk6:burn": "COM37", "hisi:log_and_burn": "COM39",
                                      "spare": "COM38"})):
            profile = Profile(path)
            benches = profile.commissioning["benches"]
            self.assertEqual(commission_module.bench_roles(benches[0]), first)
            for bench in benches:
                roles = commission_module.bench_roles(bench)
                # B is CSK6-only: it must never be the same port as the CSK6 log
                self.assertNotEqual(roles["csk6:log"], roles["csk6:burn"], bench["bench_id"])
                # C carries HISI log *and* burn as one shared physical port
                self.assertIn("hisi:log_and_burn", roles, bench["bench_id"])
                self.assertNotIn("hisi:log", roles, bench["bench_id"])
                self.assertNotIn("hisi:burn", roles, bench["bench_id"])
                self.assertEqual(roles["hisi:log_and_burn"], bench["burn_ports"]["hisi"])
                self.assertEqual(roles["hisi:log_and_burn"], bench["log_ports"]["hisi"])
                self.assertIn("spare", roles, bench["bench_id"])

    def test_the_profiles_declare_the_role_rule_and_a_spare(self):
        for path in (WIN003, WIN005):
            raw = load(path)
            self.assertIn("C", raw["port_roles"])
            self.assertIn("log_and_burn", raw["port_roles"]["C"])
            self.assertIn("B", raw["port_roles"])
            self.assertIn("仅 CSK6", raw["port_roles"]["B"])
            for bench in raw["commissioning"]["benches"]:
                self.assertRegex(bench["spare_port"], r"^COM\d+$")

    def test_plan_prints_the_shared_c_port(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "--profile", str(WIN003), "--run-dir", tmp, "--bench", "dev02"])
            text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("hisi:log_and_burn=COM39", text)
        self.assertIn("csk6:burn=COM40", text)
        self.assertIn("spare=COM37", text)


class BenchLedgerTests(unittest.TestCase):
    def test_each_bench_gets_its_own_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, path = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                   BENCH_APPROVALS["win003"]["dev02"])
            self.assertEqual(code, 0)
            profile = Profile(path)
            paths = {bench["bench_id"]: commission_module.ledger_path(profile, "win003", {
                "bench_id": bench["bench_id"], "multi": True})
                for bench in profile.commissioning["benches"]}
            self.assertEqual(len(set(paths.values())), 4)
            for bench_id, ledger in paths.items():
                self.assertIn("win003", ledger.name)
                self.assertIn(bench_id, ledger.name)
            spent = commission_module.ledger_state(profile, "win003",
                                                   {"bench_id": "dev02", "multi": True})
            self.assertEqual(spent["approved_by"], "郑保山")
            self.assertEqual(spent["approval_ref"], "NOAH-267-win003-dev02")
            self.assertEqual(spent["roles"]["hisi:log_and_burn"], "COM39")
            self.assertEqual(spent["roles"]["csk6:burn"], "COM40")
            self.assertTrue(spent["write_started"])
            self.assertFalse(commission_module.ledger_state(
                profile, "win003", {"bench_id": "dev03", "multi": True}))

    def test_one_bench_never_blocks_the_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.assertEqual(run_bench(tmp_path, draft_raw(tmp_path), "dev01",
                                       BENCH_APPROVALS["win003"]["dev01"])[0], 0)
            self.assertEqual(run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                       BENCH_APPROVALS["win003"]["dev02"])[0], 0)

    def test_a_spent_bench_refuses_a_second_run_even_under_a_new_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.assertEqual(run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                       BENCH_APPROVALS["win003"]["dev02"])[0], 0)
            again = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                              BENCH_APPROVALS["win003"]["dev02"])
            self.assertEqual(again[0], 3)
            # a profile edit that renames the ref does not re-open the bench
            raw = draft_raw(tmp_path)
            for bench in raw["commissioning"]["benches"]:
                if bench["bench_id"] == "dev02":
                    bench["approval_ref"] = "NOAH-267-win003-dev02-RETRY"
            renamed = run_bench(tmp_path, raw, "dev02", "NOAH-267-win003-dev02-RETRY")
            self.assertEqual(renamed[0], 3)

    def test_the_legacy_ledger_name_is_unchanged(self):
        profile = Profile(DEV01)
        legacy = commission_module.ledger_path(profile, "win003")
        self.assertEqual(legacy.name, f"burner-{profile.node}-win003-commissioning.json")


class BenchPromoteTests(unittest.TestCase):
    def test_promote_writes_a_single_bench_measured_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, path = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                   BENCH_APPROVALS["win003"]["dev02"])
            self.assertEqual(code, 0)
            receipt = tmp_path / "commissioning.receipt.json"
            measurement = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(measurement["complete"], measurement["missing"])
            self.assertEqual(measurement["bench"]["bench_id"], "dev02")
            self.assertEqual(measurement["commissioning"]["approval_ref"], "NOAH-267-win003-dev02")
            self.assertEqual(measurement["device"]["control_port"], "COM41")

            out = tmp_path / "dev02.measured.json"
            code = cli.main(["promote", "--profile", str(path), "--from", str(receipt),
                             "--out", str(out)])
            self.assertEqual(code, 0)
            promoted = Profile(out)
            self.assertTrue(promoted.measured)
            self.assertEqual(promoted.name, "csk6-hisi-win003-dev02")
            self.assertEqual(promoted.control["port"], "COM41")
            self.assertEqual(promoted.chip("csk6").transport["burn_port"], "COM40")
            self.assertEqual(promoted.chip("hisi").transport["burn_port"], "COM39")
            self.assertEqual(promoted.raw["nodes"], ["win003"])
            self.assertNotIn("benches", promoted.raw)
            self.assertNotIn("commissioning", promoted.raw)

    def test_promote_refuses_a_measurement_from_another_bench(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code, path = run_bench(tmp_path, draft_raw(tmp_path), "dev02",
                                   BENCH_APPROVALS["win003"]["dev02"])
            self.assertEqual(code, 0)
            receipt = tmp_path / "commissioning.receipt.json"
            measurement = json.loads(receipt.read_text(encoding="utf-8"))
            measurement["device"]["control_port"] = "COM51"
            receipt.write_text(json.dumps(measurement, ensure_ascii=False), encoding="utf-8")
            out = tmp_path / "measured.json"
            code = cli.main(["promote", "--profile", str(path), "--from", str(receipt),
                             "--out", str(out)])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())


class _FakeAssets:
    """Resolves the two chip assets to local fixture files (no manifest, no OSS)."""

    def __init__(self, tool: Path, image: Path, chip):
        self.tool, self.image, self.chip = Path(tool), Path(image), chip

    def resolve(self, asset_id, material_dir, cache_dir):
        return self.tool if asset_id == self.chip.tool_asset else self.image


class PowerSequenceTests(unittest.TestCase):
    """The tool must hold the burn port before the relay cycles power."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _tool(self, connect: bool = True) -> Path:
        script = self.dir / "fake_tool.py"
        lines = [
            "import sys, time",
            'print("Uart Burn Tool  Version: 1.0.5", flush=True)',
            'print("OPEN PORT COM40 SUCCESS", flush=True)',
            "time.sleep(0.3)",
        ]
        if connect:
            lines += [
                'print("CONNECT WITH CHIP SUCCESS", flush=True)',
                'print("CHIP TYPE = 0x60120001", flush=True)',
                'print("WRITE SIZE 5685248", flush=True)',
                'print("MD5 CHECK SUCCESS", flush=True)',
                'print("DEVICE MD5 = 0xf6d6369435d52784efe1119ebd08f582", flush=True)',
                'print("FLASH 0x0 SECTOR DOWNLOAD SUCCESS", flush=True)',
            ]
        else:
            lines += ["time.sleep(30)"]
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return script

    def test_tool_opens_the_port_before_power_is_cycled(self):
        script = self._tool()
        log = self.dir / "tool.log"
        seen: list[str] = []

        def on_start():
            seen.append(log.read_text(encoding="utf-8") if log.is_file() else "")
            return {"verdict": "PASS"}

        result = bench_module.run_tool_watched(
            [sys.executable, str(script)], self.dir, log, 20,
            lead_s=0.1, on_start=on_start, connect_markers=["CONNECT WITH CHIP SUCCESS"],
            failure_markers=["CONNECT CHIP OVERTIME"], connect_timeout_s=10)
        self.assertEqual(result["rc"], 0)
        # the tool had already opened the port when the relay ran
        self.assertIn("OPEN PORT COM40 SUCCESS", seen[0])
        self.assertTrue(result["timing"]["connect_marker_seen"])
        self.assertEqual(result["timing"]["sequence"], "connect_then_power")
        events = [item["event"] for item in result["timeline"]]
        self.assertLess(events.index("tool_started"), events.index("power_sequence_started"))
        self.assertLess(events.index("power_sequence_started"), events.index("chip_connected"))
        self.assertTrue(result["timing"]["tool_started_at"])
        self.assertTrue(result["timing"]["connected_at"])

    def test_missing_connect_marker_is_recorded_and_times_out(self):
        script = self._tool(connect=False)
        log = self.dir / "tool.log"
        started = time.monotonic()
        result = bench_module.run_tool_watched(
            [sys.executable, str(script)], self.dir, log, 3,
            lead_s=0.0, on_start=lambda: {"verdict": "PASS"},
            connect_markers=["CONNECT WITH CHIP SUCCESS"],
            failure_markers=["CONNECT CHIP OVERTIME"], connect_timeout_s=0.8)
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse(result["timing"]["connect_marker_seen"])
        self.assertEqual(result["rc"], -9, "the tool is killed when the overall timeout expires")

    def test_connect_then_power_is_graded_in_the_burn_receipt(self):
        profile = Profile(WIN003)
        bench = commission_module.select_bench(profile.commissioning, "dev02")
        commission_module.apply_bench(profile, bench)
        chip = profile.chip("csk6")
        tool = self.dir / "Uart_Burn_Tool.exe"
        tool.write_bytes(b"tool-bytes")
        image = self.dir / "fw.bin"
        image.write_bytes(b"image-bytes")
        ctx = {"profile": profile, "run_dir": str(self.dir),
               "assets": _FakeAssets(tool, image, chip),
               "material_dir": None, "cache_dir": None, "audit": mock.MagicMock()}
        canned = {"cmd": ["Uart_Burn_Tool.exe"], "rc": 0, "elapsed_s": 1.0,
                  "output": "\n".join([
                      "OPEN PORT COM40 SUCCESS", "CONNECT WITH CHIP SUCCESS",
                      "CHIP TYPE = 0x60120001", "WRITE SIZE 5685248",
                      "MD5 CHECK SUCCESS", "DEVICE MD5 = 0xf6d6369435d52784efe1119ebd08f582",
                      "FLASH 0x0 SECTOR DOWNLOAD SUCCESS"]),
                  "power": {"cycle": {"verdict": "PASS"}},
                  "timing": {"sequence": "connect_then_power", "connect_marker_seen": True,
                             "connect_markers": ["CONNECT WITH CHIP SUCCESS"]},
                  "timeline": [{"at": "2026-11-20 10:00:00", "event": "tool_started"},
                               {"at": "2026-11-20 10:00:02", "event": "power_sequence_started"},
                               {"at": "2026-11-20 10:00:06", "event": "chip_connected"}]}

        def fake_cycle(ctx_arg, chip_arg, power):
            power["cycle"] = {"verdict": "PASS"}
            return {"verdict": "PASS"}

        with mock.patch.object(phases_module, "run_tool_watched", lambda *a, **k: canned), \
                mock.patch.object(phases_module, "PortLogger", _FakePortLogger), \
                mock.patch.object(phases_module, "_csk6_power_cycle", fake_cycle):
            receipt = phases_module.phase_burn(ctx, chip)
        self.assertEqual(receipt["checks"]["connect_marker"], True)
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["timing"]["sequence"], "connect_then_power")
        self.assertEqual(receipt["power"]["sequence"], "connect_then_power")
        events = [call.args[1] for call in ctx["audit"].event.call_args_list]
        self.assertIn("power_sequence_started", events)
        self.assertIn("chip_connected", events)

    def test_connect_then_power_without_a_connect_marker_fails(self):
        profile = Profile(WIN003)
        bench = commission_module.select_bench(profile.commissioning, "dev02")
        commission_module.apply_bench(profile, bench)
        chip = profile.chip("csk6")
        tool = self.dir / "Uart_Burn_Tool.exe"
        tool.write_bytes(b"tool-bytes")
        image = self.dir / "fw.bin"
        image.write_bytes(b"image-bytes")
        ctx = {"profile": profile, "run_dir": str(self.dir),
               "assets": _FakeAssets(tool, image, chip),
               "material_dir": None, "cache_dir": None, "audit": mock.MagicMock()}
        canned = {"cmd": ["Uart_Burn_Tool.exe"], "rc": 0, "elapsed_s": 1.0,
                  "output": "OPEN PORT COM40 SUCCESS\nCONNECT CHIP OVERTIME",
                  "power": {"cycle": {"verdict": "PASS"}},
                  "timing": {"sequence": "connect_then_power", "connect_marker_seen": False},
                  "timeline": []}

        def fake_cycle(ctx_arg, chip_arg, power):
            power["cycle"] = {"verdict": "PASS"}
            return {"verdict": "PASS"}

        with mock.patch.object(phases_module, "run_tool_watched", lambda *a, **k: canned), \
                mock.patch.object(phases_module, "PortLogger", _FakePortLogger), \
                mock.patch.object(phases_module, "_csk6_power_cycle", fake_cycle):
            receipt = phases_module.phase_burn(ctx, chip)
        self.assertEqual(receipt["checks"]["connect_marker"], False)
        self.assertEqual(receipt["verdict"], "FAIL")
        self.assertIn("connect_marker", receipt["reason"])


class _FakePortLogger:
    def __init__(self, port, baud, log_path):
        self.port, self.baud, self.log_path = port, baud, Path(log_path)
        self.stop_event = __import__("threading").Event()
        self.bytes, self.lines, self.error = 0, [], ""

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("AP Boot Ready !!!\n", encoding="utf-8", newline="\n")
        self.lines = ["AP Boot Ready !!!"]
        self.bytes = 20


class TimingProfileTests(unittest.TestCase):
    def test_connect_then_power_requires_a_connect_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["control"]["timing"].pop("tool_connect_timeout_s")
            with self.assertRaises(ProfileError):
                Profile(write_profile(Path(tmp), raw, "p.json"))

    def test_unknown_power_sequence_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["control"]["timing"]["power_sequence"] = "power_after_boot"
            with self.assertRaises(ProfileError):
                Profile(write_profile(Path(tmp), raw, "p.json"))

    def test_negative_lead_time_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["control"]["timing"]["tool_connect_lead_s"] = -1
            with self.assertRaises(ProfileError):
                Profile(write_profile(Path(tmp), raw, "p.json"))

    def test_the_default_sequence_is_the_old_behaviour(self):
        profile = Profile(DEV01)
        self.assertIsNone((profile.control.get("timing") or {}).get("power_sequence"))
        self.assertEqual(plan_steps(profile),
                         ["preflight", "burn:csk6", "verify:csk6", "burn:hisi", "verify:hisi", "receipt"])


class IndexMultiBenchTests(unittest.TestCase):
    def test_the_rotation_capability_is_registered(self):
        index = load(ROOT / "index" / "index.json")
        capabilities = {item["capability_id"]: item for item in index["capabilities"]}
        capability = capabilities["burn.csk6-hisi-multi-bench"]
        self.assertEqual(capability["status"], "draft")
        self.assertEqual(capability["profiles"], ["csk6-hisi-win003", "csk6-hisi-win005"])
        workflows = {item["workflow_id"] for item in index["workflows"]}
        self.assertIn(capability["workflow"], workflows)
        refs = [bench["approval_ref"] for item in capabilities["burn.csk6-hisi-dev01"]["bench_sets"]
                for bench in item["benches"]]
        self.assertEqual(len(refs), 8)
        self.assertEqual(len(set(refs)), 8)
        self.assertIn("NOAH-267-win003-dev02", refs)
        self.assertIn("NOAH-267-win005-dev04", refs)

    def test_the_index_says_the_profiles_are_still_draft(self):
        index = load(ROOT / "index" / "index.json")
        for project_id in ("csk6-hisi-win003", "csk6-hisi-win005"):
            project = next(item for item in index["projects"] if item["project_id"] == project_id)
            self.assertEqual(project["profile_status"], "draft")
            self.assertTrue(project["blocked_reason"])


if __name__ == "__main__":
    unittest.main()

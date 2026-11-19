"""Commissioning: the audited first-run path from ``draft`` to ``measured``.

No hardware: the phase functions are replaced by fakes that emit receipts with
the same shape a real run writes, and the promote step re-reads them. The point
of these tests is that the circle (no write until measured / no measurement
without a write) is closed *only* through an explicit approval block that pins
one node, one device and every tool/image sha256, that a first run happens
once, and that a status flip without the graded values is refused.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core import commission as commission_module  # noqa: E402
from core.assets import Assets  # noqa: E402
from core.profile import Profile, ProfileError  # noqa: E402

DEV01 = ROOT / "scripts" / "profiles" / "csk6-hisi-dev01.json"
WRITE_SIZE = 5685248
DEVICE_MD5 = "0xf6d6369435d52784efe1119ebd08f582"
APPROVAL = "NOAH-263"
NODE = "win003"


def draft_raw(tmp: Path, **block_overrides) -> dict:
    raw = json.loads(DEV01.read_text(encoding="utf-8"))
    raw["lock_path"] = str(tmp / "bench.lock")
    block = dict(raw["commissioning"])
    block.update(block_overrides)
    raw["commissioning"] = block
    return raw


def write_profile(tmp: Path, raw: dict, name: str = "profile.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_material(tmp: Path, raw: dict) -> Path:
    """One dummy file per planned asset, so --material-dir resolution works."""
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


def fake_preflight(ctx):
    return {"started_at": "2026-11-19 15:00:00", "verdict": "PASS", "ports": [],
            "expected_ports": [], "missing_ports": [], "captured": {}, "versions_before": {},
            "expectations": {}, "assets": {"assets": {}, "all_ok": True}}


def fake_burn(ctx, chip):
    if chip.driver == "hisi-burntool":
        return {"chip": chip.id, "driver": chip.driver, "verdict": "PASS",
                "checks": {"success_tokens": True, "no_failure_tokens": True, "power_receipts": True},
                "markers": {}, "tool": "BurnTool.exe", "tool_sha256": "a" * 64,
                "image": "ws63.fwpkg", "image_sha256": "b" * 64,
                "cmd": ["BurnTool.exe", "-com:32", "-bin:ws63.fwpkg", "-signalbaud:1000000",
                        "-chiptype:13", "-onlyburn:root_loaderboot_sign.bin"],
                "tool_exit": 0, "optlog": "optLog_1.txt", "optlog_sha256": "c" * 64, "optlog_bytes": 100,
                "tool_log": str(Path(ctx["run_dir"]) / "tool_logs" / "tool_hisi.log")}
    return {"chip": chip.id, "driver": chip.driver, "verdict": "PASS",
            "checks": {"exit_zero": True, "markers": True, "write_size": True, "device_md5": True},
            "markers": {marker: True for marker in chip.burn.get("success_markers") or ["ok"]},
            "write_size": WRITE_SIZE, "device_md5": DEVICE_MD5,
            "tool": "tool.exe", "tool_sha256": "a" * 64, "image": "fw.bin", "image_sha256": "b" * 64,
            "image_addr": "0x000000", "burn_port": "COM34", "burn_baud": 1500000,
            "cmd": ["Uart_Burn_Tool.exe", "-b", "1500000", "-p", "COM34", "-f", "fw.bin",
                    "-a", "0x000000"],
            "tool_log": str(Path(ctx["run_dir"]) / "tool_logs" / "tool_csk6.log")}


def fake_verify(ctx, chip):
    version = "V40.1.11"
    return {"started_at": "2026-11-19 15:05:00", "chip": chip.id, "captured": {},
            "versions": {chip.verify["version_key"]: version},
            "expectation": {"chip": chip.id, "version_key": chip.verify["version_key"],
                            "expected": "", "got": version},
            "window_s": 120, "measurement_mode": True,
            "power_cycle": {"verdict": "PASS"},
            "checks": {"version_keys_present": True, "expected_version": None, "power_cycle": "PASS"},
            "verdict": "PASS"}


def fake_receipt(ctx, state):
    return {"verdict": "PASS"}


def run_all(tmp: Path, raw: dict, node: str | None = NODE, extra: list[str] | None = None,
            **patches):
    path = write_profile(tmp, raw)
    material = make_material(tmp, raw)
    argv = ["all", "--profile", str(path), "--run-dir", str(tmp),
            "--material-dir", str(material), "--confirm-write", "--commission", APPROVAL]
    if node:
        argv += ["--node", node]
    argv += extra or []
    with mock.patch.object(cli, "phase_preflight", patches.get("preflight", fake_preflight)), \
            mock.patch.object(cli, "phase_burn", patches.get("burn", fake_burn)), \
            mock.patch.object(cli, "phase_verify", patches.get("verify", fake_verify)), \
            mock.patch.object(cli, "phase_receipt", patches.get("receipt", fake_receipt)), \
            mock.patch.object(cli, "check_run_pins", patches.get("pins", lambda *a, **k: None)):
        return cli.main(argv), path


class CommissioningGuardTests(unittest.TestCase):
    def test_draft_without_commission_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp, "--confirm-write"])
        self.assertEqual(code, 3)

    def test_draft_without_any_block_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw.pop("commissioning")
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_wrong_approval_ref_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", "NOAH-999", "--node", NODE])
        self.assertEqual(code, 3)

    def test_commission_still_needs_confirm_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_commission_needs_the_approved_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            self.assertEqual(cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                                       "--confirm-write", "--commission", APPROVAL]), 3)
            self.assertEqual(cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                                       "--confirm-write", "--commission", APPROVAL,
                                       "--node", "win005"]), 3)

    def test_commission_only_runs_the_whole_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["step", "burn:csk6", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_step_outside_allowed_steps_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp), allowed_steps=["verify:csk6"])
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_write_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp), max_write_steps=1)  # the plan has two burns
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_pinned_burn_port_must_match_the_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["commissioning"]["device"]["burn_ports"]["csk6"] = "COM99"
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_pinned_control_port_must_match_the_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["commissioning"]["device"]["control_port"] = "COM1"
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_pinned_asset_must_be_the_one_the_profile_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["chips"][0]["burn"]["tool"] = "csk-uart-burn-tool"  # 1.0.10, not the pin
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_pinned_sha_must_equal_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["commissioning"]["pins"]["csk6"]["tool"]["sha256"] = "0" * 64
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", APPROVAL, "--node", NODE])
        self.assertEqual(code, 3)

    def test_expired_block_is_refused_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp), expires_at="2020-01-01 00:00:00")
            path = write_profile(Path(tmp), raw)
            with self.assertRaises(ProfileError):
                Profile(path)

    def test_block_on_a_measured_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp))
            raw["status"] = "measured"
            path = write_profile(Path(tmp), raw)
            with self.assertRaises(ProfileError):
                Profile(path)

    def test_block_shape_is_validated(self):
        for bad in ({"approved_by": ""}, {"allowed_steps": []}, {"max_write_steps": 9},
                    {"max_runs": 2}, {"node": ""}, {"device": {}}, {"pins": {}},
                    {"expires_at": "2026-11-20T00:00:00"}):  # time-lint:allow - rejection fixture
            with tempfile.TemporaryDirectory() as tmp:
                path = write_profile(Path(tmp), draft_raw(Path(tmp), **bad))
                with self.assertRaises(ProfileError):
                    Profile(path)

    def test_pin_shape_is_validated(self):
        for bad in ({"pins": {"csk6": {"tool": {}}}},
                    {"device": {"serial": "x", "control_port": "COM1", "burn_ports": {}}}):
            with tempfile.TemporaryDirectory() as tmp:
                path = write_profile(Path(tmp), draft_raw(Path(tmp), **bad))
                with self.assertRaises(ProfileError):
                    Profile(path)


class CommissioningRunTests(unittest.TestCase):
    def _run(self, tmp: Path) -> tuple[Path, Path]:
        code, path = run_all(tmp, draft_raw(tmp))
        self.assertEqual(code, 0)
        return path, tmp / "commissioning.receipt.json"

    def test_run_writes_a_complete_measurement_and_promotes(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path, receipt_path = self._run(tmp)
            measurement = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(measurement["schema"], commission_module.COMMISSION_SCHEMA)
            self.assertTrue(measurement["complete"], measurement["missing"])
            self.assertEqual(measurement["commissioning"]["approval_ref"], APPROVAL)
            self.assertEqual(measurement["node"], NODE)
            self.assertIn("resources", measurement)
            self.assertIn("commands", measurement["control"])

            out = tmp / "csk6-hisi-dev01.measured.json"
            code = cli.main(["promote", "--profile", str(path), "--from", str(receipt_path),
                             "--out", str(out)])
            self.assertEqual(code, 0)
            promoted = Profile(out)
            self.assertTrue(promoted.measured)
            csk6 = promoted.chip("csk6")
            self.assertEqual(csk6.burn["expected_write_bytes"], WRITE_SIZE)
            self.assertEqual(csk6.burn["expected_md5"], DEVICE_MD5)
            self.assertEqual(csk6.verify["expect"], "V40.1.11")
            hisi = promoted.chip("hisi")
            self.assertEqual(hisi.burn["signal_baud"], 1000000)
            self.assertEqual(hisi.verify["expect"], "V40.1.11")
            self.assertNotIn("commissioning", promoted.raw)
            self.assertNotIn("measurement_required", promoted.raw)

            # one shot: the promoted file can no longer be commissioned
            with self.assertRaises(ProfileError):
                Profile(write_profile(tmp, {**promoted.raw,
                                            "commissioning": draft_raw(tmp)["commissioning"]},
                                      "recommission.json"))

    def test_the_same_approval_never_runs_twice(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            self._run(tmp)
            code, _ = run_all(tmp, draft_raw(tmp), extra=["--issue", "NOAH-263"])
        self.assertEqual(code, 3)

    def test_preflight_failure_means_zero_device_action(self):
        def blocked(ctx):
            return {"started_at": "2026-11-19 15:00:00", "verdict": "BLOCKED",
                    "reason": "expected ports missing", "measurement_required": []}

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            calls: list[str] = []

            def tracking_burn(ctx, chip):
                calls.append(chip.id)
                return fake_burn(ctx, chip)

            code, _ = run_all(tmp, draft_raw(tmp), preflight=blocked, burn=tracking_burn)
            self.assertEqual(code, 4)
            self.assertEqual(calls, [])
            self.assertFalse((tmp / "commissioning.receipt.json").is_file())
            # the approval is not spent by a pre-gate failure: no write happened
            self.assertFalse(commission_module.ledger_state(Profile(write_profile(tmp, draft_raw(tmp))),
                                                           NODE).get("write_started"))

    def test_pin_mismatch_refuses_before_the_tool_runs(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            raw = draft_raw(tmp)
            path = write_profile(tmp, raw)
            material = make_material(tmp, raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", str(tmp),
                             "--material-dir", str(material), "--confirm-write",
                             "--commission", APPROVAL, "--node", NODE])
            # the fixture files cannot match the pinned sha256 -> refusal, no run
        self.assertEqual(code, 3)
        self.assertFalse((Path(tmp_str) / "steps" / "burn-csk6.json").exists())

    def test_incomplete_measurement_refuses_to_flip_status(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path, receipt_path = self._run(tmp)
            measurement = json.loads(receipt_path.read_text(encoding="utf-8"))
            for burn in measurement["burns"]:
                burn["device_md5"] = None
            receipt_path.write_text(json.dumps(measurement, ensure_ascii=False), encoding="utf-8")
            out = tmp / "measured.json"
            code = cli.main(["promote", "--profile", str(path), "--from", str(receipt_path),
                             "--out", str(out)])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())

    def test_measurement_from_another_profile_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path, receipt_path = self._run(tmp)
            other_raw = draft_raw(tmp)
            other_raw["board"] = "another bench with the same name"
            other = write_profile(tmp, other_raw, "other-profile.json")
            out = tmp / "measured.json"
            code = cli.main(["promote", "--profile", str(other), "--from", str(receipt_path),
                             "--out", str(out)])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())

    def test_measurement_from_another_node_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path, receipt_path = self._run(tmp)
            measurement = json.loads(receipt_path.read_text(encoding="utf-8"))
            measurement["node"] = "win005"
            receipt_path.write_text(json.dumps(measurement, ensure_ascii=False), encoding="utf-8")
            out = tmp / "measured.json"
            code = cli.main(["promote", "--profile", str(path), "--from", str(receipt_path),
                             "--out", str(out)])
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())


class StrapTimingTests(unittest.TestCase):
    def _measured(self, boot_required: bool) -> dict:
        raw = draft_raw(Path("/tmp"))
        raw.pop("commissioning")
        raw["status"] = "measured"
        raw["verified"] = {"on": "2026-11-19", "by": "unit-test", "evidence": "n/a"}
        raw["control"]["timing"] = {"cycle_hold_s": 3.0}
        for chip in raw["chips"]:
            chip["boot"] = {"required": boot_required, "method": None}
            chip["burn"]["image_addr"] = "0x0"
            chip["verify"].update({"expect": "V40.1.11", "window_s": 120})
        raw["chips"][0]["burn"].update({"expected_write_bytes": WRITE_SIZE, "expected_md5": DEVICE_MD5})
        raw["chips"][1]["burn"].update({"signal_baud": 1000000, "onlyburn_parts": ["root.bin"]})
        return raw

    def test_no_strap_chips_need_only_the_cycle_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), self._measured(False))
            self.assertTrue(Profile(path).measured)

    def test_strap_chip_still_requires_the_strap_timings(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._measured(True)
            path = write_profile(Path(tmp), raw)
            with self.assertRaises(ProfileError):
                Profile(path)


if __name__ == "__main__":
    unittest.main()

"""Commissioning: the audited first-run path from ``draft`` to ``measured``.

No hardware: the phase functions are replaced by fakes that emit receipts with
the same shape a real run writes, and the promote step re-reads them. The point
of these tests is that the circle (no write until measured / no measurement
without a write) is closed *only* through an explicit approval block, and that
a status flip without the graded values is refused.
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
from core.profile import Profile, ProfileError  # noqa: E402

DEV01 = ROOT / "scripts" / "profiles" / "csk6-hisi-dev01.json"
WRITE_SIZE = 5685248
DEVICE_MD5 = "0xf6d6369435d52784efe1119ebd08f582"


def draft_raw(tmp: Path, **block_overrides) -> dict:
    raw = json.loads(DEV01.read_text(encoding="utf-8"))
    raw["lock_path"] = str(tmp / "bench.lock")
    block = {"approved_by": "郑保山", "approval_ref": "NOAH-259",
             "allowed_steps": ["burn:csk6", "verify:csk6", "burn:hisi", "verify:hisi"],
             "max_write_steps": 2,
             "expires_at": "2999-01-01 00:00:00"}
    block.update(block_overrides)
    raw["commissioning"] = block
    for chip in raw["chips"]:
        chip["burn"]["tool"] = "csk-uart-burn-tool" if chip["id"] == "csk6" else "hisi-burntool-gold"
        chip["burn"]["image"] = ("tcl-csk7-v0.0.41" if chip["id"] == "csk6"
                                 else "hisi-ws63-v8-h363t20-wfmv206.111.1.16")
    return raw


def write_profile(tmp: Path, raw: dict, name: str = "profile.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def fake_preflight(ctx):
    return {"started_at": "2026-11-19 15:00:00", "verdict": "PASS", "ports": [],
            "expected_ports": [], "missing_ports": [], "captured": {}, "versions_before": {},
            "expectations": {}, "assets": {"assets": {}, "all_ok": True}}


def fake_burn(ctx, chip):
    return {"chip": chip.id, "driver": chip.driver, "verdict": "PASS",
            "checks": {"exit_zero": True, "markers": True, "write_size": True, "device_md5": True},
            "markers": {marker: True for marker in chip.burn.get("success_markers") or ["ok"]},
            "write_size": WRITE_SIZE, "device_md5": DEVICE_MD5,
            "tool": "tool.exe", "tool_sha256": "a" * 64, "image": "fw.bin", "image_sha256": "b" * 64,
            "image_addr": "0x0", "burn_port": "COM34" if chip.id == "csk6" else "COM32",
            "burn_baud": 3000000,
            "cmd": (["Uart_Burn_Tool.exe", "-b", "3000000", "-p", "COM34", "-f", "fw.bin", "-a", "0x0"]
                    if chip.id == "csk6" else
                    ["BurnTool.exe", "-com:32", "-bin:fw.bin", "-signalbaud:1000000",
                     "-chiptype:13", "-onlyburn:root_loaderboot_sign.bin"]),
            "tool_log": str(Path(ctx["run_dir"]) / "tool_logs" / f"tool_{chip.id}.log")}


def fake_verify(ctx, chip):
    version = "V40.1.11"
    return {"started_at": "2026-11-19 15:05:00", "chip": chip.id, "captured": {},
            "versions": {chip.verify["version_key"]: version},
            "expectation": {"chip": chip.id, "version_key": chip.verify["version_key"],
                            "expected": "", "got": version},
            "window_s": 120, "measurement_mode": True,
            "checks": {"version_keys_present": True, "expected_version": None, "power_cycle": "PASS"},
            "verdict": "PASS"}


def fake_receipt(ctx, state):
    return {"verdict": "PASS"}


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
                             "--confirm-write", "--commission", "NOAH-259"])
        self.assertEqual(code, 3)

    def test_wrong_approval_ref_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", "NOAH-999"])
        self.assertEqual(code, 3)

    def test_commission_still_needs_confirm_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), draft_raw(Path(tmp)))
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--commission", "NOAH-259"])
        self.assertEqual(code, 3)

    def test_step_outside_allowed_steps_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp), allowed_steps=["verify:csk6"])
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", "NOAH-259"])
        self.assertEqual(code, 3)

    def test_write_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = draft_raw(Path(tmp), max_write_steps=1)  # the plan has two burns
            path = write_profile(Path(tmp), raw)
            code = cli.main(["all", "--profile", str(path), "--run-dir", tmp,
                             "--confirm-write", "--commission", "NOAH-259"])
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
                    {"expires_at": "2026-11-20T00:00:00"}):  # time-lint:allow - rejection fixture
            with tempfile.TemporaryDirectory() as tmp:
                path = write_profile(Path(tmp), draft_raw(Path(tmp), **bad))
                with self.assertRaises(ProfileError):
                    Profile(path)


class CommissioningRunTests(unittest.TestCase):
    def _run(self, tmp: Path) -> tuple[Path, Path]:
        path = write_profile(tmp, draft_raw(tmp))
        with mock.patch.object(cli, "phase_preflight", fake_preflight), \
                mock.patch.object(cli, "phase_burn", fake_burn), \
                mock.patch.object(cli, "phase_verify", fake_verify), \
                mock.patch.object(cli, "phase_receipt", fake_receipt):
            code = cli.main(["all", "--profile", str(path), "--run-dir", str(tmp),
                             "--confirm-write", "--commission", "NOAH-259"])
        self.assertEqual(code, 0)
        return path, tmp / "commissioning.receipt.json"

    def test_run_writes_a_complete_measurement_and_promotes(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            path, receipt_path = self._run(tmp)
            measurement = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(measurement["schema"], commission_module.COMMISSION_SCHEMA)
            self.assertTrue(measurement["complete"], measurement["missing"])
            self.assertEqual(measurement["commissioning"]["approval_ref"], "NOAH-259")

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
            self.assertNotIn("commissioning", promoted.raw)
            self.assertNotIn("measurement_required", promoted.raw)

            # one shot: the promoted file can no longer be commissioned
            with self.assertRaises(ProfileError):
                Profile(write_profile(tmp, {**promoted.raw,
                                            "commissioning": draft_raw(tmp)["commissioning"]},
                                      "recommission.json"))

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


class StrapTimingTests(unittest.TestCase):
    def _measured(self, boot_required: bool) -> dict:
        raw = draft_raw(Path("/tmp"))
        raw.pop("commissioning")
        raw["status"] = "measured"
        raw["verified"] = {"on": "2026-11-19", "by": "unit-test", "evidence": "n/a"}
        raw["control"]["timing"] = {"cycle_hold_s": 3.0}
        for chip in raw["chips"]:
            chip["boot"] = {"required": boot_required, "method": None}
            chip["transport"]["burn_baud"] = 3000000
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

"""CSK6 (`csk6-uart`) driver, dev01 profile and index registration tests.

No hardware, no network: the tool output is a fixture and every port stays
inside a temporary directory.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core import phases as phases_module  # noqa: E402
from core.bench import BenchError  # noqa: E402
from core.contract import parse_versions  # noqa: E402
from core.phases import version_matches  # noqa: E402
from core.profile import Profile, ProfileError  # noqa: E402
from core.state import plan_steps  # noqa: E402

DEV01 = ROOT / "scripts" / "profiles" / "csk6-hisi-dev01.json"
ASSETS = ROOT / "assets" / "manifest.json"
INDEX = ROOT / "index" / "index.json"
MANIFEST = ROOT / "index" / "manifest.json"

FW_SHA256 = "b70eca75db330e801ef6c66fdac33f645e687501a92a48dd7d83b96a69563154"
FW_BYTES = 38827193

#: Uart_Burn_Tool log shape recorded in kb-knowledge-368ec0d51d2c (V4.1.8 run).
TOOL_OUTPUT = "\n".join([
    "Uart Burn Tool  Version: 1.0.5",
    "OPEN PORT COM34 SUCCESS",
    "CONNECT WITH CHIP SUCCESS",
    "CHIP TYPE = 0x60120001",
    "WRITE SIZE 5685248",
    "MD5 CHECK SUCCESS",
    "DEVICE MD5 = 0xf6d6369435d52784efe1119ebd08f582",
    "FLASH 0x0 SECTOR DOWNLOAD SUCCESS",
])

BURN_SPEC = {
    "success_markers": [
        "CONNECT WITH CHIP SUCCESS",
        "CHIP TYPE = 0x60120001",
        "MD5 CHECK SUCCESS",
        "FLASH 0x0 SECTOR DOWNLOAD SUCCESS",
    ],
    "expected_write_bytes": 5685248,
    "expected_md5": "0xf6d6369435d52784efe1119ebd08f582",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def draft_profile() -> Profile:
    return Profile(DEV01)


def write_variant(raw: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(raw, tmp, ensure_ascii=False)
    tmp.close()
    return Path(tmp.name)


def measured_raw() -> dict:
    """Turn the real draft profile into a complete measured one (fixture only)."""
    raw = load(DEV01)
    raw["status"] = "measured"
    raw["verified"] = {"on": "2026-11-19", "by": "unit-test fixture", "evidence": "n/a"}
    raw["control"]["timing"] = {"cut_to_strap_s": 3.0, "strap_to_restore_s": 1.0,
                                "restore_to_strap_off_s": 8.0, "cycle_hold_s": 3.0}
    csk6 = raw["chips"][0]
    csk6["transport"]["burn_baud"] = 3000000
    csk6["burn"].update({"tool": "csk-uart-burn-tool", "image": "tcl-csk7-v0.0.41",
                         "image_addr": "0x0"})
    csk6["burn"].update(BURN_SPEC)
    csk6["verify"].update({"expect": "V4.1.8", "window_s": 60})
    hisi = raw["chips"][1]
    hisi["transport"]["burn_baud"] = 1000000
    hisi["burn"].update({"image": "hisi-ws63-v8-h363t20-wfmv206.111.1.16",
                         "signal_baud": 1000000,
                         "onlyburn_parts": ["root_loaderboot_sign.bin"]})
    hisi["verify"].update({"expect": "V8-H363T20-WFMV206.111.1.16", "window_s": 40})
    return raw


class Csk6EvidenceTests(unittest.TestCase):
    def test_full_evidence_passes(self):
        report = phases_module.csk6_burn_checks(BURN_SPEC, TOOL_OUTPUT, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["write_size"], 5685248)
        self.assertEqual(report["device_md5"], "0xf6d6369435d52784efe1119ebd08f582")
        self.assertEqual(set(report["checks"].values()), {True})

    def test_parse_tolerates_thousands_separator_and_colon(self):
        self.assertEqual(phases_module.parse_write_size("WRITE SIZE: 5,685,248"), 5685248)
        self.assertEqual(phases_module.parse_write_size("write size = 5685248"), 5685248)
        self.assertIsNone(phases_module.parse_write_size("no numbers here"))

    def test_missing_evidence_is_not_a_pass(self):
        self.assertIsNone(phases_module.parse_device_md5("MD5 CHECK SUCCESS"))
        self.assertIsNone(phases_module.parse_write_size(""))

    def test_write_size_mismatch_fails(self):
        spec = dict(BURN_SPEC, expected_write_bytes=5685249)
        report = phases_module.csk6_burn_checks(spec, TOOL_OUTPUT, 0)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["write_size"])
        self.assertTrue(report["checks"]["device_md5"])

    def test_md5_mismatch_fails(self):
        spec = dict(BURN_SPEC, expected_md5="0x" + "0" * 32)
        report = phases_module.csk6_burn_checks(spec, TOOL_OUTPUT, 0)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["device_md5"])

    def test_missing_marker_fails(self):
        spec = dict(BURN_SPEC, success_markers=BURN_SPEC["success_markers"] + ["FLASH DOWNLOAD SUCCESS"])
        report = phases_module.csk6_burn_checks(spec, TOOL_OUTPUT, 0)
        self.assertFalse(report["ok"])
        self.assertFalse(report["markers"]["FLASH DOWNLOAD SUCCESS"])

    def test_non_zero_exit_fails(self):
        report = phases_module.csk6_burn_checks(BURN_SPEC, TOOL_OUTPUT, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["exit_zero"])


class Csk6ArgvTests(unittest.TestCase):
    def test_default_argv_uses_profile_transport(self):
        profile = draft_profile()
        chip = profile.chip("csk6")
        chip.transport["burn_baud"] = 3000000
        chip.burn["image_addr"] = "0x0"
        argv = phases_module.csk6_argv(Path("Uart_Burn_Tool.exe"), chip, Path("fw.bin"))
        self.assertEqual(argv, ["Uart_Burn_Tool.exe", "-b", "3000000", "-p", "COM34",
                                "-f", "fw.bin", "-a", "0x0"])

    def test_profile_can_override_the_argv_template(self):
        profile = draft_profile()
        chip = profile.chip("csk6")
        chip.transport["burn_baud"] = 3000000
        chip.burn["argv_template"] = ["--port", "{port}", "--image", "{image}", "--baud", "{baud}"]
        argv = phases_module.csk6_argv(Path("tool.exe"), chip, Path("fw.bin"))
        self.assertEqual(argv, ["tool.exe", "--port", "COM34", "--image", "fw.bin", "--baud", "3000000"])

    def test_unknown_placeholder_is_a_bench_error(self):
        profile = draft_profile()
        chip = profile.chip("csk6")
        chip.burn["argv_template"] = ["--serial", "{serial}"]
        with self.assertRaises(BenchError):
            phases_module.csk6_argv(Path("tool.exe"), chip, Path("fw.bin"))


class Dev01ProfileTests(unittest.TestCase):
    def test_profile_is_a_draft_with_both_chips(self):
        profile = draft_profile()
        self.assertEqual(profile.name, "csk6-hisi-dev01")
        self.assertEqual(profile.family, "generic")
        self.assertEqual(profile.status, "draft")
        self.assertFalse(profile.measured)
        self.assertEqual([chip.id for chip in profile.chips], ["csk6", "hisi"])
        self.assertEqual(profile.chip("csk6").driver, "csk6-uart")
        self.assertEqual(profile.chip("hisi").driver, "hisi-burntool")
        self.assertEqual(profile.chip("csk6").package_asset, "csk6-v40.1.11-package")

    def test_plan_order_has_no_boot_or_recover(self):
        self.assertEqual(plan_steps(draft_profile()),
                         ["preflight", "burn:csk6", "verify:csk6", "burn:hisi", "verify:hisi", "receipt"])

    def test_port_map_is_per_node_and_forbids_reuse(self):
        raw = load(DEV01)
        reference = raw["port_map_reference"]
        self.assertIn("禁止复用", reference["note"])
        self.assertEqual(reference["win003"]["control"], "COM36")
        self.assertEqual(reference["win003"]["csk6:burn"], "COM34")
        self.assertEqual(reference["win005"]["control"], "COM15")
        self.assertEqual(reference["win005"]["csk6:burn"], "COM37")
        self.assertNotEqual(reference["win003"]["csk6:burn"], reference["win005"]["csk6:burn"])
        self.assertIn("kb-knowledge-99ced0adc4e3", reference["source"])

    def test_unmeasured_values_are_declared_not_guessed(self):
        raw = load(DEV01)
        csk6 = raw["chips"][0]
        self.assertIsNone(csk6["burn"]["expected_write_bytes"])
        self.assertIsNone(csk6["burn"]["expected_md5"])
        self.assertIsNone(csk6["burn"]["tool"])
        self.assertIsNone(csk6["verify"]["expect"])
        required = " ".join(raw["measurement_required"])
        for token in ("port-map", "expected_write_bytes", "expected_md5", "Uart_Burn_Tool", "V40.1.11"):
            self.assertIn(token, required)

    def test_write_steps_are_refused_on_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["all", "--profile", str(DEV01), "--run-dir", tmp,
                                       "--confirm-write"]), 3)
            self.assertEqual(cli.main(["step", "burn:csk6", "--profile", str(DEV01),
                                       "--run-dir", tmp, "--confirm-write"]), 3)

    def test_preflight_is_blocked_without_touching_serial(self):
        def no_serial():
            raise AssertionError("draft preflight must not open a port")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phases_module, "serial_module", no_serial):
                code = cli.main(["preflight", "--profile", str(DEV01), "--run-dir", tmp])
            receipt = json.loads((Path(tmp) / "steps" / "preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 4)
        self.assertEqual(receipt["verdict"], "BLOCKED")
        self.assertTrue(receipt["measurement_required"])

    def test_plan_renders_for_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "--profile", str(DEV01), "--run-dir", tmp])
        self.assertEqual(code, 0)
        self.assertIn("csk6-uart", buffer.getvalue())
        self.assertIn("burn:csk6", buffer.getvalue())

    def test_measured_profile_requires_the_csk6_acceptance_values(self):
        for field in ("expected_write_bytes", "expected_md5", "success_markers", "image_addr"):
            raw = measured_raw()
            raw["chips"][0]["burn"][field] = None
            path = write_variant(raw)
            try:
                with self.assertRaises(ProfileError) as ctx:
                    Profile(path)
                self.assertIn(field, str(ctx.exception))
            finally:
                path.unlink()

    def test_complete_measured_profile_is_accepted(self):
        path = write_variant(measured_raw())
        try:
            profile = Profile(path)
        finally:
            path.unlink()
        self.assertTrue(profile.measured)
        self.assertEqual(profile.chip("csk6").driver, "csk6-uart")


class Csk6BurnPhaseTests(unittest.TestCase):
    """The whole burn phase with a mocked tool: proves dispatch + receipt shape."""

    def _run(self, output: str, rc: int) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "Uart_Burn_Tool.exe"
            tool.write_bytes(b"tool-bytes")
            image = Path(tmp) / "fw.bin"
            image.write_bytes(b"image-bytes")
            profile_path = write_variant(measured_raw())
            try:
                profile = Profile(profile_path)
            finally:
                profile_path.unlink()
            chip = profile.chip("csk6")

            class FakeAssets:
                def resolve(self, asset_id, material_dir, cache_dir):
                    return tool if asset_id == chip.tool_asset else image

            ctx = {"profile": profile, "run_dir": tmp, "assets": FakeAssets(),
                   "material_dir": None, "cache_dir": None}
            canned = {"cmd": ["Uart_Burn_Tool.exe"], "rc": rc, "elapsed_s": 1.5, "output": output}
            with mock.patch.object(phases_module, "run_tool", lambda *a, **k: canned):
                receipt = phases_module.phase_burn(ctx, chip)
            log = Path(receipt["tool_log"])
            self.assertTrue(log.is_file(), "the raw tool log must be kept as evidence")
        return receipt, ctx

    def test_dispatch_writes_a_pass_receipt(self):
        receipt, _ = self._run(TOOL_OUTPUT, 0)
        self.assertEqual(receipt["driver"], "csk6-uart")
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["write_size"], 5685248)
        self.assertEqual(receipt["image_addr"], "0x0")
        self.assertTrue(all(receipt["checks"].values()))

    def test_missing_write_size_fails_the_phase(self):
        receipt, _ = self._run(TOOL_OUTPUT.replace("WRITE SIZE 5685248", "WRITE SIZE ?"), 0)
        self.assertEqual(receipt["verdict"], "FAIL")
        self.assertIn("write_size", receipt["reason"])


class Csk6VersionTests(unittest.TestCase):
    def test_csk6_app_line_is_parsed_with_the_leading_v(self):
        versions = parse_versions([
            "IDE: VenusA-DSP",
            "AP Boot Ready !!!",
            "venus ap appliction(V4.1.8@Nov 11 2026 12:27:56)",
        ])
        self.assertEqual(versions["csk6_app"], "V4.1.8")
        self.assertTrue(version_matches("V4.1.8", "V4.1.8"))
        self.assertFalse(version_matches("V4.1.8", "V40.1.11"))


class Csk6IndexTests(unittest.TestCase):
    def test_capability_and_workflow_exist_as_draft(self):
        index = load(INDEX)
        capabilities = {item["capability_id"]: item for item in index["capabilities"]}
        capability = capabilities["burn.csk6-hisi-dev01"]
        self.assertEqual(capability["status"], "draft")
        self.assertEqual(capability["profile"], "csk6-hisi-dev01")
        self.assertEqual(capability["workflow"], "wf.burn.csk6-hisi-dev01")
        self.assertEqual(capability["evidence_schema"], "burner-receipt/v1")
        workflows = {item["workflow_id"]: item for item in index["workflows"]}
        workflow = workflows["wf.burn.csk6-hisi-dev01"]
        self.assertEqual(workflow["steps"], plan_steps(draft_profile()))

    def test_workflow_has_the_seven_independent_items(self):
        index = load(INDEX)
        workflow = next(item for item in index["workflows"]
                        if item["workflow_id"] == "wf.burn.csk6-hisi-dev01")
        self.assertEqual([item["n"] for item in workflow["judgement_items"]], [1, 2, 3, 4, 5, 6, 7])
        self.assertIn("kb-method-cad7be93134e", workflow["verdict_rule"])

    def test_project_and_chip_entries_point_at_the_profile(self):
        index = load(INDEX)
        project = next(item for item in index["projects"] if item["project_id"] == "csk6-hisi-dev01")
        self.assertEqual(project["profile_status"], "draft")
        self.assertIn("burn.csk6-hisi-dev01", project["capabilities"])
        chip = next(item for item in index["chips"] if item["chip_id"] == "csk6-uart")
        self.assertEqual(chip["driver"], "csk6-uart")
        self.assertIn("csk6-hisi-dev01", chip["profiles"])

    def test_every_capability_points_at_a_real_workflow(self):
        index = load(INDEX)
        workflows = {item["workflow_id"] for item in index["workflows"]}
        for capability in index["capabilities"]:
            self.assertIn(capability["workflow"], workflows, capability["capability_id"])

    def test_firmware_package_is_registered_with_measured_bytes(self):
        index = load(INDEX)
        firmware = next(item for item in index["firmware_profiles"]
                        if item["firmware_id"] == "csk6-v40.1.11-package")
        self.assertEqual(firmware["bytes"], FW_BYTES)
        self.assertEqual(firmware["sha256"], FW_SHA256)
        self.assertEqual(firmware["file"], "V40.1.11_20261119094552.7z")
        assets = {item["id"]: item for item in load(ASSETS)["assets"]}
        asset = assets["csk6-v40.1.11-package"]
        self.assertEqual(asset["bytes"], FW_BYTES)
        self.assertEqual(asset["sha256"], FW_SHA256)
        self.assertEqual(asset["provenance"]["source"], "node-attachment")

    def test_unavailable_package_is_never_downloadable(self):
        assets = {item["id"]: item for item in load(ASSETS)["assets"]}
        asset = assets["csk6-v40.1.11-package"]
        self.assertEqual(asset["oss"]["download"], "local-only")
        self.assertTrue(asset["oss"]["pending_upload"])
        self.assertNotIn("oss://", json.dumps(asset))

    def test_index_and_manifest_share_the_skill_version(self):
        self.assertEqual(load(INDEX)["skill_version"], "1.3.0")
        self.assertEqual(load(MANIFEST)["version"], "1.3.0")


if __name__ == "__main__":
    unittest.main()

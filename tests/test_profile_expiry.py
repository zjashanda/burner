"""A lapsed approval must never make the repository unreadable.

Why this file exists: v1.5.0 shipped ``csk6-hisi-dev01.json`` with
``expires_at=2026-11-21`` and enforced expiry at *profile load*. The moment the
date rolled over, 32 unit tests and ``scripts/lint_scan.py`` went red with
"expires_at ... has passed", even though nothing was wrong with the code.

Expiry is a **write** gate: the profile still loads, ``plan`` renders the window
and says it lapsed, and only a write step is refused. These tests pin that down
and make the suite independent of today's date.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core import commission as commission_module  # noqa: E402
from core.profile import Profile  # noqa: E402

PROFILES = sorted((ROOT / "scripts" / "profiles").glob("*.json"))
LEGACY = ROOT / "scripts" / "profiles" / "csk6-hisi-dev01.json"
WIN003 = ROOT / "scripts" / "profiles" / "csk6-hisi-win003.json"
WIN005 = ROOT / "scripts" / "profiles" / "csk6-hisi-win005.json"
LAPSED = "2020-01-01 00:00:00"


def lapsed_copy(tmp: Path, source: Path) -> Path:
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["lock_path"] = str(tmp / "bench.lock")
    raw["commissioning"]["expires_at"] = LAPSED
    path = tmp / "lapsed.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class ProfileReadabilityTests(unittest.TestCase):
    def test_every_shipped_profile_loads_whatever_the_date(self):
        self.assertTrue(PROFILES, "no profiles found")
        for path in PROFILES:
            with self.subTest(profile=path.name):
                Profile(path)  # must not raise after an approval window lapses

    def test_the_shipped_approvals_declare_a_window(self):
        for path in (LEGACY, WIN003, WIN005):
            block = Profile(path).commissioning
            self.assertRegex(block["expires_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertIn("expires_at", commission_module.expiry_state(block))

    def test_expiry_state_is_a_boolean_about_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Profile(lapsed_copy(Path(tmp), LEGACY))
        window = commission_module.expiry_state(profile.commissioning)
        self.assertTrue(window["expired"])
        self.assertEqual(window["expires_at"], LAPSED)


class ExpiredApprovalIsAWriteGateTests(unittest.TestCase):
    def test_a_lapsed_profile_still_renders_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = lapsed_copy(Path(tmp), WIN003)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "--profile", str(path), "--run-dir", tmp, "--bench", "dev02"])
            text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("EXPIRED", text)
        self.assertIn("burn:csk6", text)
        self.assertIn("NOAH-267-win003-dev02", text)

    def test_a_lapsed_approval_refuses_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = lapsed_copy(tmp_path, WIN003)
            code = cli.main(["all", "--profile", str(path), "--run-dir", str(tmp_path),
                             "--bench", "dev02", "--commission", "NOAH-267-win003-dev02",
                             "--node", "win003", "--confirm-write"])
            steps = list((tmp_path / "steps").glob("*.json")) if (tmp_path / "steps").is_dir() else []
            ledger = list(tmp_path.glob("*commissioning.json"))
        self.assertEqual(code, 3)
        self.assertEqual(steps, [], "an expired approval must not produce a step receipt")
        self.assertEqual(ledger, [], "an expired approval must not spend the bench ledger")

    def test_a_lapsed_single_device_approval_refuses_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = lapsed_copy(tmp_path, LEGACY)
            code = cli.main(["all", "--profile", str(path), "--run-dir", str(tmp_path),
                             "--commission", "NOAH-266", "--node", "win003", "--confirm-write"])
        self.assertEqual(code, 3)

    def test_check_expiry_names_the_bench_and_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Profile(lapsed_copy(Path(tmp), WIN003))
        bench = commission_module.select_bench(profile.commissioning, "dev02")
        with self.assertRaises(commission_module.CommissionError) as ctx:
            commission_module.check_expiry(profile.commissioning, bench)
        message = str(ctx.exception)
        self.assertIn(LAPSED, message)
        self.assertIn("dev02", message)


if __name__ == "__main__":
    unittest.main()

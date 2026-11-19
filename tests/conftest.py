"""Hard guarantee: this suite never opens a serial port.

The unit tests run on the same machines that drive real benches. NOAH-266
showed what happens when they don't: a preflight-failure test reached
``safe_recover`` and sent ``uut-switch1.off`` twice to the live relay. Tests
fake the phases, but a future test could still hit a real port by accident, so
serial access is blocked process-wide for the whole session here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _no_serial(*_args, **_kwargs):
    raise AssertionError("unit tests must never open a serial port (zero device action)")


@pytest.fixture(autouse=True)
def block_serial(monkeypatch):
    from core import bench, phases

    monkeypatch.setattr(bench, "serial_module", _no_serial)
    monkeypatch.setattr(phases, "serial_module", _no_serial)
    yield

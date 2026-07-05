"""Regression: the device accepts UI schema_versions it doesn't interpret.

The host never renders ui.json widgets (the phone does, gating via
min_app_version); it serves the file opaquely. So validate_manifest must accept
a schema_version newer than any the plugin has seen — rejecting only a malformed
value. A schema_version=2 cartridge (the dpad widget) was rejected with
"unsupported schema_version: 2" before this was fixed, which blocked every
dpad-based cartridge from installing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pwnagotchi-plugin", "src"))

import apps  # noqa: E402


def test_accepts_current_and_future_ui_schema_versions():
    for v in (1, 2, 3, 99):
        ok, reason = apps.validate_manifest({"name": "x", "schema_version": v}, "x")
        assert ok, f"schema_version {v} should be accepted, got: {reason}"


def test_missing_schema_version_defaults_ok():
    ok, _ = apps.validate_manifest({"name": "x"}, "x")
    assert ok


def test_rejects_malformed_schema_version():
    for bad in (0, -1, "2", 2.0, True, False):
        ok, _ = apps.validate_manifest({"name": "x", "schema_version": bad}, "x")
        assert not ok, f"schema_version {bad!r} should be rejected"

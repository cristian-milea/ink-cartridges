"""Tests for the over-BT plugin self-update (controls.plugin.update) and the
plugin_version surfaced in `status`.

The self-update overwrites the host plugin's own file and restarts pwnagotchi
over the only comms channel, so the safety invariants matter: a bad transfer
(sha mismatch, non-UTF-8, syntax error, oversize) must be rejected BEFORE the
live file is touched, and a good one must write atomically + keep a .bak.
"""
import base64
import hashlib
import importlib.util
import os
import sys
import time
import types

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "pwnagotchi-plugin", "src")
_BUNDLE = os.path.join(os.path.dirname(__file__), "..", "pwnagotchi-plugin", "ink-cartridge.py")
sys.path.insert(0, _SRC)

import controls  # noqa: E402  (stdlib-only imports, loads clean)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _update(target, restart_delay=0.01):
    """A plugin.update handler writing to `target`, with a recording run stub."""
    calls = []
    handlers = controls.make_handlers(
        run=lambda cmd: calls.append(cmd) or "",
        plugin_path=str(target),
        restart_delay=restart_delay,
    )
    return handlers["plugin.update"], calls


def test_valid_update_writes_backs_up_and_restarts(tmp_path):
    target = tmp_path / "ink-cartridge.py"
    target.write_text("# old bundle\n")
    update, calls = _update(target)

    new = "x = 1  # new bundle\n"
    res = update({"source_b64": _b64(new), "sha256": _sha(new), "version": "0.3.0"})

    assert res["ok"] and res["restarting"] and res["version"] == "0.3.0"
    assert res["sha256"] == _sha(new)
    assert target.read_text() == new
    assert (tmp_path / "ink-cartridge.py.bak").read_text() == "# old bundle\n"

    # Restart fires on a background thread after the ack.
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [["systemctl", "restart", "pwnagotchi"]]


def test_rejects_sha_mismatch_without_touching_file(tmp_path):
    target = tmp_path / "p.py"
    target.write_text("orig\n")
    update, calls = _update(target)
    with pytest.raises(ValueError):
        update({"source_b64": _b64("new\n"), "sha256": "deadbeef"})
    assert target.read_text() == "orig\n"
    assert not (tmp_path / "p.py.bak").exists()
    assert calls == []


def test_rejects_bad_syntax_without_touching_file(tmp_path):
    target = tmp_path / "p.py"
    target.write_text("orig\n")
    update, _ = _update(target)
    bad = "def (:\n"
    with pytest.raises(SyntaxError):
        update({"source_b64": _b64(bad), "sha256": _sha(bad)})
    assert target.read_text() == "orig\n"
    assert not (tmp_path / "p.py.bak").exists()


def test_rejects_oversize_without_touching_file(tmp_path):
    target = tmp_path / "p.py"
    target.write_text("orig\n")
    update, _ = _update(target)
    big = "x" * (controls.MAX_PLUGIN_BYTES + 1)
    with pytest.raises(ValueError):
        update({"source_b64": _b64(big)})
    assert target.read_text() == "orig\n"


def test_rejects_missing_source(tmp_path):
    target = tmp_path / "p.py"
    target.write_text("orig\n")
    update, _ = _update(target)
    with pytest.raises(ValueError):
        update({})
    assert target.read_text() == "orig\n"


# --- bundle-level: the deployed artifact reports its version + has the command -

def _load_bundle():
    for m in ("pwnagotchi", "pwnagotchi.plugins", "pwnagotchi.utils"):
        sys.modules.setdefault(m, types.ModuleType(m))
    sys.modules["pwnagotchi.plugins"].Plugin = type("P", (), {})
    sys.modules["pwnagotchi"].utils = sys.modules["pwnagotchi.utils"]
    sys.modules["pwnagotchi.utils"].total_unique_handshakes = lambda p: 0
    spec = importlib.util.spec_from_file_location("ic_bundle", _BUNDLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_status_reports_plugin_version():
    mod = _load_bundle()
    reg = mod.build_registry(lambda: None, lambda: None, mod.AppsRuntime(),
                             get_options=lambda: {}, toggle_plugin=lambda n, e: e)
    assert reg.has("plugin.update")
    env = reg.dispatch("status", {})
    assert env["ok"]
    assert env["data"]["plugin_version"] == "0.2.0"
    assert sys.modules["_ic_plugin"].PLUGIN_VERSION == "0.2.0"

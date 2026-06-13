"""Stock pwnagotchi device controls, exposed as commands."""
from __future__ import annotations
import subprocess


def _default_run(cmd: list) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout


def make_handlers(run=None, toggle_plugin=None):
    run = run or _default_run

    def reboot(_a):
        import pwnagotchi
        pwnagotchi.reboot()
        return {"ok": True}

    def shutdown(_a):
        import pwnagotchi
        pwnagotchi.shutdown()
        return {"ok": True}

    def restart(_a):
        run(["systemctl", "restart", "pwnagotchi"])
        return {"ok": True}

    def plugin_toggle(a):
        if toggle_plugin is None:
            raise RuntimeError("plugin toggle not wired")
        return {"enabled": toggle_plugin(a["name"], bool(a.get("enabled", True)))}

    return {
        "reboot": reboot,
        "shutdown": shutdown,
        "restart": restart,
        "plugin.toggle": plugin_toggle,
    }

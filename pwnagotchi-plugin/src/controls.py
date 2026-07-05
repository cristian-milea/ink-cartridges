"""Stock pwnagotchi device controls, exposed as commands."""
from __future__ import annotations
import base64
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

# Cap on a pushed plugin bundle (512 KB). The real bundle is ~100 KB; this
# catches a truncated/garbage transfer before we touch the live plugin file.
MAX_PLUGIN_BYTES = 512 * 1024

# Seconds to wait after acking a plugin.update before restarting pwnagotchi, so
# the reply flushes over the (possibly slow, chunked) BT link before the process
# dies. The client expects the link to drop here and reconnects afterward.
RESTART_DELAY_S = 2.0


def _default_run(cmd: list) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout


def make_handlers(run=None, toggle_plugin=None, plugin_path=None,
                  restart_delay=RESTART_DELAY_S):
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

    def plugin_update(a):
        """Overwrite this plugin's own bundle with a client-pushed one, then
        restart pwnagotchi so it reloads. Same trust model as `install` (the
        phone already uploads root-run Python over BT); this just targets the
        plugin file itself. Validates BEFORE touching the live file: sha256,
        size, decodes as UTF-8, and compiles. Writes atomically (temp + rename)
        and keeps a `.bak` for manual recovery if the new bundle misbehaves.
        """
        # Resolve at call time: in the bundle every module's __file__ is the
        # deployed plugin path; tests inject an explicit plugin_path.
        target = plugin_path or os.path.abspath(__file__)

        src_b64 = a.get("source_b64") or a.get("b64")
        if not src_b64:
            raise ValueError("missing source_b64")
        raw = base64.b64decode(src_b64)
        if len(raw) > MAX_PLUGIN_BYTES:
            raise ValueError(f"bundle exceeds {MAX_PLUGIN_BYTES} bytes")

        want_sha = (a.get("sha256") or "").lower()
        got_sha = hashlib.sha256(raw).hexdigest()
        if want_sha and want_sha != got_sha:
            raise ValueError("sha256 mismatch — refusing to write")

        text = raw.decode("utf-8")          # a corrupt transfer fails here...
        compile(text, "<plugin.update>", "exec")   # ...or here, before any write.

        # Atomic swap in the target's own dir (rename is atomic within a fs).
        directory = os.path.dirname(target) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ink-cartridge-",
                                   suffix=".py")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(target):
                shutil.copy2(target, target + ".bak")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        # Ack first; restart after a short delay so the reply reaches the phone
        # before the process (and this BT link) goes down.
        def _delayed_restart():
            time.sleep(restart_delay)
            try:
                run(["systemctl", "restart", "pwnagotchi"])
            except Exception as e:
                logging.error("ink-cartridge: self-update restart failed: %s", e)

        threading.Thread(target=_delayed_restart,
                         name="ink-cartridge-selfupdate-restart",
                         daemon=True).start()
        return {"ok": True, "restarting": True,
                "version": a.get("version"), "sha256": got_sha,
                "bytes": len(raw)}

    return {
        "reboot": reboot,
        "shutdown": shutdown,
        "restart": restart,
        "plugin.toggle": plugin_toggle,
        "plugin.update": plugin_update,
    }

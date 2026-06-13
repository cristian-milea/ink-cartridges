"""Loopback proxies: serve the device's web-UI surfaces over RFCOMM.

The Pi's own web server (127.0.0.1:8080) is auth-exempt on loopback, so we
fetch display PNG / session-stats / plugins-list locally and return them. This
keeps the unstable hop (phone<->Pi) on RFCOMM while reusing existing serving.
"""
from __future__ import annotations
import base64
import json
import urllib.request

_BASE = "http://127.0.0.1:8080"


def _default_fetch(path: str):
    """Return (body_bytes, content_type) for a loopback GET."""
    req = urllib.request.Request(_BASE + path)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def make_handlers(fetch=None):
    fetch = fetch or _default_fetch

    def display_png(_a):
        body, ctype = fetch("/ui")
        return {"b64": base64.b64encode(body).decode(), "content_type": ctype}

    def plugins_list(_a):
        body, _ctype = fetch("/plugins")
        return {"html": body.decode("utf-8", "replace")}

    def _stats(kind):
        def handler(args):
            session = args.get("session")
            path = f"/plugins/session-stats/{kind}"
            if session is not None:
                path += f"?session={session}"
            body, _ctype = fetch(path)
            return json.loads(body.decode("utf-8"))
        return handler

    return {
        "display.png": display_png,
        "plugins.list": plugins_list,
        "stats.session": _stats("session"),
        "stats.os": _stats("os"),
        "stats.temp": _stats("temp"),
        "stats.wifi": _stats("wifi"),
    }

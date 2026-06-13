"""ink-cartridge entry plugin.

Builds a single CommandRegistry from the four handler groups (telemetry, apps,
controls), starts the RFCOMM server thread, and keeps a temporary HTTP webhook
surface dispatching into the SAME registry.
"""
from __future__ import annotations

import json
import logging
import threading

# === INTRA-PACKAGE IMPORTS (stripped by build.py) ===
# The dev package dir is hyphenated ("ink-cartridge/"), so relative package
# imports (`from . import x`) are impossible. Put our own dir on sys.path and
# use absolute sibling imports. In the single-file bundle these names already
# live in one namespace, so build.py deletes everything between these markers.
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from registry import CommandRegistry
import telemetry
import apps
import controls
import proxy
import transport
import host_alias
# === END INTRA-PACKAGE IMPORTS ===

try:
    import pwnagotchi.plugins as plugins
    _Base = plugins.Plugin
except Exception:
    plugins = None

    class _Base:  # test/off-device stand-in
        pass


def build_registry(get_agent, get_ui, runtime, get_options=None, toggle_plugin=None) -> CommandRegistry:
    """Register all handler groups into one CommandRegistry.

    Registers (21 commands total):
      telemetry: status, pcap.get, game-over
      apps:      state, manifest, ui, activate, deactivate, install,
                 uninstall, push
      controls:  reboot, shutdown, restart, plugin.toggle
      proxy:     display.png, plugins.list, stats.session, stats.os,
                 stats.temp, stats.wifi
    """
    reg = CommandRegistry()
    for name, handler in telemetry.make_handlers(get_agent, get_ui, get_options).items():
        reg.register(name, handler)
    for name, handler in apps.make_handlers(runtime).items():
        reg.register(name, handler)
    for name, handler in controls.make_handlers(toggle_plugin=toggle_plugin).items():
        reg.register(name, handler)
    for name, handler in proxy.make_handlers().items():
        reg.register(name, handler)
    return reg


class InkCartridge(_Base):
    __author__ = "cristianmilea"
    __version__ = "0.1.0"
    __license__ = "GPL3"
    __description__ = "Companion app host: RFCOMM + HTTP over one command registry."

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        # `self.options` is normally injected by pwnagotchi's loader; default it.
        if not hasattr(self, "options") or self.options is None:
            self.options = {}
        self.runtime = apps.AppsRuntime(options=self.options)
        self._agent = None
        self._ui = None
        self._registry = None
        host_alias.register_host_alias()

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        # Keep the runtime's options in sync with whatever the loader injected.
        self.runtime.options = self.options
        self.runtime.on_loaded()
        self._registry = build_registry(
            get_agent=lambda: self._agent,
            get_ui=lambda: self._ui,
            runtime=self.runtime,
            get_options=lambda: self.options,
            toggle_plugin=self._toggle_plugin,
        )
        # `transport` option selects the companion link(s):
        #   "rfcomm" (Android, Bluetooth Classic SPP) — historical default
        #   "ble"    (iOS, Bluetooth Low Energy GATT)
        #   "both"   (default) — serve both over the one command registry
        mode = str(self.options.get("transport", "both")).lower()
        if mode in ("rfcomm", "both"):
            self._start_thread("rfcomm", self._serve_rfcomm)
        if mode in ("ble", "both"):
            self._start_thread("ble", self._serve_ble)

    def _start_thread(self, label, target):
        try:
            threading.Thread(target=target, name=f"ink-cartridge-{label}",
                             daemon=True).start()
            logging.info("ink-cartridge: %s server thread started", label)
        except Exception as e:
            logging.error("ink-cartridge: failed to start %s server: %s", label, e)

    def _on_client_connected(self, name=None):
        """Connection hook: flash the full-screen e-ink banner naming the phone
        that just connected. Debounced inside the runtime."""
        self.runtime.show_banner(name)

    def _serve_rfcomm(self):
        """Thread body: guard serve_forever so on-device BlueZ/D-Bus failures
        are logged loudly instead of dying silently."""
        try:
            transport.serve_forever(self._registry,
                                    on_client_connected=self._on_client_connected)
        except Exception as e:
            logging.error("ink-cartridge: RFCOMM server thread crashed: %s", e, exc_info=True)

    def _serve_ble(self):
        """Thread body: guard serve_forever_ble (BLE GATT for the iOS app)."""
        try:
            transport.serve_forever_ble(self._registry,
                                        on_client_connected=self._on_client_connected)
        except Exception as e:
            logging.error("ink-cartridge: BLE server thread crashed: %s", e, exc_info=True)

    def on_ui_setup(self, ui):
        self._ui = ui
        self.runtime.on_ui_setup(ui)

    def on_ready(self, agent):
        self._agent = agent

    def on_unload(self, ui):
        self.runtime.on_unload(ui)

    # --- helpers -----------------------------------------------------------

    def _toggle_plugin(self, name, enabled):
        """Best-effort enable/disable a plugin via pwnagotchi's plugin loader."""
        try:
            import pwnagotchi.plugins as _plugins
            fn = getattr(_plugins, "toggle_plugin", None)
            if callable(fn):
                fn(name, bool(enabled))
        except Exception as e:
            logging.warning("ink-cartridge: plugin toggle %s: %s", name, e)
        return enabled

    # --- temporary HTTP surface -------------------------------------------

    def on_webhook(self, subpath, request):
        """Dispatch an HTTP request into the same registry the RFCOMM uses.

        Command names use dots ("pcap.get"); HTTP subpaths may arrive
        slash-separated ("pcap/get"), so convert "/" → ".".
        """
        if self._registry is None:
            return json.dumps({"ok": False, "error": "not ready"})
        cmd = (subpath or "").strip("/").replace("/", ".")
        args = self._extract_args(request)
        env = self._registry.dispatch(cmd, args)
        return json.dumps(env)

    @staticmethod
    def _extract_args(request) -> dict:
        if request is None:
            return {}
        method = (getattr(request, "method", "GET") or "GET").upper()
        if method == "POST":
            try:
                body = request.get_json(silent=True)
            except Exception:
                body = None
            if isinstance(body, dict):
                return body
            form = getattr(request, "form", None)
            if form is not None:
                try:
                    return dict(form)
                except Exception:
                    pass
            return {}
        # GET (or anything else): pull from query args.
        gargs = getattr(request, "args", None)
        if gargs is not None:
            try:
                return dict(gargs)
            except Exception:
                pass
        return {}

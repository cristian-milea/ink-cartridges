# apps.py — the "apps runtime" for the ink-cartridge plugin: load / install /
# uninstall / activate / deactivate / push / render of alternate-screen apps
# ("Ink Cartridges") on the e-ink display.
#
# Moved verbatim out of the old ink-cartridge.py monolith (which lived in the
# device-ops repo). The plugin-instance behaviour that used to live on the
# `InkCartridge` plugin class is now an `AppsRuntime` class; a `make_handlers`
# factory binds it into command handlers for the shared command registry.
#
# An Ink Cartridge is a tiny Python class loaded from the cartridges directory.
# While a cartridge is active, the host freezes pwnagotchi's UI and paints its
# own full frame: a vertical taskbar (left, 16 px) with one icon per loaded
# cartridge, and the cartridge's render output in the remaining area. Pwnagotchi
# keeps hunting underneath. Deactivating restores the normal pwnagotchi UI.
#
# Cartridge contract — drop a *.py file in the cartridges directory containing:
#
#     class MyCartridge:
#         name = "myapp"           # required, unique, [a-z0-9_-]
#         icon = "M"               # required, 1-2 chars (fits in 16x16 cell)
#         version = "1.0.0"        # optional
#         interval_seconds = None  # optional; if set, host re-renders every N sec
#
#         def render(self, draw, w, h):
#             """Required. Paint onto a (w, h) 1-bit canvas using Pillow."""
#
#         def on_data(self, payload):
#             """Optional. Called when push targets this cartridge.
#             Return truthy to trigger an immediate repaint."""
#
#         def published_state(self):
#             """Optional. Return a {key: value} dict exposed via /state's
#             'published' map."""
#
# A cartridge may also ship two sidecar files in the same directory:
#   <stem>.manifest.json — metadata + capability declarations
#   <stem>.ui.json       — declarative Android UI tree
#
# Files starting with '_' or '.' are skipped (use _template.py.example as a guide).

import importlib.util
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from host_alias import (
    wrap_text,
    draw_wrapped,
    draw_wrapped_centered,
    register_host_alias,
)

# Display geometry — waveshare 2.13" V4 is 250x122.
TASKBAR_W = 16
ICON_H = 16

DEFAULT_APPS_DIR = "/usr/local/share/pwnagotchi/ink-cartridge"
DEFAULT_STATE_PATH = "/etc/pwnagotchi/ink-cartridge.state.json"

# Hard cap on installed source size (256 KB). Python plugins are typically a
# few KB; this catches accidents and obvious abuse. The cap applies to the
# combined size of all files in a multi-file install.
MAX_SOURCE_BYTES = 256 * 1024

# Manifest/UI sidecars: <stem>.manifest.json + <stem>.ui.json sit next to
# <stem>.py in the cartridges dir.
ALLOWED_PERMISSIONS = frozenset({"location", "notifications", "network"})


# ---------------------------------------------------------------------------
# Cartridge loader — pure, testable.
# ---------------------------------------------------------------------------

def _is_app_file(filename):
    """A python file we should try to load as a cartridge."""
    if not filename.endswith(".py"):
        return False
    if filename.startswith("_") or filename.startswith("."):
        return False
    if filename == "__init__.py":
        return False
    return True


def _validate_app(obj):
    """Return (ok, reason). obj is a cartridge instance."""
    name = getattr(obj, "name", None)
    icon = getattr(obj, "icon", None)
    if not isinstance(name, str) or not name:
        return False, "missing or empty .name"
    if not all(c.isalnum() or c in "-_" for c in name):
        return False, f"invalid characters in name: {name!r}"
    if not isinstance(icon, str) or not (1 <= len(icon) <= 2):
        return False, "icon must be a 1-2 character string"
    if not callable(getattr(obj, "render", None)):
        return False, "missing .render(draw, w, h)"
    return True, ""


def _instantiate_apps_from_module(mod):
    """Yield cartridge instances from a module: any class with a string .name
    attribute defined at module top level."""
    for attr in vars(mod).values():
        if not isinstance(attr, type):
            continue
        if attr.__module__ != mod.__name__:
            continue
        if not isinstance(getattr(attr, "name", None), str):
            continue
        try:
            yield attr()
        except Exception as e:
            logging.warning("ink-cartridge: failed to instantiate %s: %s", attr, e)


def load_apps_from_dir(directory):
    """Load every cartridge file in `directory`. Returns dict name → instance.
    Files that fail to import are logged and skipped."""
    apps = {}
    if not os.path.isdir(directory):
        logging.info("ink-cartridge: cartridges dir %s does not exist", directory)
        return apps
    for fname in sorted(os.listdir(directory)):
        if not _is_app_file(fname):
            continue
        path = os.path.join(directory, fname)
        mod_name = "ink_cartridge_app_" + os.path.splitext(fname)[0]
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            logging.warning("ink-cartridge: failed to import %s: %s", path, e)
            continue
        for inst in _instantiate_apps_from_module(mod):
            ok, reason = _validate_app(inst)
            if not ok:
                logging.warning("ink-cartridge: skipping %s: %s", inst, reason)
                continue
            if inst.name in apps:
                logging.warning("ink-cartridge: duplicate cartridge name %r — keeping first", inst.name)
                continue
            # Track the file the cartridge came from so we can uninstall it later.
            try:
                inst._source_path = path
            except Exception:
                pass
            # Attach manifest + ui sidecars (None if absent or corrupt — cartridges
            # still load so a missing sidecar can't brick the host).
            stem = os.path.splitext(fname)[0]
            manifest = _load_sidecar_json(_manifest_path(directory, stem))
            if manifest is not None:
                ok, reason = validate_manifest(manifest, inst.name)
                if not ok:
                    logging.warning(
                        "ink-cartridge: %s manifest invalid (%s) — ignored",
                        inst.name, reason)
                    manifest = None
            ui = _load_sidecar_json(_ui_path(directory, stem))
            if ui is not None:
                ok, reason = validate_ui(ui)
                if not ok:
                    logging.warning(
                        "ink-cartridge: %s ui.json invalid (%s) — ignored",
                        inst.name, reason)
                    ui = None
            try:
                inst._manifest = manifest
                inst._ui_schema = ui
            except Exception:
                pass
            apps[inst.name] = inst
    return apps


# ---------------------------------------------------------------------------
# Install / uninstall helpers — pure, testable.
# ---------------------------------------------------------------------------

def _safe_app_filename(name):
    """Return name if it's a safe leaf .py filename, else None."""
    if not isinstance(name, str) or not name:
        return None
    if os.path.basename(name) != name:
        return None
    if '/' in name or '\\' in name or '..' in name:
        return None
    if name.startswith('_') or name.startswith('.'):
        return None
    if not name.endswith('.py'):
        return None
    # Loader uses everything before .py as part of the module name; keep it sane.
    stem = name[:-3]
    if not stem or not all(c.isalnum() or c in '-_' for c in stem):
        return None
    return name


# ---------------------------------------------------------------------------
# Manifest + UI sidecar helpers — pure, testable.
# ---------------------------------------------------------------------------

def _manifest_path(apps_dir, stem):
    return os.path.join(apps_dir, f"{stem}.manifest.json")


def _ui_path(apps_dir, stem):
    return os.path.join(apps_dir, f"{stem}.ui.json")


def _load_sidecar_json(path):
    """Return parsed JSON or None if the file is missing/unreadable.
    Logs but does not raise on parse errors — a corrupt sidecar must not
    take down the host plugin."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logging.warning("ink-cartridge: failed to load sidecar %s: %s", path, e)
        return None


def validate_manifest(manifest, app_name):
    """Return (ok, reason). manifest is the parsed dict (or None)."""
    if manifest is None:
        return True, ""  # absent manifest is OK; treated as default
    if not isinstance(manifest, dict):
        return False, "manifest must be a JSON object"
    name = manifest.get("name")
    if name is not None and name.replace("_", "-") != app_name.replace("_", "-"):
        # The file system prefers underscores (Python module names); the
        # manifest's display name uses hyphens. Treat them as equivalent.
        return False, f"manifest.name {name!r} != app.name {app_name!r}"
    # schema_version tracks the *UI* schema (which widgets ui.json uses). The
    # device never interprets ui.json widgets — it serves the file opaquely and
    # the phone renders it, gating widgets it's too old for via min_app_version
    # (see validate_ui's note). So a schema_version newer than any this plugin
    # has seen is fine to accept: reject only a malformed (non-int / non-positive)
    # value, never a higher one — otherwise every new UI widget would need a
    # plugin redeploy to every device.
    schema = manifest.get("schema_version", 1)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        return False, f"invalid schema_version: {schema!r}"
    requires = manifest.get("requires") or {}
    if not isinstance(requires, dict):
        return False, "requires must be an object"
    perms = requires.get("permissions") or []
    if not isinstance(perms, list):
        return False, "requires.permissions must be a list"
    for p in perms:
        if p not in ALLOWED_PERMISSIONS:
            return False, f"unknown permission: {p!r} (allowed: {sorted(ALLOWED_PERMISSIONS)})"
    secrets = requires.get("secrets") or []
    if not isinstance(secrets, list):
        return False, "requires.secrets must be a list"
    for s in secrets:
        if not isinstance(s, str) or not s:
            return False, f"invalid secret name: {s!r}"
    ds = manifest.get("data_source")
    if ds is not None:
        ok, reason = _validate_data_source(ds)
        if not ok:
            return False, f"data_source: {reason}"
    return True, ""


ALLOWED_DATA_SOURCE_TYPES = frozenset({"http"})
ALLOWED_DATA_SOURCE_METHODS = frozenset({"GET", "POST"})
ALLOWED_DATA_SOURCE_FORMATS = frozenset({"json", "xml"})


def _validate_data_source(ds):
    if not isinstance(ds, dict):
        return False, "must be an object"
    t = ds.get("type")
    if t not in ALLOWED_DATA_SOURCE_TYPES:
        return False, f"unknown type: {t!r} (allowed: {sorted(ALLOWED_DATA_SOURCE_TYPES)})"
    url = ds.get("url")
    if not isinstance(url, str) or not url:
        return False, "missing 'url'"
    method = ds.get("method", "GET")
    if method not in ALLOWED_DATA_SOURCE_METHODS:
        return False, f"unknown method: {method!r}"
    needs = ds.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
        return False, "'needs' must be a list of strings"
    if "format" in ds and ds["format"] not in ALLOWED_DATA_SOURCE_FORMATS:
        return False, f"unknown format: {ds['format']!r} (allowed: {sorted(ALLOWED_DATA_SOURCE_FORMATS)})"
    return True, ""


# Widget types the renderer knows about. Cartridges that reference unknown
# widget types still install (the phone may have a newer renderer) but we
# sanity-check the root node is an object with a string "type".
def validate_ui(ui):
    """Return (ok, reason). ui is the parsed root node (or None)."""
    if ui is None:
        return True, ""
    if not isinstance(ui, dict):
        return False, "ui.json root must be a JSON object"
    if not isinstance(ui.get("type"), str) or not ui["type"]:
        return False, "ui.json root must have a string 'type'"
    return True, ""


def _validate_source(source):
    """Return (ok, reason). Checks size and that the source compiles."""
    if not isinstance(source, str):
        return False, "source must be a string"
    if len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        return False, f"source exceeds {MAX_SOURCE_BYTES} bytes"
    try:
        compile(source, '<install>', 'exec')
    except SyntaxError as e:
        return False, f"syntax error: {e.msg} at line {e.lineno}"
    return True, ""


# ---------------------------------------------------------------------------
# Taskbar layout — pure, testable.
# ---------------------------------------------------------------------------

def taskbar_geometry(taskbar_side, screen_w, screen_h):
    """Return (taskbar_box, app_box) where each box is (x0, y0, x1, y1).

    taskbar_side: "left" or "right".
    """
    if taskbar_side == "right":
        taskbar = (screen_w - TASKBAR_W, 0, screen_w, screen_h)
        app = (0, 0, screen_w - TASKBAR_W, screen_h)
    else:  # left (default)
        taskbar = (0, 0, TASKBAR_W, screen_h)
        app = (TASKBAR_W, 0, screen_w, screen_h)
    return taskbar, app


def icon_positions(taskbar_box, n):
    """Return list of n (x0, y0, x1, y1) icon boxes stacked top-down."""
    x0, y0, x1, _y1 = taskbar_box
    return [(x0, y0 + i * ICON_H, x1, y0 + (i + 1) * ICON_H) for i in range(n)]


# ---------------------------------------------------------------------------
# Rendering helpers (pure where possible).
# ---------------------------------------------------------------------------

_FONT_CACHE = {}


def _font(size):
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    from PIL import ImageFont
    font = ImageFont.truetype("DejaVuSansMono-Bold", size)
    _FONT_CACHE[size] = font
    return font


def _draw_taskbar(draw, taskbar_box, apps_in_order, active_name):
    """Paint the taskbar onto `draw`.

    Active cartridge: filled black box, white char(s).
    Inactive: white box, black char(s), 1-px border on the inner edge.
    """
    boxes = icon_positions(taskbar_box, len(apps_in_order))
    font = _font(11)
    for app, box in zip(apps_in_order, boxes):
        x0, y0, x1, y1 = box
        active = (app.name == active_name)
        if active:
            draw.rectangle(box, fill=0, outline=0)
            fill = 255
        else:
            fill = 0
        text = app.icon
        tw = draw.textlength(text, font=font)
        tx = x0 + max(0, ((x1 - x0) - int(tw)) // 2)
        ty = y0 + max(0, ((y1 - y0) - 12) // 2)
        draw.text((tx, ty), text, font=font, fill=fill)


def _render_app_frame(app, ui, taskbar_side, apps_in_order):
    """Compose the full 1-bit screen image: taskbar + cartridge area.

    Returns the PIL Image. Wraps the cartridge's render() in a try/except so a
    buggy cartridge yields an "ERR <name>" screen instead of crashing the host.
    """
    from PIL import Image, ImageDraw

    width, height = ui.width(), ui.height()
    taskbar_box, app_box = taskbar_geometry(taskbar_side, width, height)

    img = Image.new('1', (width, height), 255)  # white
    ax0, ay0, ax1, ay1 = app_box
    app_w = ax1 - ax0
    app_h = ay1 - ay0

    app_img = Image.new('1', (app_w, app_h), 255)
    app_draw = ImageDraw.Draw(app_img)
    try:
        app.render(app_draw, app_w, app_h)
    except Exception as e:
        logging.exception("ink-cartridge: render() of %s raised: %s", app.name, e)
        app_img = Image.new('1', (app_w, app_h), 255)
        ad = ImageDraw.Draw(app_img)
        ad.text((4, 4), f"ERR {app.name}", font=_font(12), fill=0)
        ad.text((4, 20), str(e)[:40], font=_font(10), fill=0)

    img.paste(app_img, (ax0, ay0))

    draw = ImageDraw.Draw(img)
    # Thin separator on the seam between taskbar and cartridge area. Painted
    # before the taskbar so the active-icon's filled rectangle (which extends
    # to the full taskbar width) sits flush against it without a visible gap.
    tx0, ty0, tx1, ty1 = taskbar_box
    if taskbar_side == "right":
        seam_x = tx0  # left edge of right-side taskbar
    else:
        seam_x = tx1 - 1  # right edge of left-side taskbar
    draw.line([(seam_x, ty0), (seam_x, ty1 - 1)], fill=0, width=1)

    active_name = app.name
    _draw_taskbar(draw, taskbar_box, apps_in_order, active_name)
    return img


def _render_banner_image(ui, name):
    """Full-screen "companion connected" banner: centered, static text.

    No live countdown — an e-ink full refresh is ~1-2s, so a per-second redraw
    would lag and flicker; the text states the fixed dismiss delay instead.
    The connecting client's BT ``name`` is shown best-effort (ellipsised to fit).
    """
    from PIL import Image, ImageDraw

    width, height = ui.width(), ui.height()
    img = Image.new('1', (width, height), 255)  # white, matching _render_app_frame
    draw = ImageDraw.Draw(img)
    margin = 4
    usable = max(1, width - 2 * margin)

    def _fit(text, font):
        if draw.textlength(text, font=font) <= usable:
            return text
        s = text
        while s and draw.textlength(s + "…", font=font) > usable:
            s = s[:-1]
        return (s + "…") if s else text

    title_font, body_font = _font(16), _font(12)
    lines = [("CONNECTED", title_font),
             (_fit(name or "A device", body_font), body_font),
             ("This closes in 10s", body_font)]

    measured = [(t, f, draw.textbbox((0, 0), t, font=f)[3]) for t, f in lines]
    gap = 6
    total = sum(h for _, _, h in measured) + gap * (len(measured) - 1)
    y = max(margin, (height - total) // 2)
    for text, font, h in measured:
        x = max(0, int((width - draw.textlength(text, font=font)) // 2))
        draw.text((x, y), text, font=font, fill=0)
        y += h + gap
    return img


# ---------------------------------------------------------------------------
# State persistence.
# ---------------------------------------------------------------------------

def _load_state(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        # Move the corrupt file aside so the operator can recover it, and so
        # the next _save_state doesn't immediately overwrite their last good
        # data. Without this the active cartridge + per-cartridge payloads
        # silently disappear on every restart.
        corrupt_path = f"{path}.corrupt-{int(time.time())}"
        try:
            os.rename(path, corrupt_path)
            logging.warning("ink-cartridge: state file %s unreadable (%s); moved to %s",
                            path, e, corrupt_path)
        except Exception as rename_err:
            logging.warning("ink-cartridge: failed to load state %s: %s (rename also failed: %s)",
                            path, e, rename_err)
        return {}


def _save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception as e:
        logging.warning("ink-cartridge: failed to save state %s: %s", path, e)


# ---------------------------------------------------------------------------
# Apps runtime — the instance behaviour formerly on the InkCartridge plugin.
# ---------------------------------------------------------------------------

class AppsRuntime:
    """Hosts the alternate-screen apps: load / install / uninstall / activate /
    deactivate / push / render. Behaves exactly like the old plugin instance for
    these responsibilities. `options` is the plugin options dict (apps_dir,
    state_path, taskbar)."""

    def __init__(self, options=None):
        self.options = options if options is not None else {}
        self._ui = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._apps = {}              # name → instance
        self._order = []             # ordered names for stable taskbar layout
        self._active = None          # active cartridge name or None
        self._state = {}             # persisted state dict
        self._banner = None          # transient full-screen banner, or None
        self._banner_last = 0.0      # monotonic ts of last banner (debounce)

    # --- lifecycle ---------------------------------------------------------

    def on_loaded(self):
        apps_dir = self.options.get("apps_dir", DEFAULT_APPS_DIR)
        state_path = self.options.get("state_path", DEFAULT_STATE_PATH)
        self._apps = load_apps_from_dir(apps_dir)
        self._order = sorted(self._apps.keys())
        self._state = _load_state(state_path)
        # Replay last per-cartridge payloads so on reactivation the cartridge
        # shows the last data the phone pushed.
        for name, payload in (self._state.get("data") or {}).items():
            app = self._apps.get(name)
            if app and callable(getattr(app, "on_data", None)):
                try:
                    app.on_data(payload)
                except Exception as e:
                    logging.warning("ink-cartridge: replay on_data for %s: %s", name, e)
        logging.info("ink-cartridge: loaded %d cartridge(s): %s", len(self._apps), ", ".join(self._order))

    def on_ui_setup(self, ui):
        self._ui = ui
        # Start the render thread as soon as we have a UI. on_ready may fire
        # much later (or after webhook activation), so we cannot rely on it.
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._render_loop, name="ink-cartridge", daemon=True)
            self._thread.start()
            logging.info("ink-cartridge: render thread started")
        # Failsafe: never auto-restore the last active cartridge on boot. A
        # restart must land on the plain pwnagotchi screen; the user re-activates
        # a cartridge from the app. Per-cartridge payloads are still replayed in
        # on_loaded, so reactivation shows the last pushed data.

    def on_unload(self, ui):
        self._stop.set()
        self._wake.set()
        if self._active is not None:
            self._do_deactivate()

    # --- internal: activation / deactivation -------------------------------

    def _do_activate(self, name):
        if name not in self._apps:
            raise KeyError(name)
        with self._lock:
            self._active = name
            self._persist()
            if self._ui is not None:
                with self._ui._lock:
                    self._ui._frozen = True
        self._wake.set()
        # Return the new state so callers get it in one round trip (the iOS
        # client decodes this directly; a null/None data would fail to decode).
        return self._public_state()

    def _do_deactivate(self):
        with self._lock:
            self._active = None
            self._persist()
            if self._ui is not None:
                with self._ui._lock:
                    self._ui._frozen = False
        # Nudge pwnagotchi to repaint its own UI immediately.
        try:
            if self._ui is not None:
                self._ui.update(force=True)
        except Exception as e:
            logging.warning("ink-cartridge: force-update after deactivate failed: %s", e)
        # Return the new state (see _do_activate) so the iOS client can decode it.
        return self._public_state()

    def _persist(self):
        state_path = self.options.get("state_path", DEFAULT_STATE_PATH)
        self._state["active"] = self._active
        _save_state(state_path, self._state)

    # --- internal: install / uninstall -------------------------------------

    def _reload_apps(self):
        """Re-discover cartridges from disk. Caller holds self._lock. If the
        active cartridge vanished, deactivate it."""
        apps_dir = self.options.get("apps_dir", DEFAULT_APPS_DIR)
        self._apps = load_apps_from_dir(apps_dir)
        self._order = sorted(self._apps.keys())
        if self._active is not None and self._active not in self._apps:
            self._active = None
            self._persist()
            if self._ui is not None:
                with self._ui._lock:
                    self._ui._frozen = False
            try:
                if self._ui is not None:
                    self._ui.update(force=True)
            except Exception as e:
                logging.warning("ink-cartridge: force-update during reload failed: %s", e)
        else:
            self._wake.set()

    def _do_install(self, files):
        """Install a package — a dict mapping filename → text content.

        Must contain exactly one ``<stem>.py``. May contain ``<stem>.manifest.json``
        and ``<stem>.ui.json`` sidecars. Returns the loaded cartridge's name.
        Raises ValueError on validation failure.
        """
        if not isinstance(files, dict) or not files:
            raise ValueError("missing 'files'")
        # Find the .py — there must be exactly one.
        py_files = [n for n in files if n.endswith(".py")]
        if len(py_files) != 1:
            raise ValueError("package must contain exactly one .py file")
        py_name = py_files[0]
        safe = _safe_app_filename(py_name)
        if not safe:
            raise ValueError(f"invalid filename: {py_name!r}")
        stem = safe[:-3]
        # NOTE: built-ins may be *updated* via install (catalog ships newer
        # versions); they only resist *uninstall* (see _do_uninstall).

        # All sidecars must share the .py stem.
        expected_sidecars = {f"{stem}.manifest.json", f"{stem}.ui.json"}
        for fname in files:
            if fname == py_name:
                continue
            if fname not in expected_sidecars:
                raise ValueError(
                    f"unexpected file {fname!r} — sidecars must be named "
                    f"{stem}.manifest.json or {stem}.ui.json")

        # Combined size cap.
        total_bytes = sum(len(v.encode("utf-8")) for v in files.values()
                          if isinstance(v, str))
        if total_bytes > MAX_SOURCE_BYTES:
            raise ValueError(f"package exceeds {MAX_SOURCE_BYTES} bytes")

        # Python source must compile.
        source = files[py_name]
        ok, reason = _validate_source(source)
        if not ok:
            raise ValueError(reason)

        # Sidecars must parse as JSON if present.
        manifest_text = files.get(f"{stem}.manifest.json")
        manifest = None
        if manifest_text is not None:
            try:
                manifest = json.loads(manifest_text)
            except Exception as e:
                raise ValueError(f"manifest.json invalid JSON: {e}")
            ok, reason = validate_manifest(manifest, stem)
            if not ok:
                raise ValueError(f"manifest.json: {reason}")

        ui_text = files.get(f"{stem}.ui.json")
        if ui_text is not None:
            try:
                ui = json.loads(ui_text)
            except Exception as e:
                raise ValueError(f"ui.json invalid JSON: {e}")
            ok, reason = validate_ui(ui)
            if not ok:
                raise ValueError(f"ui.json: {reason}")

        apps_dir = self.options.get("apps_dir", DEFAULT_APPS_DIR)
        os.makedirs(apps_dir, exist_ok=True)
        # Stale sidecars: any <stem>.{manifest,ui}.json on disk that the new
        # package doesn't include. The package is the source of truth, so
        # they get removed. We snapshot their content first so we can
        # restore on reload failure.
        stale_paths = []
        stale_backup = {}
        for ext in ("manifest.json", "ui.json"):
            sname = f"{stem}.{ext}"
            if sname in files:
                continue
            spath = os.path.join(apps_dir, sname)
            if os.path.exists(spath):
                try:
                    with open(spath, 'r') as f:
                        stale_backup[spath] = f.read()
                    stale_paths.append(spath)
                except Exception:
                    pass

        # Hold the lock across write + reload + rollback. Track each file we
        # wrote so we can clean up all of them on failure.
        written = []
        with self._lock:
            try:
                for fname, content in files.items():
                    target = os.path.join(apps_dir, fname)
                    tmp = target + ".tmp"
                    with open(tmp, 'w') as f:
                        f.write(content)
                    os.replace(tmp, target)
                    written.append(target)
                # Drop stale sidecars after writing the new files so any
                # reload error path can still restore them.
                for spath in stale_paths:
                    try:
                        os.remove(spath)
                    except Exception:
                        pass
                before = set(self._apps)
                self._reload_apps()
                after = set(self._apps)
                # Validate by source path so updates (where the cartridge name
                # already exists in `before`) succeed too.
                expected_path = os.path.join(apps_dir, py_name)
                loaded_name = next(
                    (name for name, inst in self._apps.items()
                     if getattr(inst, '_source_path', None) == expected_path),
                    None,
                )
                if loaded_name is None:
                    raise ValueError(
                        "package installed but no cartridge class with a valid "
                        ".name / .render(draw, w, h) was found")
                return loaded_name
            except Exception:
                # Roll back every file we wrote, then restore stale sidecars.
                for path in written:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                for spath, content in stale_backup.items():
                    try:
                        with open(spath, 'w') as f:
                            f.write(content)
                    except Exception:
                        pass
                self._reload_apps()
                raise

    def _do_uninstall(self, name):
        """Remove the cartridge file and reload. Raises KeyError if unknown."""
        if name not in self._apps:
            raise KeyError(name)
        with self._lock:
            app = self._apps.get(name)
            if app is None:
                raise KeyError(name)
            path = getattr(app, "_source_path", None)
            if not path or not os.path.isfile(path):
                raise ValueError(f"cannot locate source file for {name}")
            os.remove(path)
            # Best-effort sidecar cleanup — missing is fine.
            stem = os.path.splitext(os.path.basename(path))[0]
            apps_dir = os.path.dirname(path)
            for sidecar in (_manifest_path(apps_dir, stem), _ui_path(apps_dir, stem)):
                try:
                    if os.path.isfile(sidecar):
                        os.remove(sidecar)
                except Exception as e:
                    logging.warning("ink-cartridge: failed to remove sidecar %s: %s",
                                    sidecar, e)
            self._reload_apps()

    # --- internal: render loop ---------------------------------------------

    def show_banner(self, name=None, seconds=10):
        """Show a full-screen "companion connected" banner for ``seconds``, then
        restore the previous screen (active cartridge, or pwnagotchi's normal
        face). Safe to call from any thread (BLE/RFCOMM connection hooks).

        Debounced to one banner per 60s so iOS background auto-reconnects don't
        repeatedly flash the slow e-ink. ``name`` is the connecting client's BT
        name, shown best-effort.
        """
        now = time.monotonic()
        if now - self._banner_last < 60.0:
            return
        self._banner_last = now
        if self._ui is None:
            return  # a connection can precede on_ui_setup; nothing to paint on
        with self._lock:
            self._banner = {"name": name, "deadline": now + float(seconds)}
            with self._ui._lock:
                self._ui._frozen = True
        self._wake.set()

    def _active_interval(self):
        # A live banner takes priority — wake when it should expire.
        if self._banner is not None:
            return max(0.0, self._banner["deadline"] - time.monotonic())
        if self._active is None:
            return None  # block forever
        # The e-ink driver registers its render callback some time after
        # on_ui_setup; until we've seen at least one callback fire, keep
        # re-painting on a short cadence so the first physical frame lands as
        # soon as the driver hooks up.
        if self._ui is not None and not list(self._ui._render_cbs):
            return 2.0
        app = self._apps.get(self._active)
        if app is None:
            return None
        iv = getattr(app, "interval_seconds", None)
        if isinstance(iv, (int, float)) and iv > 0:
            return float(iv)
        return None  # push-driven only — block until wake event

    def _render_loop(self):
        while not self._stop.is_set():
            timeout = self._active_interval()
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._ui is None:
                continue
            if self._active is None and self._banner is None:
                continue
            self._paint_once()

    def _paint_once(self):
        try:
            if self._ui is None:
                logging.warning("ink-cartridge: paint skipped, no UI")
                return
            # A transient connection banner takes over the whole screen.
            if self._banner is not None:
                if time.monotonic() < self._banner["deadline"]:
                    img = _render_banner_image(self._ui, self._banner.get("name"))
                    self._push_frame(img)
                    return
                # Expired — clear it and restore the previous screen.
                self._banner = None
                if self._active is None:
                    with self._ui._lock:
                        self._ui._frozen = False
                    try:
                        self._ui.update(force=True)
                    except Exception as e:
                        logging.warning("ink-cartridge: force-update after banner failed: %s", e)
                    return
                # else: fall through to repaint the active cartridge (stays frozen).
            app = self._apps.get(self._active)
            if app is None:
                logging.debug("ink-cartridge: paint skipped, no active cartridge")
                return
            ordered = [self._apps[n] for n in self._order]
            taskbar_side = self.options.get("taskbar", "left")
            img = _render_app_frame(app, self._ui, taskbar_side, ordered)
            self._push_frame(img, label=app.name)
        except Exception as e:
            logging.exception("ink-cartridge: paint failed: %s", e)

    def _push_frame(self, img, label=None):
        """Push one composed frame to the web UI and every e-ink render callback."""
        try:
            import pwnagotchi.ui.web as web
            web.update_frame(img)
        except Exception as e:
            logging.warning("ink-cartridge: web.update_frame failed: %s", e)
        n_cbs = 0
        for cb in list(self._ui._render_cbs):
            n_cbs += 1
            try:
                cb(img)
            except Exception as e:
                logging.warning("ink-cartridge: render cb failed: %s", e)
        if label:
            logging.info("ink-cartridge: painted %s (%d render cbs)", label, n_cbs)

    # --- sidecar reads (formerly served by on_webhook) ---------------------

    def get_manifest(self, name):
        """Return the cartridge's manifest dict, or None (unknown app or no
        manifest). The transport maps None to a 404."""
        app = self._apps.get(name)
        if app is None:
            return None
        return getattr(app, "_manifest", None)

    def get_ui(self, name):
        """Return the cartridge's ui.json schema, or None (unknown app or no
        ui). The transport maps None to a 404."""
        app = self._apps.get(name)
        if app is None:
            return None
        return getattr(app, "_ui_schema", None)

    # --- push --------------------------------------------------------------

    def push(self, name, payload):
        """Deliver `payload` to cartridge `name`'s on_data, persist it, and
        repaint if the cartridge is active. Raises KeyError if the cartridge is
        unknown, ValueError if it doesn't accept data or on_data raised.
        Returns {"ok": True, "repainted": bool}."""
        if not name or name not in self._apps:
            raise KeyError(name)
        app = self._apps[name]
        if not callable(getattr(app, "on_data", None)):
            raise ValueError(f"cartridge {name} does not accept data")
        try:
            changed = app.on_data(payload)
        except Exception as e:
            logging.exception("ink-cartridge: on_data(%s) failed: %s", name, e)
            raise ValueError(f"on_data raised: {e}")
        # Remember last payload per cartridge, persisted across restarts.
        with self._lock:
            self._state.setdefault("data", {})[name] = payload
            self._persist()
        # Only repaint if this cartridge is currently active and state
        # changed (or cartridge didn't bother to return a value — treat
        # as "yes").
        if name == self._active and changed is not False:
            self._wake.set()
        return {"ok": True, "repainted": name == self._active}

    # --- helpers -----------------------------------------------------------

    def _extract_name(self, request):
        if request.is_json:
            body = request.get_json(silent=True) or {}
            return body.get("name")
        return request.form.get("name") or request.args.get("name")

    def _collect_published_state(self):
        """Call published_state() on every cartridge that implements it.
        Failures are swallowed so a buggy cartridge cannot break /state for
        everyone else."""
        out = {}
        for name, app in self._apps.items():
            fn = getattr(app, "published_state", None)
            if not callable(fn):
                continue
            try:
                v = fn()
            except Exception as e:
                logging.warning("ink-cartridge: published_state(%s) raised: %s", name, e)
                continue
            if isinstance(v, dict):
                out[name] = v
        return out

    def _public_state(self):
        return {
            "active": self._active,
            "apps": [
                {"name": a.name, "icon": a.icon,
                 "version": getattr(a, "version", None),
                 "has_ui": getattr(a, "_ui_schema", None) is not None,
                 "has_manifest": getattr(a, "_manifest", None) is not None}
                for a in (self._apps[n] for n in self._order)
            ],
            "published": self._collect_published_state(),
            "now": datetime.now(tz=timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Handler factory — binds the runtime into command handlers for the shared
# command registry (HTTP webhook + RFCOMM).
# ---------------------------------------------------------------------------

def make_handlers(runtime):
    return {
        "state": lambda a: runtime._public_state(),
        "manifest": lambda a: runtime.get_manifest(a["name"]),
        "ui": lambda a: runtime.get_ui(a["name"]),
        "activate": lambda a: runtime._do_activate(a["name"]),
        "deactivate": lambda a: runtime._do_deactivate(),
        "install": lambda a: {"name": runtime._do_install(a["files"])},
        "uninstall": lambda a: runtime._do_uninstall(a["name"]),
        "push": lambda a: runtime.push(a["app"], a["payload"]),
    }

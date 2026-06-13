# telemetry.py — device-status collection + Game Over rendering for the
# ink-cartridge plugin.
#
# Moved verbatim out of the old companion-api.py monolith.  Provides the data
# collectors that build the JSON status payload, the pcap path-safety helper,
# the Game Over rendering, and a `make_handlers` factory that binds these into
# command handlers (status / pcap.get / game-over) for the shared command
# registry.
#
# Returns a JSON object with battery, mode, bt_ip, pwnd, stats, uptime,
# handshakes, and cracked fields.  Each field is collected by an independent
# helper so a single I2C/OS failure degrades only that field (null/[]) without
# aborting the whole response.
import json
import logging
import os
import re
import glob
import struct
import socket
import subprocess
import fcntl
import sqlite3
import tempfile
import time

from datetime import datetime, timezone

# pwnagotchi is not available on the dev Mac; it is imported at module level so
# the plugin loads normally on the device while tests inject fake modules into
# sys.modules first.
import pwnagotchi
import pwnagotchi.utils as pwn_utils

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

I2C_BUS = 1
MAX17040_ADDR = 0x36
REG_SOC = 0x04
REG_VCELL = 0x02

SIOCGIFADDR = 0x8915

DEFAULT_HANDSHAKE_DIR = "/etc/pwnagotchi/handshakes"
# First-seen DB: lets us flag handshakes whose mtime predates a reliable clock.
# The Pi Zero has no RTC, so file mtimes can be wildly wrong if pwnagotchi
# captured handshakes while NTP was not yet synced after a power-on. We record
# each pcap's first-observation wall time (and whether NTP was synced then) so
# the app can render an honest "untrusted" badge instead of "captured 114d ago".
DEFAULT_FIRST_SEEN_PATH = "/var/lib/companion-api/first_seen.json"
# jayofelony's wpa-sec plugin hardcodes this path; overridable via the
# `wpa_sec_db` plugin option for non-standard installs.
DEFAULT_WPA_SEC_DB = "/home/pi/.wpa_sec_db"
POTFILE_NAME = "wpa-sec.cracked.potfile"

# wpa-sec handshake status enum (from its Status enum).
WPA_SEC_STATUS_QUEUED = 0    # TOUPLOAD — pending upload
WPA_SEC_STATUS_INVALID = 1   # INVALID — rejected by wpa-sec.org
WPA_SEC_STATUS_SUCCESS = 2   # SUCCESSFULL — uploaded and accepted

_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')

# Patchable path for /proc/self/cmdline (overridden in tests)
_CMDLINE_PATH = "/proc/self/cmdline"


# ---------------------------------------------------------------------------
# Game Over shutdown screen
# ---------------------------------------------------------------------------

# Default message painted by the game-over command when it carries no text.
DEFAULT_GAME_OVER_TEXT = "(╯°□°)╯ Game Over"
# A monospace bold font that ships with pwnagotchi's Pillow install.
GAME_OVER_FONT = "DejaVuSansMono-Bold"
GAME_OVER_FONT_SIZE = 30
GAME_OVER_MARGIN = 4


# ---------------------------------------------------------------------------
# Potfile parser — pure function, unit-tested
# ---------------------------------------------------------------------------

def parse_potfile(text):
    """Parse a wpa-sec.cracked.potfile string into a list of dicts.

    Each dict has keys: bssid, ssid, password.

    Line format: BSSID:STATION_MAC:SSID:PASSWORD
    - BSSID      = chars [0:17]   (MAC)
    - ':'        = char  [17]
    - STATION    = chars [18:35]  (MAC)
    - ':'        = char  [35]
    - remainder  = chars [36:]    — 'SSID:PASSWORD', split on the LAST ':'

    SSIDs and passwords may themselves contain ':'.  Lines failing MAC
    validation are silently skipped.
    """
    results = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            # MAC fields are fixed-width at known offsets
            if len(line) < 37:
                continue
            if line[17] != ':' or line[35] != ':':
                continue
            bssid = line[0:17]
            station = line[18:35]
            if not _MAC_RE.match(bssid) or not _MAC_RE.match(station):
                continue
            remainder = line[36:]
            if ':' not in remainder:
                continue
            last_colon = remainder.rfind(':')
            ssid = remainder[:last_colon]
            password = remainder[last_colon + 1:]
            results.append({"bssid": bssid, "ssid": ssid, "password": password})
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# Individual data collectors — each isolated, each may return None / [] / raise
# ---------------------------------------------------------------------------

def _collect_battery():
    """Read MAX17040 SOC % and VCELL voltage. Returns dict or raises."""
    import smbus2
    with smbus2.SMBus(I2C_BUS) as bus:
        raw_soc = bus.read_word_data(MAX17040_ADDR, REG_SOC)
        raw_vcell = bus.read_word_data(MAX17040_ADDR, REG_VCELL)

    swapped_soc = struct.unpack('<H', struct.pack('>H', raw_soc))[0]
    percent = swapped_soc / 256.0

    swapped_vcell = struct.unpack('<H', struct.pack('>H', raw_vcell))[0]
    voltage = round((swapped_vcell >> 4) * 1.25 / 1000, 3)

    return {"percent": round(percent, 2), "voltage": voltage}


def _collect_bt_ip(iface):
    """Return the IPv4 of iface via ioctl, or raises OSError if not up."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack('256s', iface[:15].encode('utf-8'))
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)[20:24])
    finally:
        s.close()


def _collect_mode(agent):
    """Return 'AUTO' or 'MANU'. Uses agent if available, else /proc/self/cmdline."""
    if agent is not None:
        try:
            raw = agent.mode
            return "AUTO" if raw == 'auto' else "MANU"
        except AttributeError:
            pass
    # Fallback: read cmdline
    try:
        with open(_CMDLINE_PATH, 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='replace')
        return "MANU" if '--manual' in cmdline else "AUTO"
    except Exception:
        return "AUTO"


def _collect_stats():
    """Return dict with temp_c, mem, load. Raises if /proc/sys is unavailable."""
    return {
        "temp_c": round(pwnagotchi.temperature(), 1),
        "mem": round(pwnagotchi.mem_usage(), 2),
        "load": round(pwnagotchi.cpu_load(tag="companion-api"), 2),
    }


def _collect_uptime():
    """Seconds of uptime from /proc/uptime."""
    return pwnagotchi.uptime()


def _collect_pwnd(agent, handshake_dir):
    """Return dict with session count and total unique handshakes."""
    total = pwn_utils.total_unique_handshakes(handshake_dir)
    session = 0
    if agent is not None:
        try:
            session = len(agent._handshakes)
        except Exception:
            pass
    return {"session": session, "total": total}


def _collect_cracked(handshake_dir):
    """Parse the potfile and return a list of {bssid, ssid, password}."""
    potfile_path = os.path.join(handshake_dir, POTFILE_NAME)
    if not os.path.exists(potfile_path):
        return []
    with open(potfile_path, 'r', errors='replace') as f:
        text = f.read()
    return parse_potfile(text)


# A trailing BSSID in a pcap filename: 12 contiguous hex digits (the format
# pwnagotchi actually writes, e.g. HomeNet_aabbccddeeff.pcap), or a dash/colon
# separated MAC (older / alternate format). Always preceded by '_'.
_BSSID_CONTIGUOUS_RE = re.compile(r'_([0-9A-Fa-f]{12})$')
_BSSID_SEPARATED_RE = re.compile(r'_([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})$')


def _normalize_bssid(raw):
    """Return a MAC as lowercase colon-separated form (handles all 3 inputs)."""
    hexonly = raw.replace('-', '').replace(':', '').lower()
    return ':'.join(hexonly[i:i + 2] for i in range(0, 12, 2))


def _pcap_bssid(filename):
    """
    Extract a BSSID from a pcap filename.

    pwnagotchi writes files as  <ssid>_<bssid>.pcap  where the BSSID is the
    trailing 12 hex digits with no separators, e.g.  HomeNet_aabbccddeeff.pcap.
    Dash/colon separated MACs are also accepted.
    Returns the BSSID with ':' separators (lowercase), or None.
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    m = _BSSID_CONTIGUOUS_RE.search(base) or _BSSID_SEPARATED_RE.search(base)
    if m:
        return _normalize_bssid(m.group(1))
    return None


def _pcap_ssid(filename):
    """Extract SSID from filename prefix (everything before the last '_MAC' segment)."""
    base = os.path.splitext(os.path.basename(filename))[0]
    # Greedy '.+' so the split happens at the LAST '_<MAC>' — SSIDs may
    # themselves contain underscores.
    m = re.match(r'^(.+)_[0-9A-Fa-f]{12}$', base) \
        or re.match(r'^(.+)_[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}$', base)
    if m:
        return m.group(1)
    return base


def _upload_status(pcap_path, db_path):
    """
    Return one of "uploaded", "queued", "invalid", or "unknown".

    Tries the wpa-sec SQLite db first (keyed by full pcap path), then marker
    files (<pcap>.uploaded / <pcap>.upload) when the db is absent, has no row,
    or is unreadable. Returns "unknown" when neither source has an answer.
    """
    _STATUS_MAP = {
        WPA_SEC_STATUS_SUCCESS: "uploaded",
        WPA_SEC_STATUS_QUEUED: "queued",
        WPA_SEC_STATUS_INVALID: "invalid",
    }

    # 1. SQLite — wpa-sec keys handshakes by full pcap path.
    try:
        with sqlite3.connect(db_path, timeout=1) as conn:
            cur = conn.execute(
                "SELECT status FROM handshakes WHERE path=? LIMIT 1",
                (pcap_path,)
            )
            row = cur.fetchone()
        if row is not None:
            status = _STATUS_MAP.get(row[0])
            if status is not None:
                return status
    except Exception:
        pass

    # 2. Marker-file fallback
    if os.path.exists(pcap_path + ".uploaded") or os.path.exists(pcap_path + ".upload"):
        return "uploaded"

    return "unknown"


def _uploaded_status(pcap_path, db_path):
    """Return True/False/None — kept for back-compat; wraps _upload_status."""
    status = _upload_status(pcap_path, db_path)
    if status == "uploaded":
        return True
    if status == "unknown":
        return None
    return False


def _ntp_synced():
    """Return True iff the system clock is currently NTP-synchronized.

    Wraps `timedatectl show -p NTPSynchronized --value`. Any failure (binary
    missing, non-zero exit, garbled output) returns False — we'd rather call
    a working clock untrusted than vice versa.
    """
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip() == "yes"
    except Exception:
        return False


def _first_seen_load(path):
    """Load the first-seen DB. Missing/corrupt → empty dict."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logging.warning("companion-api: _first_seen_load(%s) failed: %s", path, e)
        return {}


def _first_seen_save(path, state):
    """Persist the first-seen DB atomically (tmp + rename in same dir)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".first_seen_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logging.warning("companion-api: _first_seen_save(%s) failed: %s", path, e)


# Trust window for files whose mtime predates the plugin's first observation.
# The Pi has been demonstrably synced sometime in the last 30 days if we're
# synced now, so older mtimes are suspicious.
PREEXISTING_TRUST_WINDOW = 30 * 86400


def _trust_mtime(mtime, now_epoch, now_ntp_synced, entry):
    """Decide whether a pcap's file mtime is a trustworthy capture time.

    entry: the first-seen DB entry for this pcap, or None if no record exists
           (only the unit-test path — production always creates one before
           calling).
           shape: {"first_seen_epoch": int, "ntp_synced": bool}

    Rules in order:
      - mtime in the future → never trusted.
      - No prior record → trust only if currently synced AND mtime is within
        the last 24h.
      - First-observation was unsynced → can't trust anything; fs_epoch itself
        is from a clock-wrong window.
      - File written at-or-after our (synced) first observation → trust.
      - File pre-existed our first observation → trust if currently synced
        AND mtime falls within PREEXISTING_TRUST_WINDOW.
    """
    if mtime > now_epoch + 60:
        return False
    if entry is None:
        return bool(now_ntp_synced and (now_epoch - mtime) <= 86400)
    fs_epoch = entry.get("first_seen_epoch", 0)
    fs_synced = entry.get("ntp_synced", False)
    if not fs_synced:
        return False
    if mtime >= fs_epoch - 60:
        return True
    return bool(now_ntp_synced and (now_epoch - mtime) <= PREEXISTING_TRUST_WINDOW)


def _collect_handshakes(handshake_dir, cracked_map, db_path,
                        first_seen_state=None, now_epoch=None, ntp_synced=None):
    """
    Glob *.pcap in handshake_dir and return a list of handshake dicts.

    cracked_map: dict keyed by BSSID (lowercase, colon-separated) → cracked entry.
    db_path: path to the wpa-sec SQLite db, for upload-status lookup.

    first_seen_state: optional dict (mutated in place) tracking per-pcap
                      first-observation metadata. If provided, each handshake
                      gets a `captured_at_trusted` bool based on whether its
                      mtime can be trusted; otherwise that flag is omitted.
    now_epoch / ntp_synced: optional overrides (defaults to time.time() and
                            _ntp_synced()). Exposed for tests.
    """
    if now_epoch is None:
        now_epoch = time.time()
    if ntp_synced is None:
        ntp_synced = _ntp_synced()

    results = []
    pattern = os.path.join(handshake_dir, "*.pcap")
    for pcap_path in sorted(glob.glob(pattern)):
        try:
            bssid = _pcap_bssid(pcap_path)
            if bssid is None:
                logging.debug("companion-api: no BSSID parseable from %s", pcap_path)
            # If we can pull the SSID from the cracked map, use that (more reliable)
            cracked_entry = cracked_map.get(bssid) if bssid else None
            ssid = cracked_entry["ssid"] if cracked_entry else _pcap_ssid(pcap_path)

            mtime = os.path.getmtime(pcap_path)
            captured_epoch = int(mtime)
            captured_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

            # Upload status is keyed by pcap path, independent of the BSSID.
            upload_status = _upload_status(pcap_path, db_path)

            entry = {
                "ssid": ssid,
                "bssid": bssid,
                "captured_at": captured_at,
                "captured_epoch": captured_epoch,
                "pcap_name": os.path.basename(pcap_path),
                "upload_status": upload_status,
                "uploaded": upload_status == "uploaded",
                "cracked": cracked_entry is not None,
            }

            if first_seen_state is not None:
                pcap_name = entry["pcap_name"]
                fs_entry = first_seen_state.get(pcap_name)
                if fs_entry is None:
                    fs_entry = {
                        "first_seen_epoch": int(now_epoch),
                        "first_seen_iso": datetime.fromtimestamp(
                            now_epoch, tz=timezone.utc
                        ).isoformat(),
                        "ntp_synced": bool(ntp_synced),
                        "mtime_at_first_seen": int(mtime),
                    }
                    first_seen_state[pcap_name] = fs_entry
                entry["captured_at_trusted"] = _trust_mtime(
                    mtime, now_epoch, ntp_synced, fs_entry
                )

            results.append(entry)
        except Exception as e:
            logging.debug("companion-api: error reading %s: %s", pcap_path, e)
            continue
    return results


# ---------------------------------------------------------------------------
# Payload assembly — used by the status handler and directly in tests
# ---------------------------------------------------------------------------

def _build_payload(agent, options):
    """Assemble the full status payload dict.  Each field has its own try/except.

    `options` is the plugin's options dict (handshake_dir, bt_iface, wpa_sec_db,
    first_seen_path).  `agent` is the live pwnagotchi agent (or None).  Neither
    requires the plugin instance itself — this keeps telemetry plugin-agnostic.
    """
    handshake_dir = options.get("handshake_dir", DEFAULT_HANDSHAKE_DIR)
    bt_iface = options.get("bt_iface", "bnep0")
    wpa_sec_db = options.get("wpa_sec_db", DEFAULT_WPA_SEC_DB)
    first_seen_path = options.get("first_seen_path", DEFAULT_FIRST_SEEN_PATH)

    # Collect cracked first so handshakes can cross-reference it
    cracked = []
    try:
        cracked = _collect_cracked(handshake_dir)
    except Exception as e:
        logging.warning("companion-api: _collect_cracked failed: %s", e)

    cracked_map = {e["bssid"].lower(): e for e in cracked}

    battery = None
    try:
        battery = _collect_battery()
    except Exception as e:
        logging.warning("companion-api: _collect_battery failed: %s", e)

    bt_ip = None
    try:
        bt_ip = _collect_bt_ip(bt_iface)
    except Exception as e:
        logging.debug("companion-api: _collect_bt_ip failed: %s", e)

    mode = None
    try:
        mode = _collect_mode(agent)
    except Exception as e:
        logging.warning("companion-api: _collect_mode failed: %s", e)

    stats = None
    try:
        stats = _collect_stats()
    except Exception as e:
        logging.warning("companion-api: _collect_stats failed: %s", e)

    uptime = None
    try:
        uptime = _collect_uptime()
    except Exception as e:
        logging.warning("companion-api: _collect_uptime failed: %s", e)

    pwnd = None
    try:
        pwnd = _collect_pwnd(agent, handshake_dir)
    except Exception as e:
        logging.warning("companion-api: _collect_pwnd failed: %s", e)

    first_seen_state = _first_seen_load(first_seen_path)
    handshakes = []
    try:
        handshakes = _collect_handshakes(
            handshake_dir, cracked_map, wpa_sec_db,
            first_seen_state=first_seen_state,
        )
    except Exception as e:
        logging.warning("companion-api: _collect_handshakes failed: %s", e)
    _first_seen_save(first_seen_path, first_seen_state)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "battery": battery,
        "mode": mode,
        "bt_ip": bt_ip,
        "pwnd": pwnd,
        "stats": stats,
        "uptime": uptime,
        "handshakes": handshakes,
        "cracked": cracked,
    }


# ---------------------------------------------------------------------------
# Path safety helper
# ---------------------------------------------------------------------------

def _safe_pcap_path(handshake_dir, name):
    """Return the absolute path to `name` inside `handshake_dir`, or None.

    Returns None if:
    - name is falsy
    - name contains a path separator or '..'
    - the resolved path escapes handshake_dir
    - the file doesn't exist or isn't a regular file
    """
    if not name:
        return None
    # Reject anything that looks like a path component traversal
    if os.path.basename(name) != name or '/' in name or '\\' in name or '..' in name:
        return None
    real_dir = os.path.realpath(handshake_dir) + os.sep
    path = os.path.realpath(os.path.join(handshake_dir, name))
    if not path.startswith(real_dir):
        return None
    if not os.path.isfile(path):
        return None
    return path


# ---------------------------------------------------------------------------
# Game Over rendering
# ---------------------------------------------------------------------------

def _wrap_text(text, max_width, measure):
    """Wrap `text` into lines no wider than `max_width`.

    Explicit newline characters are honoured as hard breaks (an empty
    paragraph yields an empty line).  A single word wider than `max_width` is
    broken character by character.  `measure(s)` returns the rendered pixel
    width of `s`.
    """
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            trial = word if not line else line + " " + word
            if measure(trial) <= max_width:
                line = trial
                continue
            if line:
                lines.append(line)
                line = ""
            if measure(word) <= max_width:
                line = word
            else:
                for ch in word:
                    if line and measure(line + ch) > max_width:
                        lines.append(line)
                        line = ch
                    else:
                        line += ch
        lines.append(line)
    return lines


def _render_game_over_image(ui, text, font_size):
    """Compose a 1-bit full-screen image with `text` wrapped and centered.

    Uses the View's own black/white values so the result honours the
    `ui.invert` config exactly as a normal frame would.  Lines that overflow
    the screen height are clipped.
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = ui.width(), ui.height()
    font = ImageFont.truetype(GAME_OVER_FONT, font_size)
    img = Image.new('1', (width, height), ui._white)
    drawer = ImageDraw.Draw(img)

    usable = max(1, width - 2 * GAME_OVER_MARGIN)
    lines = _wrap_text(text, usable, lambda s: drawer.textlength(s, font=font))

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    total_h = line_h * len(lines)
    y = max(GAME_OVER_MARGIN, (height - total_h) // 2)

    for line in lines:
        line_w = drawer.textlength(line, font=font)
        x = max(0, int((width - line_w) // 2))
        drawer.text((x, y), line, font=font, fill=ui._black)
        y += line_h
        if y >= height:
            break
    return img


def render_and_show_game_over(ui, text, font_size=GAME_OVER_FONT_SIZE):
    """Paint the Game Over screen, freeze the UI, and trigger shutdown.

    Freezing first stops both the UI refresh loop and pwnagotchi's own
    shutdown screen from repainting over our frame; the e-ink then holds
    the image through `pwnagotchi.shutdown()`'s halt.
    """
    import threading
    import pwnagotchi

    img = _render_game_over_image(ui, text, font_size)

    with ui._lock:
        ui._frozen = True

    try:
        import pwnagotchi.ui.web as web
        web.update_frame(img)
    except Exception as e:
        logging.warning("companion-api: game-over web frame update failed: %s", e)

    for cb in list(ui._render_cbs):
        try:
            cb(img)
        except Exception as e:
            logging.warning("companion-api: game-over render callback failed: %s", e)

    threading.Thread(
        target=pwnagotchi.shutdown, daemon=True, name="pwnagotchi-game-over"
    ).start()


# ---------------------------------------------------------------------------
# Handler factory — binds the collectors into command handlers for the
# shared command registry (HTTP webhook + RFCOMM).
# ---------------------------------------------------------------------------

def make_handlers(get_agent, get_ui, get_options=None):
    """Return {command_name: handler} bound to live agent/ui/options accessors.

    get_agent():   returns the live pwnagotchi agent (or None).
    get_ui():      returns the live UI View (or None).
    get_options(): returns the plugin options dict; defaults to an empty dict
                   so the module constants supply every value.
    """
    if get_options is None:
        get_options = lambda: {}

    def status(_args):
        return _build_payload(get_agent(), get_options())

    def pcap_get(args):
        import base64
        options = get_options()
        handshake_dir = options.get("handshake_dir", DEFAULT_HANDSHAKE_DIR)
        name = args.get("name", "")
        path = _safe_pcap_path(handshake_dir, name)
        if path is None:
            raise FileNotFoundError(name)
        with open(path, "rb") as f:
            return {"name": name, "b64": base64.b64encode(f.read()).decode()}

    def game_over(args):
        render_and_show_game_over(
            get_ui(),
            args.get("text", DEFAULT_GAME_OVER_TEXT),
            font_size=get_options().get("game_over_font_size", GAME_OVER_FONT_SIZE),
        )
        return {"shown": True}

    return {"status": status, "pcap.get": pcap_get, "game-over": game_over}

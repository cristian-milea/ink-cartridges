"""Transports for the companion command registry.

Two links share one framing + dispatch core:

* RFCOMM (Bluetooth Classic SPP) via BlueZ Profile1 — used by the Android app.
* BLE GATT (Bluetooth Low Energy) via BlueZ GattManager1 — used by the iOS app
  (iOS exposes Core Bluetooth/BLE only; Classic SPP/RFCOMM is unavailable to
  non-MFi apps).

Both speak the same NDJSON envelope, one JSON object per ``\\n``-terminated line:

    request   {"id": <int>, "cmd": "<str>", "args": {<obj>}}
    response  {"id": <int>, "ok": <bool>, "data": <any>, "error": "<str>"}

The framing helpers (``serve_stream``, ``_response_bytes``, ``BleFramer``,
``chunk_frames``) are pure. The BlueZ glue (``serve_forever``,
``serve_forever_ble``) only runs on-device with BlueZ + dbus + gi present.

Using BLE keeps the Pi's single Wi-Fi radio free for monitor-mode pwning — the
companion link never touches the radio.
"""
from __future__ import annotations
import json
import logging
import os
import threading

# Both links share ONE dbus connection + the default GLib main context (serviced
# by a single thread), and on-device they now run command handlers concurrently
# (the RFCOMM connection is served on its own worker thread — see NewConnection).
# Serialize dispatch so a command from one link never races a command from the
# other, preserving the original single-threaded-handler safety.
_DISPATCH_LOCK = threading.Lock()

# Commands that mutate the launcher/cartridge state. After one of these succeeds
# we push an unsolicited {"id": null, "event": "state"} frame to every connected
# client (both links) so the OTHER app — e.g. iOS when Android activated a
# cartridge — re-fetches `state` and updates in real time.
_BROADCAST_CMDS = frozenset({"activate", "deactivate", "install", "uninstall"})


def _state_event_frame() -> bytes:
    return (json.dumps({"id": None, "event": "state"}) + "\n").encode()


class _Broadcaster:
    """Fan-out of server-pushed events to all connected clients across both links.

    BLE subscribers are TxCharacteristic instances (added on StartNotify, removed
    on StopNotify); RFCOMM subscribers are connection fds mapped to a per-fd write
    lock that is SHARED with that connection's serve_stream writer, so an event
    push can never interleave on the wire with a response on the same socket.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ble: set = set()        # TxCharacteristic instances
        self._rfcomm: dict = {}       # fd -> threading.Lock (per-fd write lock)

    def register_ble(self, tx) -> None:
        with self._lock:
            self._ble.add(tx)

    def deregister_ble(self, tx) -> None:
        with self._lock:
            self._ble.discard(tx)

    def register_rfcomm(self, fd) -> "threading.Lock":
        wlock = threading.Lock()
        with self._lock:
            self._rfcomm[fd] = wlock
        return wlock

    def deregister_rfcomm(self, fd) -> None:
        with self._lock:
            self._rfcomm.pop(fd, None)

    def publish_state(self) -> None:
        """Push a state-change event to every subscriber. Safe to call from any
        thread (BLE dispatch or an RFCOMM worker); never holds _DISPATCH_LOCK."""
        frame = _state_event_frame()
        with self._lock:
            ble = list(self._ble)
            rfcomm = list(self._rfcomm.items())
        logging.info("ink-cartridge: broadcasting state event to %d BLE + %d RFCOMM client(s)",
                     len(ble), len(rfcomm))
        if ble:
            # BLE notifications (PropertiesChanged) MUST be emitted on the GLib
            # main-loop thread; this may be called from an RFCOMM worker thread.
            try:
                from gi.repository import GLib
                for tx in ble:
                    GLib.idle_add(_ble_notify_once(tx, frame))
            except Exception as e:  # GLib missing (tests) or loop not running
                logging.debug("ink-cartridge: BLE event publish skipped: %s", e)
        for fd, wlock in rfcomm:
            try:
                with wlock:
                    os.write(fd, frame)
            except OSError:
                pass  # socket closed mid-broadcast; serve_stream will deregister


def _ble_notify_once(tx, frame):
    """One-shot GLib idle callback that emits a single TX notification."""
    def _cb():
        try:
            tx.send(frame)
        except Exception:
            pass
        return False  # run once
    return _cb


_BROADCASTER = _Broadcaster()

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"  # TODO: swap to a private UUID
PROFILE_PATH = "/org/bluez/inkcartridge/spp"

# Private 128-bit BLE GATT UUIDs (one service, two characteristics).
#   RX: central -> device (app writes NDJSON requests here)
#   TX: device -> central (device notifies NDJSON responses here)
BLE_SERVICE_UUID = "b1ca0001-9a1e-4c2d-8f3a-1e6b6361727a"
BLE_RX_CHAR_UUID = "b1ca0002-9a1e-4c2d-8f3a-1e6b6361727a"  # write / write-without-response
BLE_TX_CHAR_UUID = "b1ca0003-9a1e-4c2d-8f3a-1e6b6361727a"  # notify
BLE_APP_PATH = "/org/bluez/inkcartridge/gatt"
BLE_ADV_PATH = "/org/bluez/inkcartridge/adv"
BLE_LOCAL_NAME = "Pwnagotchi"

# Conservative default notification payload (ATT MTU 23 - 3 byte header). The
# real value is clamped to the negotiated MTU at runtime; this is only the floor.
BLE_DEFAULT_CHUNK = 20


# ---- Pure framing core (shared by every link) ----

def _response_bytes(line: bytes, registry) -> bytes:
    """Turn one NDJSON request line into one newline-terminated response line."""
    try:
        req = json.loads(line.decode("utf-8"))
        rid = req.get("id")
        cmd = req.get("cmd", "")
        args = req.get("args", {})
    except Exception as e:
        return (json.dumps({"id": None, "ok": False,
                            "error": f"bad request: {e}"}) + "\n").encode()
    if not cmd:
        return (json.dumps({"id": rid, "ok": False,
                            "error": "missing cmd"}) + "\n").encode()
    with _DISPATCH_LOCK:
        env = registry.dispatch(cmd, args)
    # Broadcast OUTSIDE the dispatch lock (never hold it during socket writes).
    if env.get("ok") and cmd in _BROADCAST_CMDS:
        _BROADCASTER.publish_state()
    env_out = {"id": rid, **env}
    return (json.dumps(env_out) + "\n").encode()


def chunk_frames(data: bytes, size: int = BLE_DEFAULT_CHUNK) -> list:
    """Split ``data`` into <=``size`` byte packets for an MTU-limited link."""
    if size <= 0:
        return [data]
    return [data[i:i + size] for i in range(0, len(data), size)]


def serve_stream(reader, writer, registry) -> None:
    """Blocking stream loop (RFCOMM): read NDJSON via reader(n)->bytes, write responses.

    Loops until reader returns b"" (EOF). One JSON object per line.
    """
    buf = b""
    while True:
        if b"\n" not in buf:
            chunk = reader(4096)
            if not chunk:
                if buf.strip():
                    _handle_line(buf, writer, registry)
                return
            buf += chunk
            continue
        line, buf = buf.split(b"\n", 1)
        if line.strip():
            _handle_line(line, writer, registry)


def _handle_line(line: bytes, writer, registry) -> None:
    writer(_response_bytes(line, registry))


class BleFramer:
    """Event-driven NDJSON framing for a packet link (BLE GATT).

    BLE delivers requests as discrete, MTU-sized writes and returns responses as
    notifications, so reassembly is push-style rather than the blocking
    reader/writer of ``serve_stream`` — but the newline framing and dispatch are
    identical. Feed each inbound write chunk; get back zero or more complete,
    newline-terminated response frames ready to chunk + notify.
    """

    def __init__(self, registry) -> None:
        self._registry = registry
        self._buf = b""

    def feed(self, chunk: bytes) -> list:
        out = []
        self._buf += bytes(chunk)
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                out.append(_response_bytes(line, self._registry))
        return out


# ---- On-device only below (BlueZ + dbus + gi; not imported during tests) ----

def _device_name(bus, path):  # pragma: no cover (hardware)
    """Best-effort BlueZ Device1 Alias/Name for an object path. None on failure."""
    try:
        import dbus
        props = dbus.Interface(bus.get_object("org.bluez", path),
                               "org.freedesktop.DBus.Properties")
        d = props.GetAll("org.bluez.Device1")
        return str(d.get("Alias") or d.get("Name") or "") or None
    except Exception:
        return None


def _connected_central_name(bus, adapter):  # pragma: no cover (hardware)
    """Best-effort name of a currently-connected central on ``adapter``.

    BLE StartNotify carries no device path, so enumerate BlueZ objects and
    return the first connected Device1 under the adapter. None on failure.
    """
    try:
        import dbus
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        prefix = f"/org/bluez/{adapter}/"
        for path, ifaces in om.GetManagedObjects().items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and str(path).startswith(prefix) and bool(dev.get("Connected")):
                return str(dev.get("Alias") or dev.get("Name") or "") or None
    except Exception:
        return None
    return None


def serve_forever(registry, on_client_connected=None) -> None:  # pragma: no cover (hardware)
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib

    class Profile(dbus.service.Object):
        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
        def NewConnection(self, device, fd, properties):
            fd = fd.take()
            os.set_blocking(fd, True)
            # Register this connection so broadcast events reach it; the returned
            # lock is SHARED with the writer below so an event push and a command
            # response can't interleave on this socket.
            wlock = _BROADCASTER.register_rfcomm(fd)
            # Notify the connection hook (full-screen e-ink banner) with the
            # connecting phone's BT name, best-effort.
            if on_client_connected is not None:
                try:
                    on_client_connected(_device_name(bus, device))
                except Exception as e:
                    logging.debug("ink-cartridge: connect hook failed: %s", e)
            # serve_stream is a BLOCKING read loop. Run it on a worker thread so
            # this dbus handler returns immediately and the shared GLib main loop
            # stays free to dispatch the BLE characteristic writes/notifications —
            # otherwise an active Android (RFCOMM) link starves the iOS (BLE) link
            # and its commands time out.
            def _run(fd=fd, wlock=wlock):
                def _write(b):
                    with wlock:
                        os.write(fd, b)
                try:
                    serve_stream(lambda n: os.read(fd, n), _write, registry)
                finally:
                    _BROADCASTER.deregister_rfcomm(fd)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            threading.Thread(target=_run, name="ink-cartridge-rfcomm-conn",
                             daemon=True).start()

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, device):
            pass

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    Profile(bus, PROFILE_PATH)
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                         "org.bluez.ProfileManager1")
    mgr.RegisterProfile(PROFILE_PATH, SPP_UUID, {
        "Name": "InkCartridge",
        "Role": "server",
        "RequireAuthentication": dbus.Boolean(False),
        "RequireAuthorization": dbus.Boolean(False),
    })
    logging.info("ink-cartridge: RFCOMM Profile registered")
    # Explicit constructor (not GLib.MainLoop()) — the Pythonic override is set up
    # lazily on first use and is NOT thread-safe: when the RFCOMM and BLE server
    # threads both construct a loop concurrently, one can fall through to the raw
    # static binding GLib.MainLoop.new(context, is_running) and crash. Passing the
    # args explicitly sidesteps the race.
    GLib.MainLoop.new(None, False).run()


def serve_forever_ble(registry, adapter: str = "hci0",
                      on_client_connected=None) -> None:  # pragma: no cover (hardware)
    """Register a BLE GATT server (RX write + TX notify) and advertise it.

    Reuses the pure ``BleFramer`` for reassembly/dispatch; on each TX
    notification we chunk the response to the negotiated MTU.
    """
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib

    BLUEZ = "org.bluez"
    GATT_MANAGER = "org.bluez.GattManager1"
    LE_ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
    DBUS_PROPS = "org.freedesktop.DBus.Properties"
    DBUS_OM = "org.freedesktop.DBus.ObjectManager"

    framer = BleFramer(registry)

    class Application(dbus.service.Object):
        def __init__(self, bus):
            super().__init__(bus, BLE_APP_PATH)
            self.service = Service(bus, 0)

        @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            out = {}
            out[self.service.path] = self.service.get_properties()
            for ch in self.service.chars:
                out[ch.path] = ch.get_properties()
            return out

    class Service(dbus.service.Object):
        def __init__(self, bus, index):
            self.path = f"{BLE_APP_PATH}/service{index}"
            super().__init__(bus, self.path)
            self.rx = RxCharacteristic(bus, 0, self)
            self.tx = TxCharacteristic(bus, 1, self)
            self.chars = [self.rx, self.tx]

        def get_properties(self):
            return {"org.bluez.GattService1": {
                "UUID": BLE_SERVICE_UUID,
                "Primary": dbus.Boolean(True),
                "Characteristics": dbus.Array(
                    [dbus.ObjectPath(c.path) for c in self.chars], signature="o"),
            }}

    class _Char(dbus.service.Object):
        def __init__(self, bus, index, service, uuid, flags):
            self.path = f"{service.path}/char{index}"
            self.uuid = uuid
            self.flags = flags
            self.service = service
            super().__init__(bus, self.path)

        def get_properties(self):
            return {"org.bluez.GattCharacteristic1": {
                "UUID": self.uuid,
                "Service": dbus.ObjectPath(self.service.path),
                "Flags": dbus.Array(self.flags, signature="s"),
            }}

        @dbus.service.method(DBUS_PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return self.get_properties()["org.bluez.GattCharacteristic1"]

    class RxCharacteristic(_Char):
        def __init__(self, bus, index, service):
            super().__init__(bus, index, service,
                             BLE_RX_CHAR_UUID, ["write", "write-without-response"])

        @dbus.service.method("org.bluez.GattCharacteristic1",
                             in_signature="aya{sv}")
        def WriteValue(self, value, options):
            data = bytes(bytearray(value))
            for frame in framer.feed(data):
                self.service.tx.send(frame, options)

    class TxCharacteristic(_Char):
        def __init__(self, bus, index, service):
            super().__init__(bus, index, service, BLE_TX_CHAR_UUID, ["notify"])
            self.notifying = False

        def get_properties(self):
            props = super().get_properties()
            props["org.bluez.GattCharacteristic1"]["Notifying"] = dbus.Boolean(self.notifying)
            return props

        def send(self, frame: bytes, options=None):
            if not self.notifying:
                return
            mtu = BLE_DEFAULT_CHUNK
            try:
                mtu = max(BLE_DEFAULT_CHUNK, int(options.get("mtu", 0)) - 3)
            except Exception:
                pass
            for packet in chunk_frames(frame, mtu):
                self.PropertiesChanged(
                    "org.bluez.GattCharacteristic1",
                    {"Value": dbus.Array([dbus.Byte(b) for b in packet], signature="y")},
                    [])

        @dbus.service.signal(DBUS_PROPS, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        @dbus.service.method("org.bluez.GattCharacteristic1")
        def StartNotify(self):
            self.notifying = True
            _BROADCASTER.register_ble(self)
            # iOS subscribing to TX notifications is the definitive "connected"
            # signal. Notify the hook with the central's BT name (best-effort);
            # show_banner debounces the repeats iOS can trigger on reconnect.
            if on_client_connected is not None:
                try:
                    on_client_connected(_connected_central_name(bus, adapter))
                except Exception as e:
                    logging.debug("ink-cartridge: connect hook failed: %s", e)

        @dbus.service.method("org.bluez.GattCharacteristic1")
        def StopNotify(self):
            self.notifying = False
            _BROADCASTER.deregister_ble(self)

    class Advertisement(dbus.service.Object):
        def __init__(self, bus):
            super().__init__(bus, BLE_ADV_PATH)

        @dbus.service.method(DBUS_PROPS, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return {
                "Type": "peripheral",
                "ServiceUUIDs": dbus.Array([BLE_SERVICE_UUID], signature="s"),
                "LocalName": dbus.String(BLE_LOCAL_NAME),
                "Includes": dbus.Array(["tx-power"], signature="s"),
            }

        @dbus.service.method("org.bluez.LEAdvertisement1")
        def Release(self):
            pass

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = f"/org/bluez/{adapter}"

    app = Application(bus)
    adv = Advertisement(bus)

    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter_path), GATT_MANAGER)
    adv_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter_path), LE_ADV_MANAGER)

    def _ok(label):
        return lambda: logging.info("ink-cartridge: BLE %s registered", label)

    def _err(label):
        return lambda e: logging.error("ink-cartridge: BLE %s failed: %s", label, e)

    gatt_mgr.RegisterApplication(BLE_APP_PATH, {},
                                 reply_handler=_ok("GATT app"),
                                 error_handler=_err("GATT app"))
    adv_mgr.RegisterAdvertisement(BLE_ADV_PATH, {},
                                  reply_handler=_ok("advertisement"),
                                  error_handler=_err("advertisement"))
    # Explicit constructor (not GLib.MainLoop()) — the Pythonic override is set up
    # lazily on first use and is NOT thread-safe: when the RFCOMM and BLE server
    # threads both construct a loop concurrently, one can fall through to the raw
    # static binding GLib.MainLoop.new(context, is_running) and crash. Passing the
    # args explicitly sidesteps the race.
    GLib.MainLoop.new(None, False).run()

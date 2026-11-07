#!/usr/bin/python3
"""iphone-notify: mirror iPhone notifications to the Linux desktop.

Uses Apple Notification Center Service (ANCS) over Bluetooth LE, the same
mechanism smartwatches use. Nothing has to be installed on the iPhone.

  iphone-notify        # run the daemon (what the systemd user service runs)
  iphone-notify pair   # pair an iPhone (asks the running daemon if there is one)

The daemon exposes io.github.arjun.iPhoneNotify on the session bus, which the
GNOME Shell extension uses for its Quick Settings tile.
"""
import argparse
import collections
import logging
import os
import signal
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

log = logging.getLogger("iphone-notify")

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
AGENT_IFACE = "org.bluez.Agent1"
ADV_IFACE = "org.bluez.LEAdvertisement1"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

SVC_NAME = "io.github.arjun.iPhoneNotify"
SVC_PATH = "/io/github/arjun/iPhoneNotify"

ANCS_SERVICE = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIF_SOURCE = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"
ANCS_CHARS = (NOTIF_SOURCE, CONTROL_POINT, DATA_SOURCE)

# ANCS protocol constants
EVT_ADDED, EVT_MODIFIED, EVT_REMOVED = 0, 1, 2
FLAG_SILENT, FLAG_IMPORTANT, FLAG_PREEXISTING = 0x01, 0x02, 0x04
CMD_GET_NOTIF_ATTRS, CMD_GET_APP_ATTRS = 0, 1
ATTR_APP_ID, ATTR_TITLE, ATTR_SUBTITLE, ATTR_MESSAGE = 0, 1, 2, 3
APP_ATTR_DISPLAY_NAME = 0
CATEGORY_INCOMING_CALL = 1
MAX_ATTR_LEN = 512
REQUEST_TIMEOUT_S = 5
RECONNECT_INTERVAL_S = 30
RESUBSCRIBE_S = 15
PAIR_TIMEOUT_S = 180
RECENT_MAX = 20

STATE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "iphone-notify", "devices")


# --------------------------------------------------------------------------
# Desktop notifications (org.freedesktop.Notifications on the session bus)
# --------------------------------------------------------------------------
class Notifier:
    def __init__(self, bus):
        try:
            obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
            self.iface = dbus.Interface(obj, "org.freedesktop.Notifications")
        except dbus.DBusException as e:
            log.warning("No desktop notification service (%s); printing only", e)
            self.iface = None

    def show(self, app_name, summary, body, replaces_id=0, urgency=1):
        log.info("[%s] %s: %s", app_name, summary, body.replace("\n", " | "))
        if not self.iface:
            return 0
        try:
            return int(self.iface.Notify(
                app_name, dbus.UInt32(replaces_id), "phone-apple-iphone-symbolic",
                summary, GLib.markup_escape_text(body), dbus.Array([], signature="s"),
                {"urgency": dbus.Byte(urgency)}, dbus.Int32(-1)))
        except dbus.DBusException as e:
            log.warning("Notify failed: %s", e)
            return 0

    def close(self, notif_id):
        if self.iface and notif_id:
            try:
                self.iface.CloseNotification(dbus.UInt32(notif_id))
            except dbus.DBusException:
                pass


# --------------------------------------------------------------------------
# Session-bus API used by the GNOME Shell extension
# --------------------------------------------------------------------------
class DaemonService(dbus.service.Object):
    def __init__(self, bus, app):
        # Passing the BusName to Object keeps it referenced (the name is released
        # when the BusName is garbage collected).
        super().__init__(dbus.service.BusName(SVC_NAME, bus, do_not_queue=True), SVC_PATH)
        self.app = app

    @dbus.service.method(SVC_NAME, in_signature="", out_signature="ssb")
    def GetState(self):
        return self.app.state()

    @dbus.service.method(SVC_NAME, in_signature="", out_signature="a(sssx)")
    def GetRecent(self):
        return dbus.Array(list(self.app.recent), signature="(sssx)")

    @dbus.service.method(SVC_NAME, in_signature="", out_signature="")
    def StartPairing(self):
        self.app.start_pairing(PAIR_TIMEOUT_S)

    @dbus.service.method(SVC_NAME, in_signature="", out_signature="")
    def StopPairing(self):
        self.app.stop_pairing()

    @dbus.service.signal(SVC_NAME, signature="ssb")
    def StateChanged(self, status, device, pairing):
        pass

    @dbus.service.signal(SVC_NAME, signature="u")
    def PairingCode(self, passkey):
        pass

    @dbus.service.signal(SVC_NAME, signature="sssx")
    def NotificationReceived(self, app, title, body, timestamp):
        pass


# --------------------------------------------------------------------------
# BlueZ helper objects we export: pairing agent and LE advertisement
# --------------------------------------------------------------------------
class PairingAgent(dbus.service.Object):
    """Accepts pairing. Only registered with BlueZ while pairing is active."""
    PATH = "/org/iphonenotify/agent"

    def __init__(self, bus, app):
        super().__init__(bus, self.PATH)
        self.app = app

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self.app.on_pairing_code(passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        self.app.on_pairing_code(passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log.info("PIN: %s", pincode)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log.info("Pairing cancelled by the phone")


class Advertisement(dbus.service.Object):
    """LE advertisement soliciting ANCS, so the iPhone lists/reconnects to us."""
    PATH = "/org/iphonenotify/adv0"

    def __init__(self, bus, name):
        super().__init__(bus, self.PATH)
        self.name = name[:8]  # must fit in the 31-byte advertising payload
        self.discoverable = False  # LE general-discoverable flag, only while pairing

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        if iface != ADV_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs", "Unknown interface")
        return {
            "Type": "peripheral",
            "SolicitUUIDs": dbus.Array([ANCS_SERVICE], signature="s"),
            "LocalName": dbus.String(self.name),
            "Discoverable": dbus.Boolean(self.discoverable),
        }

    @dbus.service.method(ADV_IFACE, in_signature="", out_signature="")
    def Release(self):
        log.debug("Advertisement released")


# --------------------------------------------------------------------------
# One ANCS session per connected iPhone
# --------------------------------------------------------------------------
class AncsSession:
    NOTIF_ATTRS = (ATTR_APP_ID, ATTR_TITLE, ATTR_SUBTITLE, ATTR_MESSAGE)

    def __init__(self, app, dev_path, chars):
        self.app = app
        self.dev_path = dev_path
        self.ns_path = chars[NOTIF_SOURCE]
        self.ds_path = chars[DATA_SOURCE]
        self.cp = dbus.Interface(app.bus.get_object(BLUEZ, chars[CONTROL_POINT]), CHAR_IFACE)
        self.queue = collections.deque()  # pending Control Point requests
        self.inflight = None
        self.buf = bytearray()
        self.timeout_id = None
        self.events = {}            # uid -> (category, flags)
        self.desktop_ids = {}       # uid -> desktop notification id
        self.waiting_for_app = {}   # app_id -> [(uid, attrs)]
        self.ready = False
        self.blocked = False   # connected, but the iPhone won't share notifications
        self.retry_id = None
        self.closed = False
        self.attempts = 0

    # -- setup ------------------------------------------------------------
    def start(self):
        if self.closed:
            return False
        self.attempts += 1
        log.log(logging.DEBUG if self.blocked else logging.INFO,
                "ANCS found on %s, subscribing", self.dev_path)
        ds = dbus.Interface(self.app.bus.get_object(BLUEZ, self.ds_path), CHAR_IFACE)
        ns = dbus.Interface(self.app.bus.get_object(BLUEZ, self.ns_path), CHAR_IFACE)
        # Data Source must be subscribed before Notification Source.
        ds.StartNotify(
            reply_handler=lambda: ns.StartNotify(
                reply_handler=self._subscribed, error_handler=self._sub_error),
            error_handler=self._sub_error)
        return False

    def _subscribed(self):
        # StartNotify can succeed even though the iPhone refused to enable
        # notifications (e.g. "Share System Notifications" is off for this
        # computer). BlueZ only sets Notifying once the phone accepted.
        if not self.closed:
            GLib.timeout_add_seconds(2, self.check_notifying)

    def _notifying(self, path):
        try:
            props = dbus.Interface(self.app.bus.get_object(BLUEZ, path), PROPS_IFACE)
            return bool(props.Get(CHAR_IFACE, "Notifying"))
        except dbus.DBusException:
            return False

    def check_notifying(self):
        if self.closed or self.ready:
            return False
        if self._notifying(self.ns_path) and self._notifying(self.ds_path):
            self.ready = True
            self.blocked = False
            log.info("Receiving notifications from %s", self.app.device_name(self.dev_path))
            self.app.on_session_ready(self)
        elif not self.retry_id:
            if not self.blocked:
                self.blocked = True
                log.warning("%s is connected but not sharing notifications. On the iPhone: "
                            "Settings > Bluetooth > (i) next to this computer > turn on "
                            "'Share System Notifications'. Retrying every %ds.",
                            self.app.device_name(self.dev_path), RESUBSCRIBE_S)
                self.app.emit_state()
            self.retry_id = GLib.timeout_add_seconds(RESUBSCRIBE_S, self._resubscribe)
        return False

    def _resubscribe(self):
        self.retry_id = None
        if self.closed or self.ready:
            return False
        log.debug("Resubscribing on %s", self.dev_path)
        for path in (self.ns_path, self.ds_path):
            dbus.Interface(self.app.bus.get_object(BLUEZ, path), CHAR_IFACE).StopNotify(
                reply_handler=lambda: None, error_handler=lambda e: None)
        self.attempts = 0
        GLib.timeout_add_seconds(1, self.start)
        return False

    def _sub_error(self, e):
        # Right after a reconnect BlueZ may still be busy restoring the GATT
        # subscription and answers InProgress; that is worth retrying.
        if e.get_dbus_name() == "org.bluez.Error.InProgress" and self.attempts < 5:
            log.debug("Subscribe in progress on %s, retrying", self.dev_path)
            GLib.timeout_add_seconds(2, self.start)
            return
        log.warning("Subscribe failed on %s: %s", self.dev_path, e.get_dbus_message())
        self.app.drop_session(self.dev_path)

    def close(self):
        self.closed = True
        self._cancel_timeout()
        if self.retry_id:
            GLib.source_remove(self.retry_id)
            self.retry_id = None

    # -- incoming characteristic values -----------------------------------
    def on_value(self, char_path, value):
        data = bytes(value)
        if char_path == self.ns_path:
            self._on_notification_source(data)
        elif char_path == self.ds_path:
            self._on_data_source(data)

    def _on_notification_source(self, data):
        if len(data) < 8:
            return
        event, flags, category, _count, uid = struct.unpack_from("<BBBBI", data)
        log.debug("NS event=%d flags=%#x cat=%d uid=%d", event, flags, category, uid)
        if event == EVT_REMOVED:
            self.events.pop(uid, None)
            self.app.notifier.close(self.desktop_ids.pop(uid, 0))
            return
        if event == EVT_ADDED and flags & FLAG_PREEXISTING and not self.app.args.show_existing:
            return
        self.events[uid] = (category, flags)
        req = struct.pack("<BI", CMD_GET_NOTIF_ATTRS, uid) + bytes([ATTR_APP_ID])
        for attr in self.NOTIF_ATTRS[1:]:
            req += struct.pack("<BH", attr, MAX_ATTR_LEN)
        self._enqueue(("notif", uid, req))

    def _on_data_source(self, data):
        if not self.inflight:
            return
        self.buf.extend(data)
        kind = self.inflight[0]
        parsed = self._parse_notif() if kind == "notif" else self._parse_app()
        if parsed is None:
            return  # wait for more fragments
        self._finish()
        if kind == "notif":
            self._got_notif_attrs(*parsed)
        else:
            self._got_app_attrs(*parsed)

    # -- Data Source parsing ----------------------------------------------
    @staticmethod
    def _parse_attrs(buf, pos, count):
        attrs = {}
        for _ in range(count):
            if len(buf) < pos + 3:
                return None
            attr_id, length = struct.unpack_from("<BH", buf, pos)
            pos += 3
            if len(buf) < pos + length:
                return None
            attrs[attr_id] = bytes(buf[pos:pos + length]).decode("utf-8", "replace")
            pos += length
        return attrs

    def _parse_notif(self):
        if len(self.buf) < 5:
            return None
        uid = struct.unpack_from("<I", self.buf, 1)[0]
        attrs = self._parse_attrs(self.buf, 5, len(self.NOTIF_ATTRS))
        return None if attrs is None else (uid, attrs)

    def _parse_app(self):
        end = self.buf.find(b"\0", 1)
        if end < 0:
            return None
        app_id = bytes(self.buf[1:end]).decode("utf-8", "replace")
        attrs = self._parse_attrs(self.buf, end + 1, 1)
        return None if attrs is None else (app_id, attrs)

    # -- Control Point request queue (one in flight at a time) -------------
    def _enqueue(self, req):
        self.queue.append(req)
        self._pump()

    def _pump(self):
        if self.closed or self.inflight or not self.queue:
            return
        self.inflight = self.queue.popleft()
        self.buf.clear()
        self.cp.WriteValue(
            dbus.Array(self.inflight[2], signature="y"), {"type": dbus.String("request")},
            reply_handler=lambda: None, error_handler=self._write_error)
        self.timeout_id = GLib.timeout_add_seconds(REQUEST_TIMEOUT_S, self._timed_out)

    def _write_error(self, e):
        # e.g. the notification vanished before we asked for it
        log.debug("Control Point write failed: %s", e.get_dbus_message())
        self._finish()

    def _timed_out(self):
        log.debug("Request timed out: %s", self.inflight[:2] if self.inflight else None)
        self.timeout_id = None
        self._finish()
        return False

    def _cancel_timeout(self):
        if self.timeout_id:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = None

    def _finish(self):
        self._cancel_timeout()
        self.inflight = None
        self.buf.clear()
        GLib.idle_add(lambda: self._pump() and False)

    # -- turning attributes into desktop notifications --------------------
    def _got_notif_attrs(self, uid, attrs):
        app_id = attrs.get(ATTR_APP_ID, "")
        if app_id and app_id not in self.app.app_names:
            waiting = self.waiting_for_app.setdefault(app_id, [])
            waiting.append((uid, attrs))
            if len(waiting) == 1:
                req = bytes([CMD_GET_APP_ATTRS]) + app_id.encode() + b"\0" + bytes([APP_ATTR_DISPLAY_NAME])
                self._enqueue(("app", app_id, req))
            return
        self._display(uid, attrs)

    def _got_app_attrs(self, app_id, attrs):
        self.app.app_names[app_id] = attrs.get(APP_ATTR_DISPLAY_NAME) or app_id
        for uid, nattrs in self.waiting_for_app.pop(app_id, []):
            self._display(uid, nattrs)

    def _display(self, uid, attrs):
        if uid not in self.events:
            return  # removed while we were fetching it
        category, flags = self.events[uid]
        app_id = attrs.get(ATTR_APP_ID, "")
        app_name = self.app.app_names.get(app_id) or app_id or "iPhone"
        title = attrs.get(ATTR_TITLE, "").strip()
        body = "\n".join(s for s in (attrs.get(ATTR_SUBTITLE, "").strip(),
                                    attrs.get(ATTR_MESSAGE, "").strip()) if s)
        summary = f"{app_name}: {title}" if title and title != app_name else app_name
        urgency = 2 if category == CATEGORY_INCOMING_CALL or flags & FLAG_IMPORTANT else 1
        self.desktop_ids[uid] = self.app.notifier.show(
            app_name, summary, body, self.desktop_ids.get(uid, 0), urgency)
        self.app.on_notification(app_name, title, body)
        if len(self.desktop_ids) > 500:  # bound memory for notifications never removed
            for old in list(self.desktop_ids)[:100]:
                del self.desktop_ids[old]
                self.events.pop(old, None)


# --------------------------------------------------------------------------
# Main application: watches BlueZ, manages sessions and pairing
# --------------------------------------------------------------------------
class App:
    def __init__(self, args, service_bus_name):
        self.args = args
        self.bus = dbus.SystemBus()
        session_bus = dbus.SessionBus()
        self.notifier = Notifier(session_bus)
        self.service = DaemonService(session_bus, self) if service_bus_name else None
        self.om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
        self.adapter_path = self._find_adapter()
        self.adapter_props = dbus.Interface(
            self.bus.get_object(BLUEZ, self.adapter_path), PROPS_IFACE)
        self.agent_mgr = dbus.Interface(
            self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
        self.agent = PairingAgent(self.bus, self)
        self.chars = collections.defaultdict(dict)  # dev_path -> {uuid: char_path}
        self.char_owner = {}                        # char_path -> dev_path
        self.sessions = {}                          # dev_path -> AncsSession
        self.app_names = {}
        self.recent = collections.deque(maxlen=RECENT_MAX)
        self.known = self._load_known()
        self.pairing = False
        self.pair_timeout_id = None
        self.loop = GLib.MainLoop()
        self.adv = None
        self.powered = bool(self.adapter_props.Get(ADAPTER_IFACE, "Powered"))

    # -- state reported to the extension ----------------------------------
    def state(self):
        """(status, device, pairing); status is connected, blocked, waiting or bluetooth-off."""
        if not self.powered:
            return ("bluetooth-off", "", self.pairing)
        ready = [s for s in self.sessions.values() if s.ready]
        if ready:
            return ("connected", self.device_name(ready[0].dev_path), self.pairing)
        blocked = [s for s in self.sessions.values() if s.blocked]
        if blocked:
            return ("blocked", self.device_name(blocked[0].dev_path), self.pairing)
        return ("waiting", "", self.pairing)

    def emit_state(self):
        if self.service:
            self.service.StateChanged(*self.state())

    def on_session_ready(self, session):
        self.remember(session.dev_path)
        if self.pairing:
            self.notifier.show("iPhone", f"Connected to {self.device_name(session.dev_path)}",
                               "iPhone notifications will now show up here.")
            self.stop_pairing()
        self.emit_state()

    def on_notification(self, app_name, title, body):
        ts = int(time.time())
        self.recent.appendleft((app_name, title, body, ts))
        if self.service:
            self.service.NotificationReceived(app_name, title, body, dbus.Int64(ts))

    def on_pairing_code(self, passkey):
        code = f"{passkey:06d}"
        print(f"\n>>> Pairing code {code} - tap 'Pair' on the iPhone if it shows the same code.\n",
              flush=True)
        self.notifier.show("iPhone", f"Pairing code {code}",
                           "Tap Pair on your iPhone if it shows the same code.", urgency=2)
        if self.service:
            self.service.PairingCode(dbus.UInt32(passkey))

    # -- pairing ------------------------------------------------------------
    def start_pairing(self, timeout_s=None):
        if self.pairing:
            return
        if not self.powered:
            log.warning("Turn Bluetooth on before pairing")
            return
        try:
            self.agent_mgr.RegisterAgent(PairingAgent.PATH, "DisplayYesNo")
            self.agent_mgr.RequestDefaultAgent(PairingAgent.PATH)
            self.adapter_props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))
        except dbus.DBusException as e:
            log.warning("Could not start pairing: %s", e)
            return
        self.pairing = True
        # Become discoverable over LE only. Making the adapter (classic)
        # discoverable would let the iPhone pair over BR/EDR, where iOS never
        # offers to share notifications.
        self._readvertise(discoverable=True)
        if timeout_s:
            self.pair_timeout_id = GLib.timeout_add_seconds(timeout_s, self._pair_timed_out)
        name = self.adv.name if self.adv else "this computer"
        log.info("Pairing open: on the iPhone go to Settings > Bluetooth and tap %r", name)
        self.emit_state()

    def _pair_timed_out(self):
        self.pair_timeout_id = None
        log.info("Pairing window closed")
        self.stop_pairing()
        return False

    def stop_pairing(self):
        if not self.pairing:
            return
        self.pairing = False
        if self.pair_timeout_id:
            GLib.source_remove(self.pair_timeout_id)
            self.pair_timeout_id = None
        self._readvertise(discoverable=False)
        try:
            self.agent_mgr.UnregisterAgent(PairingAgent.PATH)
        except dbus.DBusException as e:
            log.debug("UnregisterAgent: %s", e)
        log.info("Pairing closed")
        self.emit_state()

    # -- discovery helpers ------------------------------------------------
    def _find_adapter(self):
        for path, ifaces in self.om.GetManagedObjects().items():
            if ADAPTER_IFACE in ifaces and (not self.args.adapter or path.endswith(self.args.adapter)):
                return path
        sys.exit("No Bluetooth adapter found. Is Bluetooth turned on?")

    def _dev_props(self, dev_path):
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, dev_path), PROPS_IFACE)
            return props.GetAll(DEVICE_IFACE)
        except dbus.DBusException:
            return {}

    def device_name(self, dev_path):
        p = self._dev_props(dev_path)
        return str(p.get("Alias") or p.get("Address") or dev_path)

    def _load_known(self):
        try:
            with open(STATE_FILE) as f:
                return {line.strip() for line in f if line.strip()}
        except OSError:
            return set()

    def remember(self, dev_path):
        if dev_path in self.known:
            return
        self.known.add(dev_path)
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write("\n".join(sorted(self.known)) + "\n")

    # -- characteristic tracking -----------------------------------------
    def _add_char(self, path, props):
        uuid = str(props.get("UUID", "")).lower()
        if uuid not in ANCS_CHARS or not path.startswith(self.adapter_path + "/"):
            return
        dev_path = path.rsplit("/", 2)[0]
        self.chars[dev_path][uuid] = path
        self.char_owner[path] = dev_path
        self._try_start(dev_path)

    def _try_start(self, dev_path):
        if dev_path in self.sessions or len(self.chars.get(dev_path, {})) < 3:
            return
        props = self._dev_props(dev_path)
        if not (props.get("Connected") and props.get("ServicesResolved")):
            return
        session = AncsSession(self, dev_path, self.chars[dev_path])
        self.sessions[dev_path] = session
        session.start()

    def drop_session(self, dev_path):
        session = self.sessions.pop(dev_path, None)
        if session:
            session.close()
            log.info("Disconnected from %s", dev_path)
            self.emit_state()

    # -- D-Bus signal handlers -------------------------------------------
    def _on_ifaces_added(self, path, ifaces):
        if CHAR_IFACE in ifaces:
            self._add_char(path, ifaces[CHAR_IFACE])

    def _on_ifaces_removed(self, path, ifaces):
        if CHAR_IFACE in ifaces and path in self.char_owner:
            dev_path = self.char_owner.pop(path)
            self.chars[dev_path] = {u: p for u, p in self.chars[dev_path].items() if p != path}
            self.drop_session(dev_path)
        if DEVICE_IFACE in ifaces:
            self.drop_session(path)
            self.chars.pop(path, None)

    def _on_props_changed(self, iface, changed, _invalidated, path=None):
        if iface == ADAPTER_IFACE and path == self.adapter_path and "Powered" in changed:
            self._set_powered(bool(changed["Powered"]))
        elif iface == CHAR_IFACE and "Value" in changed:
            session = self.sessions.get(self.char_owner.get(path))
            if session:
                session.on_value(path, changed["Value"])
        elif iface == CHAR_IFACE and changed.get("Notifying"):
            session = self.sessions.get(self.char_owner.get(path))
            if session:
                session.check_notifying()
        elif iface == DEVICE_IFACE:
            if changed.get("Connected") is not None and not changed["Connected"]:
                self.drop_session(path)
            if changed.get("ServicesResolved"):
                self._try_start(path)
            if changed.get("Paired") and self.pairing:
                self._trust(path)
                log.info("Paired with %s; waiting for notification access", self.device_name(path))

    def _trust(self, dev_path):
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, dev_path), PROPS_IFACE)
            props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
        except dbus.DBusException as e:
            log.warning("Could not mark device trusted: %s", e)

    # -- Bluetooth on/off ---------------------------------------------------
    def _set_powered(self, powered):
        """Follow the system Bluetooth switch rather than forcing the adapter on."""
        if powered == self.powered:
            return
        self.powered = powered
        if powered:
            log.info("Bluetooth turned on")
            self._register_adv()
            self._reconnect()
        else:
            log.info("Bluetooth turned off; waiting")
            self.stop_pairing()
            for dev_path in list(self.sessions):
                self.drop_session(dev_path)
        self.emit_state()

    def _readvertise(self, discoverable):
        if not self.adv or not self.powered:
            return
        self.adv.discoverable = discoverable
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path),
                           "org.bluez.LEAdvertisingManager1").UnregisterAdvertisement(Advertisement.PATH)
        except dbus.DBusException:
            pass
        self._register_adv()

    def _register_adv(self):
        dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), "org.bluez.LEAdvertisingManager1") \
            .RegisterAdvertisement(Advertisement.PATH, {},
                                   reply_handler=lambda: log.debug("Advertising as %r (discoverable=%s)",
                                                                   self.adv.name, self.adv.discoverable),
                                   error_handler=lambda e: log.warning("Advertising failed: %s", e))

    # -- reconnecting to known iPhones -----------------------------------
    def _reconnect(self):
        if not self.powered:
            return True
        for path, ifaces in self.om.GetManagedObjects().items():
            dev = ifaces.get(DEVICE_IFACE)
            if not dev or not path.startswith(self.adapter_path + "/"):
                continue
            uuids = [str(u).lower() for u in dev.get("UUIDs", [])]
            if dev.get("Paired") and not dev.get("Connected") and (
                    ANCS_SERVICE in uuids or path in self.known):
                log.debug("Trying to reconnect %s", path)
                dbus.Interface(self.bus.get_object(BLUEZ, path), DEVICE_IFACE).Connect(
                    reply_handler=lambda: None, error_handler=lambda e: None, timeout=20)
        return True

    # -- lifecycle --------------------------------------------------------
    def run(self):
        self.bus.add_signal_receiver(self._on_ifaces_added, "InterfacesAdded", OM_IFACE, BLUEZ)
        self.bus.add_signal_receiver(self._on_ifaces_removed, "InterfacesRemoved", OM_IFACE, BLUEZ)
        self.bus.add_signal_receiver(self._on_props_changed, "PropertiesChanged", PROPS_IFACE,
                                     BLUEZ, path_keyword="path")

        name = self.args.name or str(self.adapter_props.Get(ADAPTER_IFACE, "Alias"))
        self.adv = Advertisement(self.bus, name)
        if self.powered:
            self._register_adv()
        else:
            log.info("Bluetooth is off; waiting for it to be turned on")

        for path, ifaces in self.om.GetManagedObjects().items():
            self._on_ifaces_added(path, ifaces)
        self._reconnect()
        GLib.timeout_add_seconds(RECONNECT_INTERVAL_S, self._reconnect)

        if self.args.command == "pair":
            self.start_pairing()
            print(f"""
Ready to pair. On your iPhone:
  1. Settings > Bluetooth, tap "{self.adv.name}" (under Other Devices)
  2. Confirm the pairing code matches the one printed here, tap Pair
  3. Tap Allow when asked to share notifications
""", flush=True)
        log.info("Running; waiting for iPhone (adapter %s)", self.adapter_path)

        try:
            from gi.repository import GLibUnix
            signal_add = GLibUnix.signal_add
        except ImportError:
            signal_add = GLib.unix_signal_add
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal_add(GLib.PRIORITY_DEFAULT, sig, self.loop.quit)
        try:
            self.loop.run()
        finally:
            self._cleanup()

    def _cleanup(self):
        self.stop_pairing()
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path),
                           "org.bluez.LEAdvertisingManager1").UnregisterAdvertisement(Advertisement.PATH)
        except dbus.DBusException:
            pass


def ask_running_daemon_to_pair():
    """If the service is already running, open its pairing window instead."""
    try:
        obj = dbus.SessionBus().get_object(SVC_NAME, SVC_PATH, introspect=False)
        dbus.Interface(obj, SVC_NAME).StartPairing()
    except dbus.DBusException:
        return False
    print(f"""Pairing started in the running iphone-notify service (open for {PAIR_TIMEOUT_S // 60} min).
On your iPhone: Settings > Bluetooth, tap this computer under Other Devices,
tap Pair when the codes match, then Allow notifications.
The pairing code also appears as a desktop notification.""")
    return True


def main():
    parser = argparse.ArgumentParser(description="Mirror iPhone notifications to the desktop (ANCS over BLE).")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "pair"],
                        help="'pair' to pair an iPhone, 'run' (default) to mirror notifications")
    parser.add_argument("--name", help="name shown on the iPhone (max 8 chars; default: adapter alias)")
    parser.add_argument("--adapter", help="Bluetooth adapter, e.g. hci0")
    parser.add_argument("--show-existing", action="store_true",
                        help="also show notifications already on the phone when it connects")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    if args.command == "pair" and ask_running_daemon_to_pair():
        return
    try:
        app = App(args, service_bus_name=True)
    except dbus.exceptions.NameExistsException:
        sys.exit("iphone-notify is already running (systemctl --user status iphone-notify).")
    app.run()


if __name__ == "__main__":
    main()

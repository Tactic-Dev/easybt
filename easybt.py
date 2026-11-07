#!/usr/bin/env python3
"""EasyBT: a simple, reliable Bluetooth manager (GTK3 + BlueZ D-Bus, no polling).

One window: your devices, one button each, plain-English errors, and "Add device".
"""
import json, os, re, subprocess, sys, threading, time
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango
try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # desktop notifications with buttons (Cinnamon ignores GNotification)
    Notify.init("EasyBT")
except (ValueError, ImportError):
    Notify = None
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # Cinnamon/MATE/Xfce native tray icon
except (ValueError, ImportError):
    XApp = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from easybt_settings import Config, SettingsWindow, VISIBLE_SECONDS

APP_ID = "dev.tacticdev.easybt"
BUS = "org.bluez"
DEV_IF, ADP_IF, BAT_IF = "org.bluez.Device1", "org.bluez.Adapter1", "org.bluez.Battery1"
NET_IF = "org.bluez.Network1"
AGENT_PATH = "/dev/tacticdev/easybt/agent"
CONNECT_TIMEOUT_MS = 20000

def friendly(err, what="device"):
    """Turn a BlueZ error into something a person can act on."""
    m = str(err)
    low = m.lower()
    if any(k in low for k in ("page-timeout", "page timeout", "host is down", "connection-unknown",
                              "create-socket", "le-connection", "connection-refused", "abort-by-remote",
                              "connection-aborted", "no route")):
        return f"Couldn't reach it. Make sure the {what} is on and nearby."
    if "timeout" in low or "timed out" in low or "noreply" in low:
        return f"The {what} didn't answer in time. Turn it off and on, then try again."
    if "profile-unavailable" in low or "protocol not available" in low:
        return f"The {what} is busy with another phone or computer. Disconnect it there first."
    if "authenticationfailed" in low or "authentication failed" in low or "authenticationrejected" in low:
        return "Pairing was rejected. Put the device in pairing mode and try again."
    if "authenticationcanceled" in low:
        return "Pairing was cancelled."
    if "inprogress" in low or "in progress" in low:
        return "Already working on it…"
    if "notready" in low or "not ready" in low or "powered" in low:
        return "Bluetooth is off. Turn it on at the top."
    if "alreadyexists" in low:
        return "That device is already paired."
    if "does not exist" in low or "unknownobject" in low or "doesnotexist" in low:
        return "That device went away. Try again."
    # Fall back to the last meaningful chunk of the D-Bus message
    tail = m.split("\n")[0].rsplit(":", 1)[-1].strip()
    return f"Couldn't connect ({tail[:60]}). Try turning the {what} off and on." if tail else "Couldn't connect."

def kind_of(icon):
    icon = icon or ""
    if "headset" in icon or "headphone" in icon: return "headphones", "audio-headphones-symbolic"
    if "audio" in icon or "speaker" in icon: return "speaker", "audio-speakers-symbolic"
    if "keyboard" in icon: return "keyboard", "input-keyboard-symbolic"
    if "mouse" in icon: return "mouse", "input-mouse-symbolic"
    if "phone" in icon: return "phone", "phone-symbolic"
    if "computer" in icon: return "computer", "computer-symbolic"
    if "gaming" in icon or "joystick" in icon: return "controller", "input-gaming-symbolic"
    return "device", "bluetooth-active-symbolic"

def is_audio(icon):
    return kind_of(icon)[0] in ("headphones", "speaker")

def fix_audio_profile(mac):
    """Speakers/headphones sometimes come up in phone-call (headset) mode and sound awful.
    Switch them to the high-quality A2DP profile. Runs in a thread; best effort."""
    card = "bluez_card." + mac.replace(":", "_")
    for _ in range(10):  # card appears a moment after Connect returns
        try:
            out = subprocess.run(["pactl", "list", "cards"], capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return
        block = None
        for chunk in out.split("Card #"):
            if f"Name: {card}" in chunk:
                block = chunk; break
        if block:
            active = (re.search(r"Active Profile:\s*(\S+)", block) or [None, ""])[1]
            if active.startswith("a2dp"):
                return
            profiles = re.findall(r"^\s+(a2dp[\w-]*):.*available: (?:yes|unknown)", block, re.M)
            if profiles:
                subprocess.run(["pactl", "set-card-profile", card, profiles[0]], timeout=3)
            return
        threading.Event().wait(0.5)

def _card_block(mac):
    card = "bluez_card." + mac.replace(":", "_")
    try:
        out = subprocess.run(["pactl", "list", "cards"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return card, None
    for chunk in out.split("Card #"):
        if f"Name: {card}" in chunk:
            return card, chunk
    return card, None

def audio_modes(mac):
    """-> (current 'music'|'calls'|None, music_profile, calls_profile). Picks the best of each kind."""
    card, block = _card_block(mac)
    if not block:
        return None, None, None
    best = {}
    for name, prio in re.findall(r"^\s+([\w-]+):.*priority: (\d+), available: (?:yes|unknown)", block, re.M):
        kind = "music" if name.startswith("a2dp") else "calls" if name.startswith(("headset", "handsfree")) else None
        if kind and (kind not in best or int(prio) > best[kind][1]):
            best[kind] = (name, int(prio))
    active = (re.search(r"Active Profile:\s*(\S+)", block) or [None, ""])[1]
    cur = "music" if active.startswith("a2dp") else "calls" if active.startswith(("headset", "handsfree")) else None
    return cur, best.get("music", (None,))[0], best.get("calls", (None,))[0]

def set_audio_mode(mac, mode):
    _cur, music, calls = audio_modes(mac)
    prof = music if mode == "music" else calls
    if prof:
        subprocess.run(["pactl", "set-card-profile", "bluez_card." + mac.replace(":", "_"), prof], timeout=3)
    return bool(prof)

HCIGETCONNINFO = 0x800448D5  # _IOR('H', 213, int)

def hci_rssi(mac, hci="hci0"):
    """RSSI of an existing classic (ACL) link, via a raw HCI socket + "Read RSSI" command.
    Unprivileged (the kernel allows this read-only command). Value is relative to the controller's
    ideal receive range: 0 = ideal, negative = weaker. None if not available."""
    import socket, fcntl, struct, select
    try:
        dev_id = int(hci.replace("hci", ""))
        addr = bytes(int(x, 16) for x in reversed(mac.split(":")))
    except ValueError:
        return None
    try:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except (OSError, AttributeError):
        return None
    try:
        s.bind((dev_id,))
        # struct hci_conn_info_req { bdaddr; type; [pad]; hci_conn_info { handle u16 @8, ... } }
        req = bytearray(addr + bytes([1]) + bytes(17))
        fcntl.ioctl(s.fileno(), HCIGETCONNINFO, req, True)
        handle = struct.unpack_from("<H", req, 8)[0]
        s.setsockopt(0, 2, struct.pack("<IIIH2x", 1 << 4, 1 << 0x0E, 0, 0))  # only Command Complete events
        s.send(struct.pack("<BHBH", 0x01, 0x1405, 2, handle))                 # OGF 0x05 / OCF 0x0005
        for _ in range(5):
            if not select.select([s], [], [], 1.0)[0]:
                break
            pkt = s.recv(260)
            if len(pkt) >= 10 and pkt[1] == 0x0E and struct.unpack_from("<H", pkt, 4)[0] == 0x1405:
                status, _h, rssi = struct.unpack_from("<BHb", pkt, 6)
                return rssi if status == 0 else None
    except OSError:
        return None
    finally:
        s.close()
    return None

def signal_word(mac, hci="hci0"):
    """Strong / OK / Weak for a connected device (no Blueman needed)."""
    rssi = hci_rssi(mac, hci)
    if rssi is None:
        return None
    return "Strong" if rssi >= -4 else "OK" if rssi >= -12 else "Weak"

UUID_WORDS = {
    "0000110b": "Plays audio", "0000110a": "Sends audio", "0000111e": "Calls & mic", "00001108": "Calls & mic",
    "0000111f": "Phone calls", "00001124": "Keyboard / mouse", "00001812": "Keyboard / mouse",
    "0000110e": "Media buttons", "0000110c": "Media buttons", "00001105": "Files", "00001106": "Files",
    "0000180f": "Battery level", "00001132": "Messages", "0000112f": "Contacts", "00001116": "Internet sharing",
}

def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def open_file(path):
    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)

def show_in_folder(path):
    """Open the file manager with the file selected (Nemo/Nautilus via FileManager1), else just the folder."""
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1",
            "ShowItems", GLib.Variant("(ass)", ([Gio.File.new_for_path(path).get_uri()], "")), None, 0, 3000, None)
    except GLib.Error:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_parent().get_uri(), None)


class TransferWindow(Gtk.Window):
    """Progress for one file transfer, both directions: bar + 'x of y · rate' + Cancel."""
    def __init__(self, app, title, transfer_path, size, on_done=None):
        super().__init__(title=title, transient_for=app.win if app.win.get_visible() else None)
        self.app, self.tpath, self.size, self.on_done = app, transfer_path, size or 0, on_done
        self.set_default_size(380, -1); self.set_border_width(14); self.set_resizable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.head = Gtk.Label(label=title, xalign=0, wrap=True)
        self.bar = Gtk.ProgressBar()
        self.info = Gtk.Label(label="Waiting for the other device…", xalign=0)
        self.info.get_style_context().add_class("dim-label")
        self.btn = Gtk.Button(label="Cancel")
        self.btn.connect("clicked", self.on_btn)
        self.btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.btns.pack_end(self.btn, False, False, 0)
        for w in (self.head, self.bar, self.info, self.btns): box.pack_start(w, False, False, 0)
        self.add(box); self.show_all()
        self.t0 = None; self.finished = False
        self.sub = app.obex.bus.signal_subscribe("org.bluez.obex", "org.freedesktop.DBus.Properties",
                                                 "PropertiesChanged", transfer_path, None,
                                                 Gio.DBusSignalFlags.NONE, self._changed)

    def _changed(self, conn, sender, path, iface, signal, params):
        ifname, ch, _inv = params.unpack()
        if "Size" in ch: self.size = ch["Size"]
        if "Transferred" in ch:
            done = ch["Transferred"]
            if self.t0 is None: self.t0 = GLib.get_monotonic_time()
            secs = max((GLib.get_monotonic_time() - self.t0) / 1e6, 0.001)
            if self.size: self.bar.set_fraction(min(done / self.size, 1.0))
            else: self.bar.pulse()
            self.info.set_text(f"{human_size(done)} of {human_size(self.size)} · {human_size(done / secs)}/s"
                               if self.size else f"{human_size(done)} · {human_size(done / secs)}/s")
        st = ch.get("Status")
        if st == "complete": self.finish(True)
        elif st == "error": self.finish(False)

    def finish(self, ok, msg=None):
        if self.finished: return
        self.finished = True
        self.app.obex.bus.signal_unsubscribe(self.sub)
        if ok: self.bar.set_fraction(1.0)
        self.info.set_markup(GLib.markup_escape_text(msg) if msg else
                             ("<span foreground='#3fb950'>Done.</span>" if ok else
                              "<span foreground='#e5534b'>Transfer failed or was cancelled on the other device.</span>"))
        self.btn.set_label("Close")
        if self.on_done: self.on_done(ok)

    def saved(self, path):
        """Received file is in place: say where, and offer Open / Show in folder."""
        folder = GLib.path_get_basename(GLib.path_get_dirname(path))
        self.info.set_markup(f"<span foreground='#3fb950'>Saved</span> as <b>{GLib.markup_escape_text(GLib.path_get_basename(path))}</b> in {GLib.markup_escape_text(folder)}.")
        for label, fn in (("Show in folder", show_in_folder), ("Open", open_file)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, fn=fn: (fn(path), self.destroy()))
            self.btns.pack_start(b, False, False, 0)
        self.btns.show_all()

    def on_btn(self, _b):
        if not self.finished:
            self.app.obex.bus.call("org.bluez.obex", self.tpath, "org.bluez.obex.Transfer1", "Cancel",
                                   None, None, 0, 3000, None, None)
            self.finish(False, "Cancelled.")
        else:
            self.destroy()


OBEX_AGENT_XML = """
<node><interface name="org.bluez.obex.Agent1">
 <method name="Release"/>
 <method name="AuthorizePush"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
 <method name="Cancel"/>
</interface></node>"""
OBEX_AGENT_PATH = "/dev/tacticdev/easybt/obexagent"


class Obex:
    """Send files (Object Push) and receive them with an accept prompt. Uses obexd on the session bus."""
    def __init__(self, app):
        self.app = app
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.receiving = False  # True once our receive agent is registered with obexd
        node = Gio.DBusNodeInfo.new_for_xml(OBEX_AGENT_XML)
        self._reg = self.bus.register_object(OBEX_AGENT_PATH, node.interfaces[0], self._method, None, None)
        if app.cfg["receive_files"]: self._claim()
        # obexd restarts (or another app releasing the slot) → claim receiving again automatically.
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", "org.bluez.obex", Gio.DBusSignalFlags.NONE,
                                  lambda *a: (setattr(self, "receiving", False), GLib.timeout_add(800, self._claim)))
        GLib.timeout_add_seconds(15, lambda: (self.receiving or self._claim(), True)[-1])

    def _claim(self):
        if self.receiving or not self.app.cfg["receive_files"]:
            return False
        try:
            self.bus.call_sync("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.AgentManager1", "RegisterAgent",
                               GLib.Variant("(o)", (OBEX_AGENT_PATH,)), None, 0, 3000, None)
            self.receiving = True
        except GLib.Error as e:
            if "AlreadyExists" in e.message and "Agent already exists" in e.message:
                pass  # another app (e.g. Blueman's tray) holds it; retried every 15 s
            else:
                print("[obex] receive agent:", e.message, file=sys.stderr)
        return False

    def release(self):
        if not self.receiving: return
        try:
            self.bus.call_sync("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.AgentManager1", "UnregisterAgent",
                               GLib.Variant("(o)", (OBEX_AGENT_PATH,)), None, 0, 3000, None)
        except GLib.Error:
            pass
        self.receiving = False

    def send(self, address, alias, filepath):
        name = GLib.path_get_basename(filepath)
        def session_made(conn, res):
            try:
                session = conn.call_finish(res).unpack()[0]
            except GLib.Error as e:
                self.app.toast(f"Couldn't send to {alias}: " + friendly(e.message, "device")); return
            def pushed(conn2, res2):
                try:
                    tpath, props = conn2.call_finish(res2).unpack()
                except GLib.Error as e:
                    self.app.toast(f"{alias} refused the file. " + ("Accept it on the device." if "forbidden" in e.message.lower() else friendly(e.message)))
                    self._end(session); return
                TransferWindow(self.app, f"Sending {name} to {alias}", tpath, props.get("Size", 0),
                               on_done=lambda ok: self._end(session))
            conn.call("org.bluez.obex", session, "org.bluez.obex.ObjectPush1", "SendFile",
                      GLib.Variant("(s)", (filepath,)), None, 0, 30000, None, pushed)
        self.bus.call("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "CreateSession",
                      GLib.Variant("(sa{sv})", (address, {"Target": GLib.Variant("s", "opp")})),
                      None, 0, 30000, None, session_made)

    def _end(self, session):
        self.bus.call("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "RemoveSession",
                      GLib.Variant("(o)", (session,)), None, 0, 3000, None, None)

    def _props(self, path, iface):
        try:
            r = self.bus.call_sync("org.bluez.obex", path, "org.freedesktop.DBus.Properties", "GetAll",
                                   GLib.Variant("(s)", (iface,)), None, 0, 2000, None)
            return r.unpack()[0]
        except GLib.Error:
            return {}

    def _method(self, conn, sender, path, iface, method, params, inv):
        if method != "AuthorizePush":
            inv.return_value(None); return
        tpath = params.unpack()[0]
        t = self._props(tpath, "org.bluez.obex.Transfer1")
        s = self._props(t.get("Session", "/"), "org.bluez.obex.Session1")
        name = GLib.path_get_basename(t.get("Name") or "file")
        sender_name = self.app.alias_for(s.get("Destination", "")) or "A device"
        if not self.app.cfg["receive_files"]:
            inv.return_dbus_error("org.bluez.obex.Error.Rejected", "Receiving is off"); return
        def accept_now():
            resp(None, Gtk.ResponseType.ACCEPT)
        dlg = Gtk.MessageDialog(transient_for=self.app.win if self.app.win.get_visible() else None, modal=False,
                                message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                                text=f"{sender_name} wants to send you a file")
        dlg.format_secondary_text(f"{name}" + (f" ({human_size(t['Size'])})" if t.get("Size") else "") +
                                  f"\nIt will be saved to {GLib.path_get_basename(self.app.cfg.receive_dir())}.")
        dlg.add_buttons("Decline", Gtk.ResponseType.REJECT, "Accept", Gtk.ResponseType.ACCEPT)
        dlg.set_keep_above(True)
        def resp(d, r):
            if d: d.destroy()
            if r != Gtk.ResponseType.ACCEPT:
                inv.return_dbus_error("org.bluez.obex.Error.Rejected", "Declined"); return
            # obexd only writes inside its root folder (~/.cache/obexd), so receive there, then move to Downloads.
            staging_dir = GLib.build_filenamev([GLib.get_user_cache_dir(), "obexd"])
            GLib.mkdir_with_parents(staging_dir, 0o700)
            staging = GLib.build_filenamev([staging_dir, f"easybt-{GLib.get_real_time()}-{name}"])
            inv.return_value(GLib.Variant("(s)", (staging,)))
            def done(ok):
                if not ok:
                    try: Gio.File.new_for_path(staging).delete(None)
                    except GLib.Error: pass
                    return
                folder = self.app.cfg.receive_dir()
                base, ext = name.rsplit(".", 1) if "." in name else (name, "")
                dest, n = GLib.build_filenamev([folder, name]), 1
                while GLib.file_test(dest, GLib.FileTest.EXISTS):
                    n += 1; dest = GLib.build_filenamev([folder, f"{base} ({n})" + (f".{ext}" if ext else "")])
                try:
                    Gio.File.new_for_path(staging).move(Gio.File.new_for_path(dest), Gio.FileCopyFlags.NONE, None, None, None)
                except GLib.Error as e:
                    dest = staging
                    self.app.notify("File received", f"Couldn't move it to {folder} ({e.message}).")
                win.saved(dest)
                self.app.notify("File received", f"{GLib.path_get_basename(dest)} from {sender_name}",
                                actions=[("Open", lambda: open_file(dest)), ("Show in folder", lambda: show_in_folder(dest))])
            win = TransferWindow(self.app, f"Receiving {name} from {sender_name}", tpath, t.get("Size", 0), on_done=done)
        dlg.connect("response", resp)
        if self.app.cfg["auto_accept_trusted"] and self.app.is_trusted(s.get("Destination", "")):
            dlg.destroy(); accept_now()
        else:
            dlg.show_all()


AGENT_XML = """
<node><interface name="org.bluez.Agent1">
 <method name="Release"/>
 <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
 <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
 <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
 <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
 <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
 <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
 <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
 <method name="Cancel"/>
</interface></node>"""


class Bluez:
    """Thin async wrapper over BlueZ's ObjectManager. Signals drive the UI; nothing polls."""
    def __init__(self, on_change):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.on_change = on_change
        self.objects = {}  # path -> {iface: {prop: value}}
        self.alive = self._has_owner()
        self._load()
        self.bus.signal_subscribe(BUS, "org.freedesktop.DBus.ObjectManager", None, None, None,
                                  Gio.DBusSignalFlags.NONE, self._om_signal)
        self.bus.signal_subscribe(BUS, "org.freedesktop.DBus.Properties", "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self._props_signal)
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", BUS, Gio.DBusSignalFlags.NONE, self._owner_signal)

    def _load(self):
        try:
            r = self.bus.call_sync(BUS, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                                   None, None, Gio.DBusCallFlags.NONE, 5000, None)
            self.objects = {p: {i: dict(props) for i, props in ifs.items()} for p, ifs in r.unpack()[0].items()}
        except GLib.Error:
            self.objects = {}

    def _has_owner(self):
        try:
            r = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                   "NameHasOwner", GLib.Variant("(s)", (BUS,)), None, 0, 2000, None)
            return bool(r.unpack()[0])
        except GLib.Error:
            return False

    def _owner_signal(self, conn, sender, path, iface, signal, params):
        _name, _old, new = params.unpack()
        if new:  # bluetoothd (re)started → reload everything once it has registered objects
            def back():
                self.alive = True; self._load(); self.on_change(); return False
            GLib.timeout_add(800, back)
        else:    # bluetoothd stopped or crashed
            self.alive = False; self.objects = {}; self.on_change()

    def _om_signal(self, conn, sender, path, iface, signal, params):
        p = params.unpack()
        if signal == "InterfacesAdded":
            self.objects.setdefault(p[0], {}).update({i: dict(v) for i, v in p[1].items()})
        elif signal == "InterfacesRemoved":
            o = self.objects.get(p[0], {})
            for i in p[1]: o.pop(i, None)
            if not o: self.objects.pop(p[0], None)
        self.on_change()

    def _props_signal(self, conn, sender, path, iface, signal, params):
        ifname, changed, invalidated = params.unpack()
        if path in self.objects and ifname in (DEV_IF, ADP_IF, BAT_IF, NET_IF):
            self.objects[path].setdefault(ifname, {}).update(changed)
            self.on_change(path)

    # ---- queries ----
    def adapter(self):
        for p, o in sorted(self.objects.items()):
            if ADP_IF in o: return p, o[ADP_IF]
        return None, {}

    def devices(self):
        out = []
        for p, o in self.objects.items():
            if DEV_IF in o:
                d = dict(o[DEV_IF]); d["path"] = p
                d["battery"] = o.get(BAT_IF, {}).get("Percentage")
                out.append(d)
        return out

    # ---- actions (all async with explicit timeouts) ----
    def call(self, path, iface, method, args=None, cb=None, timeout=CONNECT_TIMEOUT_MS):
        def done(conn, res):
            try:
                conn.call_finish(res); cb and cb(None)
            except GLib.Error as e:
                cb and cb(e.message)
        self.bus.call(BUS, path, iface, method, args, None, Gio.DBusCallFlags.NONE, timeout, None, done)

    def set_prop(self, path, iface, prop, value, cb=None):
        self.call(path, "org.freedesktop.DBus.Properties", "Set",
                  GLib.Variant("(ssv)", (iface, prop, value)), cb, 5000)


class Agent:
    """Pairing agent. Always registered as the default, so pairing requests that come FROM other devices
    (a phone pairing to this laptop) get a prompt too, not just ones started with Add device."""
    def __init__(self, bz, parent_getter, adding=lambda: False):
        self.bz, self.parent, self.adding = bz, parent_getter, adding
        self.reg_id = None
        self.node = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)

    def register(self):
        if self.reg_id: return
        try:
            self.reg_id = self.bz.bus.register_object(AGENT_PATH, self.node.interfaces[0], self._method, None, None)
            self.bz.bus.call_sync(BUS, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                                  GLib.Variant("(os)", (AGENT_PATH, "KeyboardDisplay")), None, 0, 3000, None)
            self.bz.bus.call_sync(BUS, "/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent",
                                  GLib.Variant("(o)", (AGENT_PATH,)), None, 0, 3000, None)
        except GLib.Error as e:
            print("[agent] register:", e.message, file=sys.stderr)

    def unregister(self):
        if not self.reg_id: return
        try:
            self.bz.bus.call_sync(BUS, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                                  GLib.Variant("(o)", (AGENT_PATH,)), None, 0, 3000, None)
        except GLib.Error:
            pass
        self.bz.bus.unregister_object(self.reg_id); self.reg_id = None
        self._close_code()

    def _name(self, path):
        d = self.bz.objects.get(path, {}).get(DEV_IF, {})
        return d.get("Alias") or d.get("Name") or "the device"

    def _show_code(self, title, text):
        self._close_code()
        dlg = Gtk.MessageDialog(transient_for=self.parent(), modal=False, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_markup(text)
        dlg.show_all(); self._code_dlg = dlg

    def _close_code(self):
        d = getattr(self, "_code_dlg", None)
        if d: d.destroy(); self._code_dlg = None

    def _ask(self, title, text):
        dlg = Gtk.MessageDialog(transient_for=self.parent(), modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=title)
        dlg.format_secondary_markup(text)
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy(); return ok

    def _method(self, conn, sender, path, iface, method, params, inv):
        a = params.unpack()
        reject = lambda: inv.return_dbus_error("org.bluez.Error.Rejected", "Rejected")
        if method == "RequestPinCode":
            inv.return_value(GLib.Variant("(s)", ("0000",)))
        elif method == "DisplayPinCode":
            self._show_code(f"Type this on {self._name(a[0])}", f"<span size='xx-large' weight='bold'>{a[1]}</span>\nthen press Enter.")
            inv.return_value(None)
        elif method == "RequestPasskey":
            inv.return_value(GLib.Variant("(u)", (0,)))
        elif method == "DisplayPasskey":
            self._show_code(f"Type this on {self._name(a[0])}", f"<span size='xx-large' weight='bold'>{a[1]:06d}</span>\nthen press Enter.")
            inv.return_value(None)
        elif method == "RequestConfirmation":
            if self._ask(f"Pair with {self._name(a[0])}?", f"Check that it shows <b>{a[1]:06d}</b>."): inv.return_value(None)
            else: reject()
        elif method == "RequestAuthorization":
            # "Just works" pairing started by the other device. Fine if we're the ones adding; otherwise ask.
            if self.adding() or self._ask(f"{self._name(a[0])} wants to pair with this laptop",
                                          "Allow it? Only say yes if you're pairing it right now."):
                inv.return_value(None)
            else: reject()
        elif method == "AuthorizeService":
            d = self.bz.objects.get(a[0], {}).get(DEV_IF, {})
            if d.get("Trusted") or self.adding() or self._ask(
                    f"Let {self._name(a[0])} connect?", "It's paired but not trusted. Allow this connection?"):
                inv.return_value(None)
            else: reject()
        elif method == "Release":
            self.reg_id and self.bz.bus.unregister_object(self.reg_id); self.reg_id = None
            inv.return_value(None)
        elif method == "Cancel":
            self._close_code(); inv.return_value(None)
        else:
            reject()


class DeviceRow(Gtk.ListBoxRow):
    def __init__(self, app, dev):
        super().__init__()
        self.app, self.path = app, dev["path"]
        self.busy = False
        box = Gtk.Box(spacing=12, margin=10)
        box.set_size_request(-1, 52)  # uniform row height
        self.icon = Gtk.Image(pixel_size=28)
        box.pack_start(self.icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        # Status is one line (full text in the tooltip) so every row is the same height.
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=30)
        self.status.get_style_context().add_class("dim-label")
        name_line = Gtk.Box(spacing=8)
        name_line.pack_start(self.name, False, True, 0)
        self.bat_icon = Gtk.Image(pixel_size=14); self.bat_lbl = Gtk.Label()
        self.bat_lbl.get_style_context().add_class("dim-label")
        self.bat_box = Gtk.Box(spacing=3); self.bat_box.pack_start(self.bat_icon, False, False, 0)
        self.bat_box.pack_start(self.bat_lbl, False, False, 0); self.bat_box.set_no_show_all(True)
        name_line.pack_start(self.bat_box, False, False, 0)
        text.set_valign(Gtk.Align.CENTER)
        text.pack_start(name_line, False, False, 0); text.pack_start(self.status, False, False, 0)
        box.pack_start(text, True, True, 0)
        self.spinner = Gtk.Spinner()
        box.pack_start(self.spinner, False, False, 0)
        self.btn = Gtk.Button(valign=Gtk.Align.CENTER, width_request=104)
        self.btn.connect("clicked", self.on_toggle)
        box.pack_start(self.btn, False, False, 0)
        more = Gtk.Button(valign=Gtk.Align.CENTER, relief=Gtk.ReliefStyle.NONE, tooltip_text="More")
        more.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        more.connect("clicked", self.show_menu)
        box.pack_start(more, False, False, 0)
        self.add(box)
        self.error = None
        self.signal = None   # "Strong"/"OK"/"Weak" while connected
        self.mode = None     # "music"/"calls" for connected audio devices
        self.modes = (None, None, None)
        self.user_disconnect = False  # set when *we* disconnect, so it isn't reported as a drop
        self.low_warned = False
        self.update(dev)

    def show_menu(self, btn):
        """Built fresh each time so labels/checks match the device right now."""
        menu = Gtk.Menu()
        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label); it.set_sensitive(sensitive)
            it.connect("activate", lambda *_: cb()); menu.append(it)
        d, on, blocked = self.dev, bool(self.dev.get("Connected")), bool(self.dev.get("Blocked"))
        if on and is_audio(d.get("Icon")):
            cur, music, calls = self.modes
            if music: item(("✓ " if cur == "music" else "    ") + "Music (best sound)", lambda: self.set_mode("music"))
            if calls: item(("✓ " if cur == "calls" else "    ") + "Calls (use its mic)", lambda: self.set_mode("calls"))
            if music and not calls:
                item("    Calls: this device has no mic mode", lambda: None, False)
            menu.append(Gtk.SeparatorMenuItem())
        if self.kind not in ("speaker", "headphones", "keyboard", "mouse", "controller"):
            item("Send file…", self.on_send, not blocked and self.app.powered)
        mac = d.get("Address", "")
        auto = self.app.cfg.is_auto(mac)
        item(("✓ " if auto else "    ") + "Connect automatically", lambda: (self.app.cfg.set_auto(mac, not auto), self.update(self.dev)))
        if NET_IF in self.app.bz.objects.get(self.path, {}):
            net_on = self.app.bz.objects[self.path][NET_IF].get("Connected")
            item("Stop using its internet" if net_on else "Use its internet (tethering)", self.on_tether, not blocked and on)
        menu.append(Gtk.SeparatorMenuItem())
        item("Details…", self.on_details)
        item("Rename…", lambda: self.on_rename(None))
        menu.append(Gtk.SeparatorMenuItem())
        item("Unblock" if blocked else "Block (stop it connecting)", self.on_block)
        item("Forget this device…", lambda: self.on_forget(None))
        menu.show_all()
        menu.attach_to_widget(btn, None)
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.NORTH_EAST, None)

    def refresh_extras(self):
        """Signal strength + audio mode; runs in a worker thread (pactl / HCI can take a moment)."""
        d = self.dev
        if not d.get("Connected"):
            sig, modes = None, (None, None, None)
        else:
            sig = signal_word(d.get("Address", ""), self.app.hci)
            modes = audio_modes(d.get("Address", "")) if is_audio(d.get("Icon")) else (None, None, None)
        def apply():
            self.signal, self.modes, self.mode = sig, modes, modes[0]
            self.update(self.dev); return False
        GLib.idle_add(apply)

    def set_mode(self, mode):
        mac = self.dev.get("Address", "")
        def work():
            ok = set_audio_mode(mac, mode)
            if not ok:
                GLib.idle_add(self.app.toast, "That mode isn't available on this device.")
            self.refresh_extras()
        threading.Thread(target=work, daemon=True).start()

    def on_tether(self):
        net = self.app.bz.objects.get(self.path, {}).get(NET_IF, {})
        if net.get("Connected"):
            self.app.bz.call(self.path, NET_IF, "Disconnect", None, lambda e: e and self.app.toast(friendly(e)), 8000)
        else:
            def done(e):
                if e: self.app.toast("Couldn't use its internet. Turn on Bluetooth tethering on the phone "
                                     "(Settings → Hotspot & tethering), then try again.")
                else: self.app.notify("Using phone internet", f"Online through {self.dev.get('Alias')}.")
            self.app.bz.call(self.path, NET_IF, "Connect", GLib.Variant("(s)", ("nap",)), done, 20000)

    def on_block(self):
        self.app.bz.set_prop(self.path, DEV_IF, "Blocked", GLib.Variant("b", not self.dev.get("Blocked")),
                             lambda e: e and self.app.toast(friendly(e)))

    def on_send(self):
        dlg = Gtk.FileChooserNative.new(f"Send a file to {self.dev.get('Alias')}", self.app.win,
                                        Gtk.FileChooserAction.OPEN, "Send", "Cancel")
        if dlg.run() == Gtk.ResponseType.ACCEPT and dlg.get_filename():
            self.app.obex.send(self.dev.get("Address"), self.dev.get("Alias") or "device", dlg.get_filename())
        dlg.destroy()

    def on_details(self):
        d = self.dev
        rows = [("Name", d.get("Alias") or d.get("Name") or "—")]
        if d.get("Name") and d.get("Name") != d.get("Alias"):
            rows.append(("Calls itself", d["Name"]))
        rows += [("Type", self.kind.capitalize()),
                 ("Status", "Blocked" if d.get("Blocked") else "Connected" if d.get("Connected") else "Not connected")]
        if d.get("Connected") and self.signal: rows.append(("Signal", self.signal))
        if d.get("battery") is not None: rows.append(("Battery", f"{d['battery']}%"))
        if self.mode: rows.append(("Sound mode", "Music (best sound)" if self.mode == "music" else "Calls (with mic)"))
        caps = sorted({UUID_WORDS[u[:8]] for u in d.get("UUIDs", []) if u[:8] in UUID_WORDS})
        rows += [("Can do", ", ".join(caps) or "—"),
                 ("Paired", "Yes" if d.get("Paired") else "No"),
                 ("Trusted", "Yes (can reconnect by itself)" if d.get("Trusted") else "No"),
                 ("Address", d.get("Address", "—"))]
        dlg = Gtk.Dialog(title=d.get("Alias") or "Details", transient_for=self.app.win, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=16, row_spacing=6, margin=16)
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=1); kl.get_style_context().add_class("dim-label")
            vl = Gtk.Label(label=v, xalign=0, wrap=True, max_width_chars=36, selectable=True)
            grid.attach(kl, 0, i, 1, 1); grid.attach(vl, 1, i, 1, 1)
        dlg.get_content_area().add(grid); dlg.show_all(); dlg.run(); dlg.destroy()

    def update(self, dev):
        self.dev = dev
        kind, icon = kind_of(dev.get("Icon"))
        self.kind = kind
        self.icon.set_from_icon_name(icon, Gtk.IconSize.DND)
        self.name.set_markup(f"<b>{GLib.markup_escape_text(dev.get('Alias') or dev.get('Name') or dev.get('Address',''))}</b>")
        on = bool(dev.get("Connected"))
        blocked = bool(dev.get("Blocked"))
        bat = dev.get("battery")
        if bat is not None and on:
            lvl = min(100, max(0, int(round(bat / 10.0)) * 10))
            self.bat_icon.set_from_icon_name(f"battery-level-{lvl}-symbolic", Gtk.IconSize.MENU)
            self.bat_lbl.set_markup(f"<small>{bat}%</small>" if bat > 15 else f"<small><span foreground='#e5534b'>{bat}%</span></small>")
            self.bat_box.set_tooltip_text(f"Battery {bat}%")
            self.bat_icon.show(); self.bat_lbl.show(); self.bat_box.show()  # show_all() skips no_show_all widgets
        else:
            self.bat_box.hide()
        self.status.set_line_wrap(False); self.status.set_ellipsize(Pango.EllipsizeMode.END)
        if self.busy:
            pass
        elif self.error:
            # Errors may wrap (they're temporary and need to be readable).
            self.status.set_ellipsize(Pango.EllipsizeMode.NONE); self.status.set_line_wrap(True)
            self.status.set_markup(f"<span foreground='#e5534b'>{GLib.markup_escape_text(self.error)}</span>")
        elif blocked:
            self.status.set_text("Blocked · can't connect (⋯ → Unblock)")
        elif on:
            bits = []
            if self.signal: bits.append(f"{self.signal} signal" if self.signal != "Weak" else "<span foreground='#e3b341'>Weak signal</span>")
            if self.mode == "calls": bits.append("Calls mode")
            self.status.set_markup("<span foreground='#3fb950'>●</span> Connected" + "".join(" · " + b for b in bits))
        else:
            self.status.set_text("Not connected" + (" · connects automatically" if self.app.cfg.is_auto(dev.get("Address", "")) else ""))
        self.status.set_tooltip_text(self.status.get_text())
        self.btn.set_label("Disconnect" if on else "Connect")
        self.btn.set_sensitive(not self.busy and self.app.powered and not blocked)

    def set_busy(self, busy, msg=None):
        self.busy = busy
        if busy:
            self.error = None
            self.spinner.start(); self.status.set_text(msg or "Working…")
        else:
            self.spinner.stop()
        self.update(self.dev)

    def finish(self, err, connecting):
        self.set_busy(False)
        if err:
            self.error = friendly(err, self.kind)
            self.update(self.dev)
            GLib.timeout_add_seconds(12, self._clear_error)
        elif connecting and is_audio(self.dev.get("Icon")):
            threading.Thread(target=fix_audio_profile, args=(self.dev.get("Address", ""),), daemon=True).start()

    def _clear_error(self):
        self.error = None; self.update(self.dev); return False

    def on_toggle(self, _b):
        if self.dev.get("Connected"):
            self.user_disconnect = True
            self.set_busy(True, "Disconnecting…")
            self.app.bz.call(self.path, DEV_IF, "Disconnect", None, lambda e: self.finish(e, False), 8000)
        else:
            self.set_busy(True, "Connecting…")
            self.app.bz.call(self.path, DEV_IF, "Connect", None, lambda e: self.finish(e, True))

    def on_rename(self, _i):
        dlg = Gtk.Dialog(title="Rename", transient_for=self.app.win, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=self.dev.get("Alias", ""), activates_default=True, margin=12)
        dlg.get_content_area().add(entry); dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK and entry.get_text().strip():
            self.app.bz.set_prop(self.path, DEV_IF, "Alias", GLib.Variant("s", entry.get_text().strip()))
        dlg.destroy()

    def on_forget(self, _i):
        name = self.dev.get("Alias") or "this device"
        dlg = Gtk.MessageDialog(transient_for=self.app.win, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text=f"Forget {name}?")
        dlg.format_secondary_text("You'll need to pair it again to use it.")
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Forget", Gtk.ResponseType.OK)
        dlg.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        if dlg.run() == Gtk.ResponseType.OK:
            ap, _ = self.app.bz.adapter()
            self.app.bz.call(ap, ADP_IF, "RemoveDevice", GLib.Variant("(o)", (self.path,)),
                             lambda e: e and self.app.toast(friendly(e)), 8000)
        dlg.destroy()


class AddDialog(Gtk.Dialog):
    """Scan, show only named unpaired devices, one click to pair + trust + connect."""
    def __init__(self, app):
        super().__init__(title="Add a device", transient_for=app.win, modal=True)
        self.app = app
        self.set_default_size(400, 440)
        self.add_button("Done", Gtk.ResponseType.CLOSE)
        area = self.get_content_area(); area.set_spacing(8); area.set_border_width(12)
        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Put your device in pairing mode (usually hold its Bluetooth or power button until a light flashes).")
        area.pack_start(hint, False, False, 0)
        top = Gtk.Box(spacing=8)
        self.spin = Gtk.Spinner(); self.spin.start()
        self.state = Gtk.Label(label="Looking for devices…", xalign=0)
        top.pack_start(self.spin, False, False, 0); top.pack_start(self.state, True, True, 0)
        area.pack_start(top, False, False, 0)
        sc = Gtk.ScrolledWindow(vexpand=True); self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.set_placeholder(Gtk.Label(label="Nothing yet…", margin=24))
        sc.add(self.list); area.pack_start(sc, True, True, 0)
        self.rows = {}
        self.pairing = set()
        app.agent.register()
        self.ap, _ = app.bz.adapter()
        app.bz.call(self.ap, ADP_IF, "SetDiscoveryFilter",
                    GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "auto")},)), None, 3000)
        app.bz.call(self.ap, ADP_IF, "StartDiscovery", None,
                    lambda e: e and self.state.set_text(friendly(e)), 5000)
        self.connect("response", lambda *a: self.destroy())
        self.connect("destroy", self._cleanup)
        self.refresh()
        self.show_all()

    def _cleanup(self, *a):
        self.app.bz.call(self.ap, ADP_IF, "StopDiscovery", None, None, 3000)
        self.app.add_dlg = None

    def refresh(self):
        seen = set()
        for d in self.app.bz.devices():
            p = d["path"]
            if (d.get("Paired") and p not in self.pairing) or not (d.get("Alias") or d.get("Name")):
                continue
            if not d.get("Name") and p not in self.pairing:  # Alias = MAC-ish when no name; hide noise
                continue
            seen.add(p)
            if p not in self.rows:
                row = Gtk.ListBoxRow(); box = Gtk.Box(spacing=12, margin=8)
                img = Gtk.Image.new_from_icon_name(kind_of(d.get("Icon"))[1], Gtk.IconSize.DND)
                lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                sub = Gtk.Label(xalign=0); sub.get_style_context().add_class("dim-label")
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); col.pack_start(lab, False, False, 0); col.pack_start(sub, False, False, 0)
                sp = Gtk.Spinner(); btn = Gtk.Button(label="Pair", valign=Gtk.Align.CENTER)
                btn.connect("clicked", self.on_pair, p)
                for w, e in ((img, False), (col, True), (sp, False), (btn, False)): box.pack_start(w, e, e, 0)
                row.add(box); row.show_all(); self.list.add(row)
                self.rows[p] = (row, lab, sub, sp, btn)
            row, lab, sub, sp, btn = self.rows[p]
            lab.set_markup(f"<b>{GLib.markup_escape_text(d.get('Alias') or d.get('Name'))}</b>")
            if p not in self.pairing:
                sub.set_text(kind_of(d.get("Icon"))[0].capitalize())
        for p in list(self.rows):
            if p not in seen:
                self.rows.pop(p)[0].destroy()
        n = len(self.rows)
        if not self.pairing:
            self.state.set_text("Looking for devices…" if n == 0 else f"Found {n}. Pick yours.")

    def on_pair(self, btn, path):
        row, lab, sub, sp, _ = self.rows[path]
        self.pairing.add(path); btn.set_sensitive(False); sp.start(); sub.set_text("Pairing…")
        bz = self.app.bz
        def fail(e):
            self.pairing.discard(path); sp.stop(); btn.set_sensitive(True); btn.set_label("Try again")
            sub.set_markup(f"<span foreground='#e5534b'>{GLib.markup_escape_text(friendly(e))}</span>")
        def paired(e):
            if e and "alreadyexists" not in e.lower(): return fail(e)
            bz.set_prop(path, DEV_IF, "Trusted", GLib.Variant("b", True))
            sub.set_text("Connecting…")
            bz.call(path, DEV_IF, "Connect", None, connected)
        def connected(e):
            sp.stop(); self.pairing.discard(path)
            if e: return fail(e)
            sub.set_markup("<span foreground='#3fb950'>●</span> Added and connected")
            btn.set_label("Done")
            d = bz.objects.get(path, {}).get(DEV_IF, {})
            if is_audio(d.get("Icon")):
                threading.Thread(target=fix_audio_profile, args=(d.get("Address", ""),), daemon=True).start()
        bz.call(path, DEV_IF, "Pair", None, paired, 60000)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.win = None; self.rows = {}; self.add_dlg = None; self.powered = False
        self.tray = None; self.restarting = False; self.was_alive = True
        self.start_hidden = "--hidden" in sys.argv
        self.cfg = Config()
        self.script_path = os.path.abspath(__file__)
        self.settings_win = None
        self.visible_until = None   # monotonic time when "visible to others" ends (for the countdown)
        self.prev_powered = None

    def do_activate(self):
        if self.win:
            self.show_window(); return
        self.hold()  # keep running in the tray when the window is closed
        self.bz = Bluez(self.on_change)
        self.agent = Agent(self.bz, lambda: self.add_dlg or (self.win if self.win and self.win.get_visible() else None),
                           adding=lambda: self.add_dlg is not None)
        self.agent.register()
        self.win = Gtk.ApplicationWindow(application=self, title="Bluetooth")
        self.win.set_default_size(460, 520)
        self.win.set_icon_name("bluetooth")
        def on_close(*_):
            if self.tray and self.cfg["show_tray"]:
                self.win.hide(); return True   # keep running in the tray
            self.quit(); return True
        self.win.connect("delete-event", on_close)
        hb = Gtk.HeaderBar(show_close_button=True, title="Bluetooth")
        self.power = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Bluetooth on/off")
        self.power_handler = self.power.connect("notify::active", self.on_power)
        hb.pack_end(self.power)
        gear = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Settings")
        gear.add(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON))
        gear.connect("clicked", lambda *_: self.open_settings())
        hb.pack_start(gear)   # gear left, power right → "Bluetooth" sits centered between them
        self.win.set_titlebar(hb)
        ap, _ = self.bz.adapter()
        self.hci = ap.rsplit("/", 1)[-1] if ap else "hci0"
        try:
            self.obex = Obex(self)
        except GLib.Error as e:  # obexd missing: file sending just won't be offered
            print("[obex]", e.message, file=sys.stderr); self.obex = None
        GLib.timeout_add_seconds(10, self._tick)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        self.banner_box = Gtk.Box(spacing=10)
        self.banner_box.get_style_context().add_class("frame")
        self.banner = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
        self.banner_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.banner_btn.connect("clicked", self.on_banner_action)
        self.banner_box.pack_start(self.banner, True, True, 0)
        self.banner_box.pack_end(self.banner_btn, False, False, 0)
        self.banner_box.set_no_show_all(True)
        outer.pack_start(self.banner_box, False, False, 0)
        head = Gtk.Box()
        h = Gtk.Label(xalign=0); h.set_markup("<b>My devices</b>")
        head.pack_start(h, True, True, 0)
        add = Gtk.Button(label="＋ Add device"); add.get_style_context().add_class("suggested-action")
        add.connect("clicked", self.on_add); self.add_btn = add
        head.pack_end(add, False, False, 0)
        outer.pack_start(head, False, False, 0)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        frame = Gtk.Frame(); self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        # Divider under the last device too (the header func only draws lines *between* rows).
        css = Gtk.CssProvider()
        css.load_from_data(b"row.easybt-last { border-bottom: 1px solid alpha(currentColor, 0.12); }")
        Gtk.StyleContext.add_provider_for_screen(self.win.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.list.set_header_func(lambda row, before: row.set_header(Gtk.Separator() if before else None))
        self.list.set_sort_func(self._sort)
        self.list.set_placeholder(Gtk.Label(label="No devices yet. Click “Add device”.", margin=30))
        frame.add(self.list); sc.add(frame)
        outer.pack_start(sc, True, True, 0)
        self.toast_lbl = Gtk.Label(xalign=0, wrap=True); outer.pack_start(self.toast_lbl, False, False, 0)
        self.win.add(outer)
        self.setup_tray()
        self.on_change()
        # Wake from sleep → reconnect favorites (once).
        self.bz.bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                                     "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE,
                                     lambda c, s, p, i, n, params: (not params.unpack()[0]) and
                                     GLib.timeout_add_seconds(4, lambda: (self.autoconnect("wake"), False)[-1]))
        GLib.timeout_add_seconds(3, lambda: (self.autoconnect("startup"), False)[-1])
        self.win.show_all()
        if self.start_hidden and self.tray:
            self.win.hide()
        # Don't park keyboard focus on the first Connect button: a stray Enter/Space
        # (e.g. typed into another window as this one opens) would connect a device.
        self.win.set_focus(None)

    def _tick(self):
        """Every 10 s: refresh signal strength / sound mode for connected devices (off the UI thread)."""
        rows = [r for r in self.rows.values() if r.dev.get("Connected")]
        if rows:
            threading.Thread(target=lambda: [r.refresh_extras() for r in rows], daemon=True).start()
        return True

    def alias_for(self, address):
        for d in self.bz.devices():
            if d.get("Address", "").upper() == (address or "").upper():
                return d.get("Alias") or d.get("Name")
        return None

    def open_settings(self):
        if self.settings_win:
            self.settings_win.present(); return
        self.settings_win = SettingsWindow(self)

    # ---- settings actions ----
    def set_visible(self, on):
        ap, _ = self.bz.adapter()
        if not ap: return
        if on:
            self.visible_until = time.monotonic() + VISIBLE_SECONDS
            self.bz.set_prop(ap, ADP_IF, "DiscoverableTimeout", GLib.Variant("u", VISIBLE_SECONDS),
                             lambda e: self.bz.set_prop(ap, ADP_IF, "Discoverable", GLib.Variant("b", True),
                                                        lambda e2: e2 and self.toast(friendly(e2))))
        else:
            self.visible_until = None
            self.bz.set_prop(ap, ADP_IF, "Discoverable", GLib.Variant("b", False))

    def visible_left(self):
        if not self.visible_until: return None
        return max(0, int(self.visible_until - time.monotonic()))

    def set_receiving(self, on):
        self.cfg.set("receive_files", on)
        if self.obex:
            self.obex._claim() if on else self.obex.release()

    def set_tray_visible(self, on):
        if self.tray: self.tray.set_visible(on)

    def power_cycle(self):
        ap, _ = self.bz.adapter()
        if not ap: return
        self.bz.set_prop(ap, ADP_IF, "Powered", GLib.Variant("b", False),
                         lambda e: GLib.timeout_add(1500, lambda: (self.bz.set_prop(ap, ADP_IF, "Powered", GLib.Variant("b", True),
                                                    lambda e2: e2 and self.toast(friendly(e2))), False)[-1]))

    def is_trusted(self, address):
        return any(d.get("Trusted") for d in self.bz.devices() if d.get("Address", "").upper() == (address or "").upper())

    def autoconnect(self, why):
        """One connect attempt per favorite that's paired, not connected, not blocked. No retry loops."""
        if not (self.cfg["autoconnect_enabled"] and self.bz.alive and self.powered): return
        for r in list(self.rows.values()):
            d = r.dev
            if self.cfg.is_auto(d.get("Address", "")) and not d.get("Connected") and not d.get("Blocked") and not r.busy:
                r.set_busy(True, "Connecting automatically…")
                self.bz.call(r.path, DEV_IF, "Connect", None,
                             lambda e, r=r: (r.set_busy(False), e and r.update(r.dev)))

    def _sort(self, a, b):
        ka = (not a.dev.get("Connected"), (a.dev.get("Alias") or "").lower())
        kb = (not b.dev.get("Connected"), (b.dev.get("Alias") or "").lower())
        return (ka > kb) - (ka < kb)

    def toast(self, msg):
        self.toast_lbl.set_markup(f"<span foreground='#e5534b'>{GLib.markup_escape_text(msg)}</span>")
        GLib.timeout_add_seconds(8, lambda: (self.toast_lbl.set_text(""), False)[-1])

    def on_change(self, path=None):
        ap, a = self.bz.adapter()
        was_powered, was_alive_agent = self.prev_powered, getattr(self, "_alive_seen", True)
        self.powered = bool(a.get("Powered"))
        self.prev_powered = self.powered
        if was_powered is False and self.powered:
            GLib.timeout_add_seconds(2, lambda: (self.autoconnect("power on"), False)[-1])
        if self.bz.alive and not was_alive_agent:   # bluetoothd came back: re-register prompts, reconnect favorites
            self.agent.reg_id and self.bz.bus.unregister_object(self.agent.reg_id); self.agent.reg_id = None
            self.agent.register()
            GLib.timeout_add_seconds(2, lambda: (self.autoconnect("service back"), False)[-1])
        self._alive_seen = self.bz.alive
        disc = bool(a.get("Discoverable"))
        if getattr(self, "_was_disc", False) and not disc:   # only clear once it actually turned off
            self.visible_until = None
        if disc and self.visible_until is None:              # turned on elsewhere (e.g. bluetoothctl)
            t = a.get("DiscoverableTimeout") or 0
            self.visible_until = time.monotonic() + t if t else None
        self._was_disc = disc
        self.power.handler_block(self.power_handler)
        self.power.set_active(self.powered); self.power.set_sensitive(ap is not None)
        self.power.handler_unblock(self.power_handler)
        self.banner_action = None
        if not self.bz.alive:
            self.banner.set_markup("<b><span foreground='#e5534b'>Bluetooth service stopped.</span></b>\n"
                                   "Your devices can't connect until it's running again.")
            self.banner_action = "restart"
        elif ap is None:
            self.banner.set_markup("<b>No Bluetooth adapter found.</b>\nIt may be switched off (airplane mode) or stuck.")
            self.banner_action = "restart"
        elif not self.powered:
            self.banner.set_markup("Bluetooth is <b>off</b>.")
            self.banner_action = "power"
        else:
            self.banner.set_text("")
        if self.banner_action:
            self.banner_btn.set_label("Turn on" if self.banner_action == "power" else "Restart Bluetooth")
            self.banner_btn.set_sensitive(not self.restarting)
            self.banner_box.show_all()
        else:
            self.banner_box.hide()
        self.add_btn.set_sensitive(self.powered)
        paired = {d["path"]: d for d in self.bz.devices() if d.get("Paired")}
        for p in list(self.rows):
            if p not in paired:
                self.rows.pop(p).destroy()
        for p, d in paired.items():
            # Any device that just connected (button, tray, or the device reconnecting on its own):
            # make sure speakers/headphones are in high-quality mode.
            if p in self.rows and bool(d.get("Connected")) != bool(self.rows[p].dev.get("Connected")):
                row, just_on = self.rows[p], bool(d.get("Connected"))
                self.connection_changed(row, d, just_on)
                def work(row=row, d=d, just_on=just_on):
                    if just_on and is_audio(d.get("Icon")):
                        fix_audio_profile(d.get("Address", ""))
                    row.refresh_extras()
                GLib.timeout_add(300, lambda w=work: (threading.Thread(target=w, daemon=True).start(), False)[-1])
            if p in self.rows:
                self.check_battery(self.rows[p], d)
                self.rows[p].update(d)
            else:
                r = DeviceRow(self, d); self.rows[p] = r; self.list.add(r); r.show_all()
                if d.get("Connected"):
                    threading.Thread(target=r.refresh_extras, daemon=True).start()
        self.list.invalidate_sort()
        self.mark_last_row()
        if self.add_dlg: self.add_dlg.refresh()
        if getattr(self, "settings_sync", None): self.settings_sync()
        self.update_tray()  # last, so the icon/tooltip reflect the device list just rebuilt above

    def mark_last_row(self):
        """Tag whichever row sorts last (connected first, then A–Z) so it gets the bottom divider."""
        rows = sorted(self.rows.values(), key=lambda r: (not r.dev.get("Connected"), (r.dev.get("Alias") or "").lower()))
        for r in rows:
            ctx = r.get_style_context()
            (ctx.add_class if r is rows[-1] else ctx.remove_class)("easybt-last")

    def connection_changed(self, row, d, now_on):
        name = d.get("Alias") or "Device"
        if now_on:
            row.user_disconnect = False
            if self.cfg["notify_connects"]:
                self.notify(f"{name} connected", "")
            return
        dropped = not row.user_disconnect and self.powered and self.bz.alive
        row.user_disconnect = False
        if dropped and self.cfg["notify_drops"]:
            self.notify(f"{name} disconnected", "If you didn't turn it off, it may be out of range or low on battery.",
                        actions=[("Reconnect", lambda: row.on_toggle(None))])
        elif self.cfg["notify_connects"]:
            self.notify(f"{name} disconnected", "")

    def check_battery(self, row, d):
        b = d.get("battery")
        if b is None: return
        if b > 20: row.low_warned = False
        elif b <= 15 and not row.low_warned and self.cfg["low_battery"]:
            row.low_warned = True
            self.notify(f"{d.get('Alias') or 'Device'} battery low", f"{b}% left. Charge it soon.")

    def on_power(self, sw, _p):
        ap, _ = self.bz.adapter()
        if not ap: return
        on = sw.get_active()
        def done(e):
            if e and on:
                # Soft-blocked? Unblock once and retry.
                subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True)
                GLib.timeout_add(600, lambda: (self.bz.set_prop(ap, ADP_IF, "Powered", GLib.Variant("b", True),
                                               lambda e2: e2 and self.toast(friendly(e2))), False)[-1])
            elif e:
                self.toast(friendly(e))
        self.bz.set_prop(ap, ADP_IF, "Powered", GLib.Variant("b", on), done)

    # ---- window / tray ----
    def show_window(self):
        self.win.show(); self.win.present()

    def toggle_window(self):
        if self.win.get_visible() and self.win.is_active(): self.win.hide()
        else: self.show_window()

    def setup_tray(self):
        if XApp is None:
            return
        self.tray = XApp.StatusIcon()
        self.tray.set_name("easybt")
        self.tray.connect("activate", lambda icon, button, time: self.toggle_window())
        self.tray_menu = Gtk.Menu()
        self.tray.set_secondary_menu(self.tray_menu)
        self.tray.set_visible(self.cfg["show_tray"])

    def update_tray(self):
        if not self.tray:
            return
        connected = [r.dev.get("Alias") or "device" for r in self.rows.values() if r.dev.get("Connected")]
        if not self.bz.alive:
            icon, tip = "dialog-warning-symbolic", "Bluetooth service stopped. Click to fix."
        elif not self.powered:
            icon, tip = "bluetooth-disabled-symbolic", "Bluetooth is off"
        elif connected:
            icon, tip = "bluetooth-active-symbolic", "Connected: " + ", ".join(connected)
        else:
            icon, tip = "bluetooth-disconnected-symbolic", "Bluetooth on, nothing connected"
        self.tray.set_icon_name(icon); self.tray.set_tooltip_text(tip)
        # Tell the user once when the service dies / comes back (works even if the window is hidden)
        if self.was_alive and not self.bz.alive:
            self.notify("Bluetooth service stopped", "Open EasyBT and click Restart Bluetooth.")
        elif not self.was_alive and self.bz.alive:
            self.notify("Bluetooth is back", "The Bluetooth service is running again.")
        self.was_alive = self.bz.alive
        # Right-click menu: one line per device + power + open/quit
        for c in self.tray_menu.get_children(): c.destroy()
        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label); it.set_sensitive(sensitive)
            it.connect("activate", lambda *_: cb()); self.tray_menu.append(it)
        if not self.bz.alive:
            item("Restart Bluetooth", self.restart_service, not self.restarting)
        else:
            for r in sorted(self.rows.values(), key=lambda r: (not r.dev.get("Connected"), (r.dev.get("Alias") or "").lower())):
                on = r.dev.get("Connected")
                item(("✓ " if on else "    ") + (r.dev.get("Alias") or "device") + ("  (disconnect)" if on else ""),
                     lambda r=r: r.on_toggle(None), self.powered and not r.busy)
            self.tray_menu.append(Gtk.SeparatorMenuItem())
            vis = bool(self.bz.adapter()[1].get("Discoverable"))
            item("Hide from other devices" if vis else "Make visible to other devices (3 min)",
                 lambda: self.set_visible(not vis), self.powered)
            item("Turn Bluetooth off" if self.powered else "Turn Bluetooth on",
                 lambda: self.power.set_active(not self.powered))
        self.tray_menu.append(Gtk.SeparatorMenuItem())
        item("Open EasyBT", self.show_window)
        item("Settings…", self.open_settings)
        item("Quit", self.quit)
        self.tray_menu.show_all()

    def notify(self, title, body, actions=()):
        if Notify:
            n = Notify.Notification.new(title, body, "bluetooth")
            for i, (label, cb) in enumerate(actions):
                n.add_action(f"a{i}", label, lambda _n, _a, cb=cb: cb())
            if actions:
                n.set_timeout(15000)
            self._notes = [x for x in getattr(self, "_notes", [])[-5:]] + [n]  # keep alive so buttons work
            try:
                n.show(); return
            except GLib.Error:
                pass
        n = Gio.Notification.new(title); n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new("bluetooth"))
        self.send_notification("easybt-service", n)

    # ---- recovery ----
    def on_banner_action(self, _b):
        if self.banner_action == "power":
            self.power.set_active(True)
        else:
            self.restart_service()

    def restart_service(self):
        """Unblock the radio and restart bluetoothd (asks for your password once via polkit)."""
        if self.restarting: return
        self.restarting = True
        self.banner_btn.set_label("Restarting…"); self.banner_btn.set_sensitive(False)
        def work():
            subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True)
            r = subprocess.run(["pkexec", "systemctl", "restart", "bluetooth"], capture_output=True, text=True)
            GLib.idle_add(done, r.returncode, r.stderr.strip())
        def done(code, err):
            self.restarting = False
            if code != 0:
                self.toast("Couldn't restart Bluetooth" + (" (cancelled)" if code in (126, 127) else f": {err[:80]}"))
            self.on_change()
            return False
        threading.Thread(target=work, daemon=True).start()

    def on_add(self, _b):
        if self.add_dlg: self.add_dlg.present(); return
        self.add_dlg = AddDialog(self)


if __name__ == "__main__":
    if "--version" in sys.argv:
        from easybt_settings import VERSION
        print(f"EasyBT {VERSION}"); sys.exit(0)
    app = App()
    # Our own flag; strip it so Gtk.Application doesn't reject it as unknown.
    sys.exit(app.run([x for x in sys.argv if x != "--hidden"]))

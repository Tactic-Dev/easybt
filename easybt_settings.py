"""EasyBT settings: config storage + the Settings window (General / Local services / Features / Troubleshoot)."""
import json, os, shutil, subprocess, threading, time
from gi.repository import Gtk, Gio, GLib

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "easybt")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
AUTOSTART_DIR = os.path.join(GLib.get_user_config_dir(), "autostart")
EASYBT_AUTOSTART = os.path.join(AUTOSTART_DIR, "easybt.desktop")
BLUEMAN_AUTOSTART_SYS = "/etc/xdg/autostart/blueman.desktop"
BLUEMAN_AUTOSTART_USER = os.path.join(AUTOSTART_DIR, "blueman.desktop")
WP_MUSIC_FILE = os.path.join(GLib.get_user_config_dir(), "wireplumber", "policy.lua.d",
                             "51-easybt-no-headset-autoswitch.lua")
WP_MUSIC_BODY = """-- EasyBT: keep Bluetooth speakers/headphones in high-quality A2DP.
-- Default WirePlumber flips them into mono "call" (HFP) mode whenever any app opens a mic,
-- which sounds like a drop/garble. Toggle in EasyBT → Settings → Local services.
bluetooth_policy.policy["media-role.use-headset-profile"] = false
"""
VISIBLE_SECONDS = 180
VERSION = "1.1.0"  # keep in sync with the VERSION file and git tag

DEFAULTS = {
    "receive_files": True,          # accept incoming files (Object Push)
    "receive_dir": "",              # "" = Downloads
    "auto_accept_trusted": False,   # skip the prompt for my own (trusted) devices
    "notify_drops": True,           # a connected device disconnected without me clicking Disconnect
    "notify_connects": False,       # every connect/disconnect
    "low_battery": True,            # warn once at 15 %
    "autoconnect_enabled": True,    # reconnect favorites on startup / Bluetooth on / wake
    "autoconnect": [],              # MAC addresses marked "Connect automatically"
    "show_tray": True,
}


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(CONFIG_PATH) as f:
                self.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def set(self, key, value):
        self[key] = value
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def receive_dir(self):
        d = self.get("receive_dir")
        return d if d and os.path.isdir(d) else (
            GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or GLib.get_home_dir())

    def is_auto(self, mac):
        return mac.upper() in (m.upper() for m in self["autoconnect"])

    def set_auto(self, mac, on):
        macs = [m for m in self["autoconnect"] if m.upper() != mac.upper()]
        if on: macs.append(mac.upper())
        self.set("autoconnect", macs)


# ---- things that live outside our config file (login items, audio policy) ----
def start_at_login():
    return os.path.exists(EASYBT_AUTOSTART)

def launch_command(script_path):
    """Installed (deb / install.sh) → the `easybt` launcher on PATH; running from a checkout → that script."""
    launcher = shutil.which("easybt")
    return launcher if launcher else f"/usr/bin/python3 {script_path}"

def set_start_at_login(on, script_path):
    if on:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        with open(EASYBT_AUTOSTART, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=EasyBT\nComment=Bluetooth tray icon\n"
                    f"Exec={launch_command(script_path)} --hidden\nIcon=bluetooth\nTerminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n")
    elif os.path.exists(EASYBT_AUTOSTART):
        os.remove(EASYBT_AUTOSTART)

def blueman_tray_enabled():
    if not os.path.exists(BLUEMAN_AUTOSTART_SYS):
        return False
    try:
        with open(BLUEMAN_AUTOSTART_USER) as f:
            txt = f.read()
        return not ("Hidden=true" in txt or "X-GNOME-Autostart-enabled=false" in txt)
    except OSError:
        return True

def set_blueman_tray(on):
    """Per-user override of Blueman's login item (system file untouched), and stop/start it now."""
    if on:
        if os.path.exists(BLUEMAN_AUTOSTART_USER):
            os.remove(BLUEMAN_AUTOSTART_USER)
        if shutil.which("blueman-applet"):
            subprocess.Popen(["blueman-applet"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    else:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        with open(BLUEMAN_AUTOSTART_SYS) as f:
            txt = f.read()
        with open(BLUEMAN_AUTOSTART_USER, "w") as f:
            f.write(txt.rstrip("\n") + "\nHidden=true\n")
        subprocess.run(["pkill", "-f", "^/usr/bin/python3 /usr/bin/blueman-applet"], capture_output=True)

def music_quality_locked():
    return os.path.exists(WP_MUSIC_FILE)

def set_music_quality_locked(on):
    if on:
        os.makedirs(os.path.dirname(WP_MUSIC_FILE), exist_ok=True)
        with open(WP_MUSIC_FILE, "w") as f:
            f.write(WP_MUSIC_BODY)
    elif os.path.exists(WP_MUSIC_FILE):
        os.remove(WP_MUSIC_FILE)
    # WirePlumber only reads policy at start; restart it (sound blips for ~1 s).
    threading.Thread(target=lambda: subprocess.run(["systemctl", "--user", "restart", "wireplumber"],
                                                   capture_output=True), daemon=True).start()

def bluez_reconnect_attempts():
    try:
        with open("/etc/bluetooth/main.conf") as f:
            for line in f:
                s = line.strip()
                if s.startswith("ReconnectAttempts") and "=" in s:
                    return int(s.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 7  # BlueZ default


# ---- UI helpers ----
def _row(title, subtitle=None, widget=None):
    """A settings row: bold-ish title, dim subtitle, control on the right."""
    box = Gtk.Box(spacing=16, margin_top=10, margin_bottom=10, margin_start=14, margin_end=14)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    t = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=42)
    text.pack_start(t, False, False, 0)
    sub = None
    if subtitle is not None:
        sub = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        sub.set_markup(f"<small>{subtitle}</small>")
        sub.get_style_context().add_class("dim-label")
        text.pack_start(sub, False, False, 0)
    box.pack_start(text, True, True, 0)
    if widget is not None:
        widget.set_valign(Gtk.Align.CENTER)
        box.pack_end(widget, False, False, 0)
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    row.add(box)
    row.sub = sub
    return row

def _group(title, rows):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    h = Gtk.Label(xalign=0); h.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
    box.pack_start(h, False, False, 0)
    lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lb.set_header_func(lambda r, before: r.set_header(Gtk.Separator() if before else None))
    for r in rows: lb.add(r)
    fr = Gtk.Frame(); fr.add(lb)
    box.pack_start(fr, False, False, 0)
    return box

def _switch(active, on_toggle):
    sw = Gtk.Switch(active=active)
    sw.connect("notify::active", lambda s, _p: on_toggle(s.get_active()))
    return sw

def _page(*groups):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin=18)
    for g in groups: box.pack_start(g, False, False, 0)
    sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    sc.add(box)
    return sc


class SettingsWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="EasyBT settings", transient_for=app.win if app.win.get_visible() else None)
        self.app = app
        self.set_default_size(680, 520)
        self.set_icon_name("bluetooth")
        # Same client-side header bar as the main window (dark, rounded, title centered).
        hb = Gtk.HeaderBar(show_close_button=True, title="Settings")
        self.set_titlebar(hb)
        self.connect("destroy", self._closed)
        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        side = Gtk.StackSidebar(stack=stack); side.set_size_request(170, -1)
        # The sidebar paints its own (square) background over the window's rounded bottom-left corner.
        # Round it to match the theme's window corner (Orchis uses 18px; harmless on other themes).
        side.set_name("easybt-sidebar")
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            #easybt-sidebar, #easybt-sidebar scrolledwindow, #easybt-sidebar viewport,
            #easybt-sidebar list { border-bottom-left-radius: 18px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        paned = Gtk.Box(); paned.pack_start(side, False, False, 0)
        paned.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        paned.pack_start(stack, True, True, 0)
        self.add(paned)
        stack.add_titled(self._general(), "general", "General")
        stack.add_titled(self._services(), "services", "Local services")
        stack.add_titled(self._features(), "features", "Features")
        stack.add_titled(self._troubleshoot(), "trouble", "Troubleshoot")
        self.timer = GLib.timeout_add_seconds(1, self.sync)
        self.sync()
        self.show_all()

    def _closed(self, *_):
        GLib.source_remove(self.timer)
        self.app.settings_win = None

    # ---------- General ----------
    def _general(self):
        app = self.app
        ap, a = app.bz.adapter()
        self.name = Gtk.Entry(text=a.get("Alias") or a.get("Name") or "", width_chars=18)
        def save_name(*_):
            v = self.name.get_text().strip()
            ap2, a2 = app.bz.adapter()
            if ap2 and v and v != a2.get("Alias"):
                app.bz.set_prop(ap2, "org.bluez.Adapter1", "Alias", GLib.Variant("s", v))
        self.name.connect("activate", save_name)
        self.name.connect("focus-out-event", lambda *_: (save_name(), False)[-1])
        self.vis = Gtk.Switch()
        self.vis_handler = self.vis.connect("notify::active", lambda s, _p: app.set_visible(s.get_active()))
        self.vis_row = _row("Let other devices find this laptop", "", self.vis)
        login = _switch(start_at_login(), lambda on: set_start_at_login(on, app.script_path))
        startup = [_row("Start EasyBT when I log in", "Runs quietly in the tray so reconnects, file receiving and warnings work.", login)]
        if os.path.exists(BLUEMAN_AUTOSTART_SYS):  # only offered when Blueman is installed
            blue = _switch(not blueman_tray_enabled(), lambda on: set_blueman_tray(not on))
            startup.append(_row("Hide Blueman's tray icon",
                                "Recommended. EasyBT does everything it did. Blueman's apps stay installed.", blue))
        return _page(
            _group("This laptop", [
                _row("Bluetooth name", "What phones and speakers see when they look for this laptop.", self.name),
                self.vis_row,
            ]),
            _group("Startup", startup),
        )

    # ---------- Local services ----------
    def _services(self):
        app, cfg = self.app, self.app.cfg
        recv = _switch(cfg["receive_files"], app.set_receiving)
        folder = Gtk.FileChooserButton(title="Save received files to", action=Gtk.FileChooserAction.SELECT_FOLDER)
        folder.set_filename(cfg.receive_dir())
        folder.connect("file-set", lambda b: cfg.set("receive_dir", b.get_filename() or ""))
        auto = _switch(cfg["auto_accept_trusted"], lambda on: cfg.set("auto_accept_trusted", on))
        self.recv_row = _row("Receive files", "", recv)
        music = _switch(music_quality_locked(), set_music_quality_locked)
        n = bluez_reconnect_attempts()
        return _page(
            _group("Files (Object Push)", [
                self.recv_row,
                _row("Save them to", None, folder),
                _row("Accept automatically from my trusted devices",
                     "Your own paired phone/tablet skips the Accept prompt. Others still ask.", auto),
            ]),
            _group("Audio", [
                _row("Keep speakers and headphones in music quality",
                     "Stops apps that open a mic (Zoom, Chrome) from switching them to muffled call mode. "
                     "Turn off to use a headset's mic. Sound blips for a second when changed.", music),
            ]),
            _group("Connections", [
                _row("Reconnect after a drop",
                     (f"On. Bluetooth retries {n} times over about 2 minutes when a device drops out."
                      if n else "Off in /etc/bluetooth/main.conf (ReconnectAttempts = 0)."), None),
                _row("Pairing requests from other devices",
                     "Always ask. A device that isn't paired can't connect or send files without your OK.", None),
            ]),
        )

    # ---------- Features (plugins) ----------
    def _features(self):
        app, cfg = self.app, self.app.cfg
        def toggle(key, after=None):
            return _switch(cfg[key], lambda on: (cfg.set(key, on), after and after(on)))
        return _page(
            _group("Features", [
                _row("Tray icon", "Status at a glance; right-click for quick connect. Turn off and EasyBT quits when you close its window.",
                     toggle("show_tray", app.set_tray_visible)),
                _row("Auto-connect favorites",
                     "Devices marked “Connect automatically” (in their ⋯ menu) connect when EasyBT starts, "
                     "Bluetooth turns on, or the laptop wakes. One try each time, never a constant retry loop.",
                     toggle("autoconnect_enabled")),
                _row("Tell me when a device drops", "A notification with a Reconnect button when something disconnects that you didn't disconnect.",
                     toggle("notify_drops")),
                _row("Tell me about every connect and disconnect", None, toggle("notify_connects")),
                _row("Low battery warning", "Once per charge, when a device reports 15% or less.", toggle("low_battery")),
            ]),
        )

    # ---------- Troubleshoot ----------
    def _troubleshoot(self):
        app = self.app
        self.diag = Gtk.Label(xalign=0, selectable=True, wrap=True, max_width_chars=60)
        restart = Gtk.Button(label="Restart Bluetooth")
        restart.connect("clicked", lambda *_: app.restart_service())
        cycle = Gtk.Button(label="Turn off and on")
        cycle.connect("clicked", lambda *_: app.power_cycle())
        copy = Gtk.Button(label="Copy details")
        copy.connect("clicked", lambda *_: Gtk.Clipboard.get_default(self.get_display()).set_text(self.diag.get_text(), -1))
        btns = Gtk.Box(spacing=8)
        for b in (cycle, restart, copy): btns.pack_start(b, False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=14)
        box.pack_start(self.diag, False, False, 0)
        tips = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        tips.set_markup("<small><b>Something won't connect?</b> 1) Turn it off and on. 2) Use “Turn off and on” here. "
                        "3) “Restart Bluetooth”. 4) Forget it and add it again in pairing mode.</small>")
        tips.get_style_context().add_class("dim-label")
        fr = Gtk.Frame(); fr.add(box)
        return _page(_group("Status", []), fr, btns, tips)

    # ---------- live refresh (1 s) ----------
    def sync(self):
        app = self.app
        ap, a = app.bz.adapter()
        # visibility + countdown
        on = bool(a.get("Discoverable"))
        self.vis.handler_block(self.vis_handler); self.vis.set_active(on); self.vis.handler_unblock(self.vis_handler)
        self.vis.set_sensitive(bool(ap) and app.powered)
        left = app.visible_left()
        self.vis_row.sub.set_markup("<small>" + (
            f"Visible now · hides itself in {left // 60}:{left % 60:02d}" if on and left is not None else
            "Visible now." if on else
            "Only needed when pairing from the other device. Turns off by itself after 3 minutes.") + "</small>")
        # receiving
        ob = app.obex
        self.recv_row.sub.set_markup("<small>" + (
            "Unavailable: the Bluetooth file service (obexd) isn't installed." if not ob else
            "Off. Phones get “refused” if they try to send." if not app.cfg["receive_files"] else
            "On. You'll be asked before anything is saved." if ob.receiving else
            "Waiting for another app to release file receiving. EasyBT takes over automatically.") + "</small>")
        # diagnostics
        devs = app.bz.devices()
        lines = [
            f"EasyBT {VERSION}",
            f"Bluetooth service: {'running' if app.bz.alive else 'STOPPED'}",
            f"Adapter: {a.get('Alias', '—')} ({a.get('Address', 'none found')})" if ap else "Adapter: none found",
            f"Powered: {'yes' if a.get('Powered') else 'no'} · Visible: {'yes' if on else 'no'} · Pairable: {'yes' if a.get('Pairable') else 'no'}",
            f"Paired devices: {sum(1 for d in devs if d.get('Paired'))} · Connected: {sum(1 for d in devs if d.get('Connected'))}",
            f"File receiving: {'EasyBT' if ob and ob.receiving else 'not EasyBT'} · Pairing prompts: {'EasyBT' if app.agent.reg_id else 'other app'}",
            f"Reconnect attempts after a drop: {bluez_reconnect_attempts()}",
        ]
        self.diag.set_text("\n".join(lines))
        return True

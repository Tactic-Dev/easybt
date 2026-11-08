# Changelog

## 1.2.0 (2026-11-07)
- Main window is now a rounded tray panel (matches the other Easy* panels), opens above the tray icon and closes when you click away.
- Device rows mention signal only when it's OK or weak.

## 1.1.0 (2026-11-07)
- Only one tray panel open at a time across the TACTICDEV Easy* apps (opening one closes the others).
- `restart` action so the app can restart itself inside the desktop session.

## 1.0.0 (2026-11-07)
First release. A simple, reliable Bluetooth manager for Linux Mint / Cinnamon (GTK3 + BlueZ D-Bus).

- Device list: one Connect/Disconnect button each, plain-English errors, signal strength, battery badge.
- Add device: one-click pair + trust + connect; always-on pairing agent that asks before unknown devices connect.
- Per-device ⋯ menu: Music/Calls sound mode, Send file, Details, Rename, Connect automatically,
  Use its internet (tethering), Block/Unblock, Forget.
- File receiving (OBEX): accept prompt, progress + speed, Open / Show in folder, notification.
- Tray icon with quick connect, visibility toggle, and a warning + Restart when the Bluetooth service stops.
- Settings: General, Local services, Features, Troubleshoot.
- Auto-connect favorites on startup / Bluetooth on / wake (one try each, no retry loop).
- Notifications for unexpected drops (with Reconnect) and low battery.
- Built-in signal-strength reader (no Blueman dependency).
- Packaging: `.deb` for Debian/Ubuntu/Mint, per-user `install.sh`/`uninstall.sh`, `easybt --version`.

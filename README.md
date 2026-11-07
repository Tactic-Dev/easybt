# EasyBT 1.0.0

Simple Bluetooth manager for Linux (GTK3 + BlueZ D-Bus). Search **EasyBT** in the menu, or run
`python3 ~/Dev/easybt/easybt.py`.

- One list of your devices: one Connect/Disconnect button, battery when the device reports it.
- Plain-English errors ("Couldn't reach it. Make sure the speaker is on and nearby.").
- Add device: shows only named devices; one click pairs, trusts and connects.
- Speakers/headphones that come up in muffled call mode are switched to A2DP (high quality).
- Live updates from BlueZ signals (no polling); async calls with timeouts so the window never freezes.
- ⋯ menu per device: Music / Calls sound mode, Send file…, Details…, Rename, Block/Unblock, Forget.
- Signal (Strong/OK/Weak) for connected devices, refreshed every 10 s.
- ⚙ settings: laptop's Bluetooth name; "let other devices find this laptop" (auto-off after 3 min).
- Receives files itself (accept/decline prompt, saved to Downloads, progress + speed). Blueman's
  TransferService plugin is disabled (`gsettings get org.blueman.general plugin-list`) so it can't hold
  the receive slot; EasyBT re-claims it automatically if obexd restarts.
- Tray icon (XApp) with per-device quick connect; warns + offers Restart when bluetoothd stops.
- Pairing agent is only registered while "Add device" is open.

No dependencies beyond what Linux Mint ships (python3-gi, GTK3, bluez, pactl).

## Settings (⚙ or tray → Settings…)
- **General:** Bluetooth name; "let other devices find this laptop" (3-minute countdown); start at login; hide Blueman's tray icon.
- **Local services:** receive files on/off, save folder, auto-accept from trusted devices; keep speakers in music quality; reconnect-after-drop status; pairing prompts.
- **Features:** tray icon, auto-connect favorites (startup / Bluetooth on / wake, one try each), drop notifications with Reconnect, connect/disconnect notifications, low battery warning.
- **Troubleshoot:** live status, Turn off and on, Restart Bluetooth, Copy details.

Per device (⋯): Connect automatically, Use its internet (phones with Bluetooth tethering).
Config lives in `~/.config/easybt/config.json`.

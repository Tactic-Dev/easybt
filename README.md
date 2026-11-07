# EasyBT

A simple, reliable Bluetooth manager for Linux desktops. One window, your devices, one button each.

EasyBT is a GTK3 front end for BlueZ, the standard Linux Bluetooth stack. It's built for Linux Mint and
Ubuntu and works alongside Blueman or replaces it completely.

## Features

- **Your devices at a glance:** one Connect/Disconnect button per device, signal strength, battery level.
- **Errors in plain English:** "Couldn't reach it. Make sure the speaker is on and nearby." instead of
  `br-connection-page-timeout`.
- **One-click pairing:** Add device shows only devices with a name; one click pairs, trusts and connects.
- **Sound that stays good:** speakers and headphones are kept in high-quality music mode, with a simple
  Music / Calls switch when you need a headset's mic.
- **Files both ways:** send files to phones, and receive files with an Accept prompt, a progress bar,
  and Open / Show in folder buttons.
- **Tray icon:** status at a glance, quick connect from the right-click menu, and a warning with a
  one-click fix if the Bluetooth service stops.
- **Auto-connect favorites:** they connect when EasyBT starts, when Bluetooth turns on, or when the
  laptop wakes. Each event gets one try; there is no endless retry loop.
- **Notifications:** when a device drops unexpectedly (with a Reconnect button) and when a battery runs low.
- **Per-device menu:** Details, Rename, Connect automatically, Use its internet (phone tethering),
  Block, Forget.
- **Settings:** Bluetooth name, a "let others find this laptop" switch that turns itself off after 3
  minutes, start at login, file receiving, features, and a Troubleshoot page with Restart Bluetooth.

## Install

### Debian, Ubuntu, Linux Mint (recommended)

Download the latest `easybt_*_all.deb` from
[Releases](https://github.com/Tactic-Dev/easybt/releases/latest), then:

```sh
sudo apt install ./easybt_*_all.deb
```

Open **EasyBT** from the app menu. To have it start with your desktop, turn on
⚙ → General → _Start EasyBT when I log in_.

### Any distro, no root

Install the dependencies with your package manager: Python 3 GObject bindings, GTK 3, BlueZ, obexd and
`pactl`. XApp and libnotify are optional and add the tray icon and notifications. Then:

```sh
git clone https://github.com/Tactic-Dev/easybt.git
cd easybt
./install.sh      # installs to ~/.local; ./uninstall.sh removes it
```

### Run without installing

```sh
python3 easybt.py
```

## Requirements

- Linux with BlueZ 5.x, and `bluez-obexd` for file transfer.
- GTK 3 with PyGObject.
- A tray that supports XApp status icons, used for the tray icon: Cinnamon, MATE, Xfce, and others.
  Without one, EasyBT runs as a normal window.
- PipeWire or PulseAudio, through `pactl`, for the Music / Calls switch.

## Recommended system tweak

Some distros ship BlueZ with automatic reconnection turned off, so a device that drops out stays
disconnected. If ⚙ → Troubleshoot shows **Reconnect attempts after a drop: 0**, edit
`/etc/bluetooth/main.conf` and under `[Policy]` set:

```ini
ReconnectAttempts = 7
ReconnectIntervals = 1,2,4,8,16,32,64
```

Then run `sudo systemctl restart bluetooth`.

## Removing Blueman

EasyBT handles everything Blueman's tray applet does: pairing prompts, receiving files, the tray
icon and reconnects. Once EasyBT is set to start at login, you can remove Blueman:

```sh
sudo apt remove blueman
```

## Known limitations

- The "keep music quality" switch writes a WirePlumber 0.4 policy file, the version used by Ubuntu
  24.04 and Mint 22. WirePlumber 0.5+ uses a different config format, so the switch has no effect there yet.
- Sharing this computer's internet over Bluetooth (NAP server) and serial ports aren't supported.

## Files

| Path                                 | What                                                                       |
| ------------------------------------ | -------------------------------------------------------------------------- |
| `~/.config/easybt/config.json`       | Your settings                                                              |
| `~/.config/autostart/easybt.desktop` | Start-at-login entry (created by the setting)                              |
| `~/.cache/obexd/`                    | Where incoming files land briefly before being moved to your chosen folder |

## License

MIT © TACTICDEV. See [LICENSE](LICENSE).

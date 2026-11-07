#!/bin/sh
# Per-user install (no root): ~/.local/share/easybt, ~/.local/bin/easybt, app menu entry.
# System packages it needs (Debian/Ubuntu/Mint names):
#   python3-gi gir1.2-gtk-3.0 bluez bluez-obexd pulseaudio-utils  (+ gir1.2-xapp-1.0 gir1.2-notify-0.7 for tray/notifications)
set -eu
cd "$(dirname "$0")"
HOME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/easybt"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$HOME_DIR" "$BIN" "$APPS"
cp easybt.py easybt_settings.py VERSION "$HOME_DIR/"
sed "s|@EASYBT_HOME@|$HOME_DIR|" packaging/easybt.sh > "$BIN/easybt"; chmod 755 "$BIN/easybt"
sed "s|^Exec=easybt|Exec=$BIN/easybt|" packaging/easybt.desktop > "$APPS/easybt.desktop"
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null \
  || echo "warning: GTK3 Python bindings missing: sudo apt install python3-gi gir1.2-gtk-3.0"
command -v bluetoothctl >/dev/null || echo "warning: BlueZ missing: sudo apt install bluez bluez-obexd"
echo "Installed EasyBT $(cat VERSION). Open it from the app menu, or run: $BIN/easybt"

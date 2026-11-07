#!/bin/sh
# Removes a per-user install (install.sh). Your settings in ~/.config/easybt are kept.
set -eu
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/easybt"
rm -f "$HOME/.local/bin/easybt" "${XDG_DATA_HOME:-$HOME/.local/share}/applications/easybt.desktop" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/easybt.desktop"
echo "EasyBT removed. Settings kept in ${XDG_CONFIG_HOME:-$HOME/.config}/easybt (delete it to reset)."

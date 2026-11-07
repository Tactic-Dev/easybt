#!/bin/sh
# Build dist/easybt_<version>_all.deb   (needs: dpkg-deb)
set -eu
cd "$(dirname "$0")/.."
VERSION=$(cat VERSION)
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

install -Dm644 easybt.py          "$ROOT/usr/share/easybt/easybt.py"
install -Dm644 easybt_settings.py "$ROOT/usr/share/easybt/easybt_settings.py"
install -d -m755 "$ROOT/usr/bin"
sed 's|@EASYBT_HOME@|/usr/share/easybt|' packaging/easybt.sh > "$ROOT/usr/bin/easybt"
chmod 755 "$ROOT/usr/bin/easybt"
install -Dm644 packaging/easybt.desktop "$ROOT/usr/share/applications/easybt.desktop"
install -Dm644 LICENSE   "$ROOT/usr/share/doc/easybt/copyright"
install -Dm644 README.md "$ROOT/usr/share/doc/easybt/README.md"
gzip -9n -c CHANGELOG.md > "$ROOT/usr/share/doc/easybt/changelog.gz"

find "$ROOT" -type d -exec chmod 755 {} +
mkdir -p "$ROOT/DEBIAN"
SIZE=$(du -sk "$ROOT/usr" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CTRL
Package: easybt
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, bluez, bluez-obexd, pulseaudio-utils, rfkill, pkexec | policykit-1
Recommends: gir1.2-xapp-1.0, gir1.2-notify-0.7
Maintainer: TACTICDEV <noreply@github.com>
Homepage: https://github.com/Tactic-Dev/easybt
Description: Simple, reliable Bluetooth manager
 One window with your devices and one Connect button each, plain-English
 errors, one-click pairing, file sending/receiving, a tray icon, sound-mode
 switching for headphones, auto-connect favorites and drop notifications.
 GTK3 front end for BlueZ; works alongside or instead of Blueman.
CTRL
mkdir -p dist
dpkg-deb --root-owner-group --build "$ROOT" "dist/easybt_${VERSION}_all.deb"
echo "built dist/easybt_${VERSION}_all.deb"

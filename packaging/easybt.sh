#!/bin/sh
# EasyBT launcher. Installed to /usr/bin/easybt (deb) or ~/.local/bin/easybt (install.sh).
# EASYBT_HOME is substituted at install time.
exec /usr/bin/python3 "@EASYBT_HOME@/easybt.py" "$@"

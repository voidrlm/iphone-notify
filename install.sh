#!/usr/bin/env bash
# Installs the iphone-notify daemon (systemd user service) and GNOME extension.
set -euo pipefail
cd "$(dirname "$0")"

UUID="iphone-notify@arjun"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

python3 -c 'import dbus, gi' 2>/dev/null || sudo dnf install -y python3-dbus python3-gobject

# Daemon
install -Dm755 iphone_notify.py "$HOME/.local/bin/iphone-notify"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/iphone-notify.service" <<EOF
[Unit]
Description=Mirror iPhone notifications to the desktop
After=graphical-session.target bluetooth.target
PartOf=graphical-session.target

[Service]
ExecStart=%h/.local/bin/iphone-notify
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user enable iphone-notify.service
systemctl --user restart iphone-notify.service

# GNOME extension
mkdir -p "$EXT_DIR"
install -m644 extension/metadata.json extension/extension.js extension/stylesheet.css "$EXT_DIR/"
if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "Extension enabled. If the tile doesn't appear, log out and back in."
else
    # The running shell hasn't seen the new extension yet (Wayland only scans
    # at login), so add it to the enabled list for the next login.
    current=$(gsettings get org.gnome.shell enabled-extensions)
    case "$current" in
        *"'$UUID'"*) ;;
        "@as []"|"[]") gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
        *) gsettings set org.gnome.shell enabled-extensions "${current%]}, '$UUID']" ;;
    esac
    echo "Extension installed; log out and back in to load it."
fi

echo "Done. Daemon logs: journalctl --user -u iphone-notify -f"

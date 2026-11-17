#!/bin/sh
# Copy PowerDeck to the Cardputer Zero and install it.
# Usage: ./install.sh user@host   (the Cardputer's SSH login)
set -e
HOST=${1:?usage: ./install.sh user@host}
cd "$(dirname "$0")"
ssh "$HOST" 'mkdir -p ~/powerdeck'
scp -q powerdeck.py make_icon.py powerdeck.desktop test_ui.py "$HOST":powerdeck/
ssh "$HOST" '
set -e
cd ~/powerdeck
sudo install -d /opt/powerdeck
sudo install -m 755 powerdeck.py /opt/powerdeck/
python3 make_icon.py /tmp/powerdeck.png
sudo install -m 644 /tmp/powerdeck.png /usr/share/APPLaunch/share/images/powerdeck.png
sudo install -m 644 powerdeck.desktop /usr/share/APPLaunch/applications/powerdeck.desktop
systemctl --user restart APPLaunch.service
echo installed
'

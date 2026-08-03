#!/bin/sh
# launcher.sh
# navigate to home directory, then to this directory, then execute python script, then back home
#
# No sudo here (unlike modulino-hid-bridge-arcade-emulator's injector.py, which needs root to
# open /dev/uinput for writing synthetic HID events): reading /dev/input/eventN only requires
# membership in the `input` group, and `id arduino` on this board already shows gid 995 (input)
# in the arduino user's group list. If a future kernel/udev rule tightens permissions on
# /dev/input/eventN such that group membership stops being sufficient, add `sudo` back here and
# note the concrete error that forced it.

cd /
cd ~/ArduinoApps/techaq/host
python3 scanner_reader.py
cd /

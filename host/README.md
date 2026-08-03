# TechaQ host-side scripts

Scripts in this directory run **outside** the App Lab Docker container, directly on the Arduino
UNO Q board's host Debian OS. They exist because certain hardware is not reachable from inside
the app's container.

## Barcode scanner setup

TechaQ scans book EAN/ISBN barcodes with a generic USB or Bluetooth HID barcode scanner (any
HID-compliant scanner; tested with an "Eyoyo mini"). These scanners work in standard
"keyboard-wedge" mode: they enumerate as a HID keyboard and type the decoded barcode digits,
then send Enter.

**Why this can't live inside the app's container:** every App Lab app with a sketch gets an
identical fixed Docker volume/cgroup baseline. It bind-mounts `/dev` and grants access to
character-device majors 226/250/504/81/116 plus group membership for video/audio/render/users/
gpiod -- but not the `input` group (gid 995) or the input-event character-device major (13),
and there's no `app.yaml` key that changes this. So `/dev/input/eventN` (where scanner keystrokes
land) is invisible from inside the container. `scanner_reader.py` therefore runs on the host OS
as a systemd user service -- the same pattern used by
`modulino-hid-bridge-arcade-emulator/host/injector.py` for the mirror-image problem (writing HID
output via `/dev/uinput` instead of reading HID input).

### What `scanner_reader.py` does

1. Enumerates `/dev/input/event*` via `evdev.list_devices()`.
2. Skips built-in devices by name (`gpio-keys`, `pm8941_pwrkey`,
   `Arduino-Imola-HPH-LOUT Headset Jack` -- confirmed via
   `cat /proc/bus/input/devices` on this board), plus anything matching the user's exclusion
   list (see below).
3. Spawns one thread per remaining candidate device, reading `EV_KEY` events and accumulating
   digits/letters into a buffer.
4. On `Enter` (or `Tab`), treats the buffer as a completed scan, clears it, and `POST`s
   `{"code": "<scanned string>", "device": "<device name>"}` as JSON to
   `http://127.0.0.1:7000/api/scan` -- the running TechaQ app's own WebUI REST endpoint (wired up
   separately in `python/main.py`). A failed POST (app not running yet, etc.) is logged and
   ignored; it never crashes the reader loop.
5. Handles `SIGINT`/`SIGTERM` cleanly so `systemctl --user stop` exits without hanging.

### Excluding a device (e.g. a real attached keyboard)

`scanner_reader.py` reads a plain JSON list of device-name substrings (case-insensitive) from:

```
~/.config/techaq/excluded_devices.json
```

Example:

```json
["Logitech", "My Keyboard"]
```

If the file doesn't exist, the exclusion list is treated as empty. **This file is only read once,
at startup** -- device enumeration happens once when the script starts (matching the injector
precedent's simplicity), so after editing the exclusion list, restart the service:

```bash
systemctl --user restart techaq-scanner.service
```

A Settings UI page is expected to write this file for the user; this script only ever reads it.

### Scanner service setup

The reader runs as a systemd user service on the UNO Q, the same shape as
`modulino-hid-bridge-arcade-emulator`'s injector service.

**1. Install the `evdev` dependency.** Try these in order (documented here because `pip`/
`ensurepip` are not present on this board's system Python by default, so the "obvious" `pip
install evdev` may not work out of the box):

- `python3 -m pip install --user evdev` -- works if `pip` is present.
- `python3 -m venv ~/.techaq-venv && ~/.techaq-venv/bin/pip install evdev`, then point
  `ExecStart` below at `~/.techaq-venv/bin/python3` instead of the system `python3` -- only
  works if `python3-venv`/`ensurepip` is installed; on a stock trixie image without
  `python3.13-venv`, venv creation itself fails with "ensurepip is not available" and this
  route is a dead end too.
- `sudo apt-get install python3-evdev` -- confirmed available in this board's apt repos
  (`apt-cache policy python3-evdev` shows candidate `1.9.1-1` from the trixie repo) and installs
  system-wide without needing pip at all. **This requires an interactive sudo password** (the
  `arduino` user has general `sudo ALL` rights but no passwordless rule for `apt-get install
  python3-evdev` specifically), so a human operator needs to run this once by hand during setup:

  ```bash
  sudo apt-get install python3-evdev
  ```

If none of the above work in your environment, install `python3-pip` or `python3-venv` first
(also via `sudo apt-get install python3-pip` / `python3.13-venv`, same interactive-password
caveat) and retry.

**2. Create the service file:**

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/techaq-scanner.service
```

Paste:

```ini
[Unit]
Description=TechaQ barcode scanner reader (evdev bridge)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/arduino/ArduinoApps/techaq/host/scanner_reader.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

(If you installed `evdev` into the venv from step 1 instead of system-wide, change `ExecStart`
to `/home/arduino/.techaq-venv/bin/python3 /home/arduino/ArduinoApps/techaq/host/scanner_reader.py`.)

**3. Enable and start it:**

```bash
systemctl --user daemon-reload
systemctl --user enable techaq-scanner.service
systemctl --user start techaq-scanner.service
```

**4. Verify:**

```bash
systemctl --user status techaq-scanner.service
journalctl --user -u techaq-scanner.service -f
```

Plug in a scanner and scan a barcode; you should see `scan complete from <device>: <code>` in
the log, followed by a POST to `http://127.0.0.1:7000/api/scan` (which will fail with a logged
error until the TechaQ app itself is running and that endpoint exists).

### Why `launcher.sh` doesn't use `sudo`

Unlike `modulino-hid-bridge-arcade-emulator/host/injector.py` (which needs root to open
`/dev/uinput` for *writing* synthetic HID events), reading `/dev/input/eventN` only requires
membership in the `input` group. `id arduino` on this board already shows gid `995` (`input`) in
the `arduino` user's groups, so `launcher.sh` runs `scanner_reader.py` directly without `sudo`.

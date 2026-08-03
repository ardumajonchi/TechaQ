#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Host-side (NOT run inside the App Lab Docker container) HID barcode-scanner reader.

USB/Bluetooth HID input devices (/dev/input/eventN) are not reachable from inside the app's
Docker container -- the board's fixed App Lab volume/cgroup baseline bind-mounts /dev and grants
character-device access for majors 226/250/504/81/116 (video/audio/render/gpiod-ish groups), but
not the input-event major (13) or the `input` group (gid 995). So, mirroring the approach used by
modulino-hid-bridge-arcade-emulator's host/injector.py (which solves the mirror-image problem --
writing HID output via /dev/uinput from outside the container), this script runs directly on the
board's host Debian OS as a systemd user service and reads scanner keystrokes from /dev/input
directly, then relays completed scans to the containerized TechaQ app over loopback HTTP.

Generic HID barcode scanners (tested with an "Eyoyo mini") operate in standard keyboard-wedge
mode: they enumerate as a USB/Bluetooth HID keyboard and "type" the decoded barcode content one
character at a time, then send Enter (occasionally Tab). This script watches every /dev/input
event device that isn't in a built-in-device blocklist or a user-configured exclusion list,
accumulates digits (and best-effort letters) between keydowns, and on Enter/Tab treats the
buffer as a completed scan and POSTs it to the running TechaQ app's own WebUI REST port.

Does NOT hot-reload the exclusion file or re-enumerate devices while running -- evdev device
discovery happens once at startup, matching the simplicity of the injector precedent. Plug in a
new scanner or edit the exclusion list, then restart the service (`systemctl --user restart
techaq-scanner.service`) to pick it up.
"""

import json
import os
import signal
import threading
import time
import urllib.request
import urllib.error

from evdev import InputDevice, categorize, ecodes as E, list_devices

# Built-in devices on this board that are never a barcode scanner (confirmed via
# `cat /proc/bus/input/devices` over adb). Case-insensitive substring match.
BUILTIN_BLOCKLIST = [
    "gpio-keys",
    "pm8941_pwrkey",
    "arduino-imola-hph-lout headset jack",
]

# User-configurable exclusion list (e.g. a real attached keyboard the user wants ignored).
# Plain JSON list of device-name substrings, case-insensitive. Missing file == empty list.
# A teammate's Settings UI is expected to write this file; this script only reads it, once,
# at startup (no hot-reload -- restart the service to pick up changes).
EXCLUDE_FILE = os.path.expanduser("~/.config/techaq/excluded_devices.json")

# TechaQ app's own WebUI REST port, running inside the Docker container but exposed on
# loopback. A teammate is wiring up POST /api/scan in python/main.py.
SCAN_URL = "http://127.0.0.1:7000/api/scan"

RUNNING = True


def _stop(signum=None, frame=None):
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def load_excluded_names() -> list[str]:
    """Read the user-configurable exclusion list. Returns [] if the file is missing or invalid
    -- a missing/corrupt exclusion file must never stop the reader from starting."""
    try:
        with open(EXCLUDE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(s).lower() for s in data]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[scanner_reader] failed to read {EXCLUDE_FILE}, ignoring: {exc!r}")
    return []


def is_excluded(name: str, excluded_substrings: list[str]) -> bool:
    lname = (name or "").lower()
    for sub in BUILTIN_BLOCKLIST:
        if sub in lname:
            return True
    for sub in excluded_substrings:
        if sub and sub in lname:
            return True
    return False


# Minimal keycode -> character mapping. Digits are the critical path (EAN/ISBN barcodes are
# all-numeric); letters are supported best-effort for robustness but not hardened (no locale/
# dead-key handling, shift only flips case/digit-row per a plain US layout assumption).
_DIGIT_MAP = {
    E.KEY_0: "0", E.KEY_1: "1", E.KEY_2: "2", E.KEY_3: "3", E.KEY_4: "4",
    E.KEY_5: "5", E.KEY_6: "6", E.KEY_7: "7", E.KEY_8: "8", E.KEY_9: "9",
}
_LETTER_MAP = {getattr(E, f"KEY_{ch}"): ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}

_SHIFT_KEYS = {E.KEY_LEFTSHIFT, E.KEY_RIGHTSHIFT}
_TERMINATOR_KEYS = {E.KEY_ENTER, E.KEY_KPENTER, E.KEY_TAB}


def keycode_to_char(keycode: int, shift_down: bool) -> str | None:
    if keycode in _DIGIT_MAP:
        return _DIGIT_MAP[keycode]
    if keycode in _LETTER_MAP:
        ch = _LETTER_MAP[keycode]
        return ch if shift_down else ch.lower()
    return None


def post_scan(code: str, device_name: str) -> None:
    payload = json.dumps({"code": code, "device": device_name}).encode("utf-8")
    req = urllib.request.Request(
        SCAN_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
    except Exception as exc:
        # The TechaQ app may not be running yet, or may be mid-restart -- a dropped scan POST
        # must never crash the reader loop; just log and keep watching for the next scan.
        print(f"[scanner_reader] POST {SCAN_URL} failed for code={code!r}: {exc!r}")


def watch_device(path: str, name: str) -> None:
    """Runs in its own thread per candidate device; reads EV_KEY events and reconstructs
    complete barcode scans on Enter/Tab. One thread per device lets us watch multiple scanners
    (or a scanner plus other candidate devices) concurrently without blocking on any single one."""
    try:
        dev = InputDevice(path)
    except Exception as exc:
        print(f"[scanner_reader] could not open {path} ({name}): {exc!r}")
        return

    buf: list[str] = []
    shift_down = False
    print(f"[scanner_reader] watching {path} ({name})")

    try:
        for event in dev.read_loop():
            if not RUNNING:
                break
            if event.type != E.EV_KEY:
                continue
            key_event = categorize(event)
            keycode = key_event.scancode
            keystate = key_event.keystate  # 0=up, 1=down, 2=hold

            if keycode in _SHIFT_KEYS:
                shift_down = keystate != key_event.key_up
                continue

            if keystate != key_event.key_down:
                continue  # only act on keydown (and ignore autorepeat via key_hold)

            if keycode in _TERMINATOR_KEYS:
                if buf:
                    code = "".join(buf)
                    buf.clear()
                    print(f"[scanner_reader] scan complete from {name}: {code}")
                    post_scan(code, name)
                continue

            ch = keycode_to_char(keycode, shift_down)
            if ch is not None:
                buf.append(ch)
    except OSError as exc:
        # Device unplugged mid-run, etc. -- let this thread end quietly; other device threads
        # (and the main loop) keep running.
        print(f"[scanner_reader] device {path} ({name}) disconnected: {exc!r}")
    except Exception as exc:
        print(f"[scanner_reader] watch_device({path}) crashed: {exc!r}")
    finally:
        try:
            dev.close()
        except Exception:
            pass


def main() -> None:
    excluded = load_excluded_names()
    if excluded:
        print(f"[scanner_reader] user exclusion list: {excluded}")

    threads: list[threading.Thread] = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            name = dev.name
            dev.close()
        except Exception as exc:
            print(f"[scanner_reader] skipping {path}, could not read name: {exc!r}")
            continue

        if is_excluded(name, excluded):
            print(f"[scanner_reader] skipping excluded device {path} ({name})")
            continue

        t = threading.Thread(target=watch_device, args=(path, name), daemon=True)
        t.start()
        threads.append(t)

    if not threads:
        print("[scanner_reader] no candidate input devices found -- idling until stopped")

    # Main thread just waits for a stop signal; the per-device threads do the real work.
    while RUNNING:
        time.sleep(0.5)

    print("[scanner_reader] stopping...")
    # Daemon threads are blocked in dev.read_loop() with no clean cancel; since they're daemon
    # threads, process exit (right after main() returns) reaps them without hanging the service.


if __name__ == "__main__":
    main()

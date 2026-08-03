# SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
#
# SPDX-License-Identifier: MPL-2.0
"""Thin wrapper over a single Bridge RPC to the paired MCU, which drives a physical Modulino
Buzzer. Mirrors progq's Hardware wrapper: deferred Bridge import (so this module still imports
fine with no MCU/Bridge present, e.g. in unit tests), and every call is wrapped in try/except so
a missing buzzer/MCU degrades to silent no-op rather than ever taking the app down. Tone set is
tuned for "IBM PC speaker" vibes: short, single/double square-wave-ish blips at classic BIOS/
DOS-beep frequencies, not musical fanfare.
"""

# (freq_hz, duration_ms) pairs, or a list of them for multi-beep confirmations.
TONE_SCAN = (1500, 40)
TONE_SAVE = [(900, 60), (1400, 80)]
TONE_SEARCH = (1200, 30)
TONE_ERROR = [(220, 120), (180, 160)]
TONE_DELETE = (330, 100)
TONE_STARTUP = (1000, 60)


class Hardware:
    def __init__(self):
        from arduino.app_utils import Bridge  # deferred: only required when actually on-device

        self._bridge = Bridge

    def _play(self, freq: int, ms: int) -> None:
        try:
            self._bridge.call("play_tone", freq, ms)
        except Exception as exc:
            print(f"[techaq] play_tone({freq}, {ms}) failed, MCU/buzzer not ready: {exc!r}")

    def _play_sequence(self, tones) -> None:
        for freq, ms in tones:
            self._play(freq, ms)

    def _emit(self, tone) -> None:
        if isinstance(tone, list):
            self._play_sequence(tone)
        else:
            self._play(*tone)

    def play_scan(self) -> None:
        self._emit(TONE_SCAN)

    def play_save(self) -> None:
        self._emit(TONE_SAVE)

    def play_search(self) -> None:
        self._emit(TONE_SEARCH)

    def play_error(self) -> None:
        self._emit(TONE_ERROR)

    def play_delete(self) -> None:
        self._emit(TONE_DELETE)

    def play_startup(self) -> None:
        self._emit(TONE_STARTUP)

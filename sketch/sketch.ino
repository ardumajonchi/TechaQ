// SPDX-FileCopyrightText: Copyright (C) TechaQ contributors
//
// SPDX-License-Identifier: MPL-2.0
//
// MCU side of TechaQ: drives a physical Modulino Buzzer for action-confirmation beeps. All
// logic lives on the Linux side; the MCU only ever receives a frequency/duration pair and plays
// exactly that tone, once, then returns -- it never blocks on or waits for anything happening on
// the Linux side, and loop() stays empty so Bridge RPC servicing is never delayed.

#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>

ModulinoBuzzer buzzer;

// RPC provided to the MPU: hw.py's Hardware._play(freq, ms) calls this via Bridge.call("play_tone", ...)
String playTone(int freq, int ms) {
  buzzer.tone(freq, ms);
  return "{\"ok\":true}";
}

void setup() {
  Bridge.begin();
  Modulino.begin();
  buzzer.begin();
  Bridge.provide("play_tone", playTone);
}

void loop() {
}

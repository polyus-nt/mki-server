#pragma once
#include <stdint.h>
#include <stddef.h>
#include <math.h>
enum { LOW=0, HIGH=1, INPUT=0, OUTPUT=1, A0=14, A1=15, A2=16,
       A3=17, A4=18, A5=19, NUM_DIGITAL_PINS=20, NUM_ANALOG_INPUTS=6 };
extern uint32_t fakeMillis;
extern unsigned adcReads;
inline uint32_t millis() { return fakeMillis; }
inline void pinMode(uint8_t, uint8_t) {}
inline int analogRead(uint8_t pin) { ++adcReads; return pin == A1 ? 512 : 1023; }

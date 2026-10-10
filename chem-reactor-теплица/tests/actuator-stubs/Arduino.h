#pragma once
#include <stdint.h>
#include <stddef.h>
enum { LOW=0, HIGH=1, INPUT_PULLUP=2, OUTPUT=3, FALLING=4,
       A0=14, A1=15, A2=16, A3=17, A4=18, A5=19,
       NUM_DIGITAL_PINS=20, NOT_AN_INTERRUPT=255 };
struct GpioEvent { char action; uint8_t pin; int value; };
extern GpioEvent events[256];
extern unsigned eventCount;
extern uint32_t fakeMillis;
inline void digitalWrite(uint8_t pin, uint8_t value) {
  events[eventCount++] = {'D',pin,value};
}
inline void pinMode(uint8_t pin, uint8_t value) {
  events[eventCount++] = {'M',pin,value};
}
inline void analogWrite(uint8_t pin, int value) {
  events[eventCount++] = {'A',pin,value};
}
inline bool digitalPinHasPWM(uint8_t pin) {
  return pin==3 || pin==5 || pin==6 || pin==9 || pin==10 || pin==11;
}
inline uint32_t millis() { return fakeMillis; }
inline void noInterrupts() {}
inline void interrupts() {}
inline long random(long) { return 230; }

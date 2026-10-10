#pragma once
#include "Arduino.h"
extern void (*interruptHandlers[NUM_DIGITAL_PINS])();
inline uint8_t digitalPinToPCINT(uint8_t pin) {
  return pin < NUM_DIGITAL_PINS ? pin : static_cast<uint8_t>(NOT_AN_INTERRUPT);
}
inline void attachPCINT(uint8_t pin, void (*handler)(), uint8_t) {
  interruptHandlers[pin] = handler;
}

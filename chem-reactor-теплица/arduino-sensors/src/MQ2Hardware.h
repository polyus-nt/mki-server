#pragma once
#include <Arduino.h>
#include "../MQ2.h"
class MQ2Hardware : public MQ2::Reader {
 public:
  explicit MQ2Hardware(uint8_t pin) : pin_(pin) {}
  void begin() { pinMode(pin_, INPUT); }
  bool read(uint16_t& adc) override {
    // Предварительное чтение после переключения аналогового мультиплексора.
    analogRead(pin_); const int value = analogRead(pin_);
    if (value < 0 || value > 1023) return false;
    adc = static_cast<uint16_t>(value); return true;
  }
 private:
  uint8_t pin_;
};

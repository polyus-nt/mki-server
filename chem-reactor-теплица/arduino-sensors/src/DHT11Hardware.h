#pragma once
#include <Arduino.h>
#include "../DHT11.h"
#include "dht/DHT.h"

// GPIO только по пину из BoardConfig.h; библиотека включена локально.
class DHT11Hardware : public DHT11::Reader {
 public:
  explicit DHT11Hardware(uint8_t pin) : sensor_(pin, DHT_TYPE_11) {}
  void begin() override { sensor_.begin(); }
  bool humidity(float& percent) override {
    // force=true: планировщик DHT11 уже выдержал интервал 2 с. Не выдаём
    // предыдущий внутренний кэш Adafruit как новое физическое измерение.
    percent = sensor_.readHumidity(true);
    return isfinite(percent);
  }
  uint32_t completedAt(uint32_t) const override { return millis(); }
 private:
  DHT sensor_;
};

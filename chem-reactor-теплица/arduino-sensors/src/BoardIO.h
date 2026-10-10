#pragma once
#include <Arduino.h>
#include "../BoardConfig.h"
#include "SensorApi.h"
#include "BMP280SoftwareBus.h"
#include "DHT11Hardware.h"
#include "MQ2Hardware.h"

class BoardIO : public hardware_api::SensorHardware {
 public:
  BoardIO() : ready_(false), next_(0),
      buses_{{API_BMP_SDA0_PIN, API_BMP_SCL_PIN}, {API_BMP_SDA1_PIN, API_BMP_SCL_PIN}},
      dhtReaders_{DHT11Hardware(API_DHT0_PIN), DHT11Hardware(API_DHT1_PIN)},
      gasReaders_{MQ2Hardware(API_GAS0_PIN), MQ2Hardware(API_GAS1_PIN)} {}
  bool setup() {
    ready_ = false;
    // Общий SCL записан один раз. Все остальные роли обязаны быть уникальны.
    const uint8_t pins[] = {API_LINK_MISO, API_LINK_MOSI, API_LINK_CLK, API_LINK_CS,
        API_LINK_IRQ, API_BMP_SCL_PIN, API_BMP_SDA0_PIN, API_BMP_SDA1_PIN,
        API_DHT0_PIN, API_DHT1_PIN, API_GAS0_PIN, API_GAS1_PIN};
    for (uint8_t i = 0; i < sizeof(pins); ++i) {
      if (pins[i] >= NUM_DIGITAL_PINS) return false;
      for (uint8_t j = 0; j < i; ++j) if (pins[i] == pins[j]) return false;
    }
    if (API_GAS0_PIN < A0 || API_GAS0_PIN >= A0 + NUM_ANALOG_INPUTS ||
        API_GAS1_PIN < A0 || API_GAS1_PIN >= A0 + NUM_ANALOG_INPUTS) return false;
    for (uint8_t i = 0; i < 2; ++i) {
      buses_[i].begin(); bmp_[i].attach(buses_[i], millis());
      dht_[i].attach(dhtReaders_[i], millis());
      gasReaders_[i].begin(); gas_[i].attach(gasReaders_[i], API_ADC_REFERENCE_MV, millis());
    }
    ready_ = true; return true;
  }
  void poll(uint32_t now) {
    if (!ready_) return;
    if (next_ < 2) bmp_[next_].poll(now);
    else if (next_ < 4) dht_[next_ - 2].poll(now);
    else gas_[next_ - 4].poll(now);
    next_ = (next_ + 1) % 6;
  }
  hardware_api::Status measure(uint8_t command, uint8_t idx, hardware_api::Measurement& result) override {
    using namespace hardware_api;
    if (idx >= 2) return INVALID_INDEX;
    if (!ready_) return NOT_IMPLEMENTED;
    if (command == kGetGas) {
      const MQ2::Reading r = gas_[idx].read(millis());
      if (!r.valid) return SENSOR_UNAVAILABLE;
      result.value = (static_cast<uint32_t>(r.adc) << 16) | r.millivolts;
      result.flags = r.fresh ? 3 : 1; result.ageMs = r.ageMs; return OK;
    }
    if (command == kGetHumidity) {
      const DHT11::Reading r = dht_[idx].read(millis());
      if (!r.valid) return SENSOR_UNAVAILABLE;
      result.value = static_cast<uint32_t>(round(r.value * 100.0));
      result.flags = r.fresh ? 3 : 1; result.ageMs = r.ageMs; return OK;
    }
    if (command != kGetTemp && command != kGetPressure) return UNKNOWN_COMMAND;
    const bool temp = command == kGetTemp;
    const BMP280::Reading r = temp ? bmp_[idx].readTemperature(millis()) : bmp_[idx].read(millis());
    if (!r.valid) return SENSOR_UNAVAILABLE;
    const double value = round(static_cast<double>(r.value) * (temp ? 100.0 : 1.0));
    if (!isfinite(value)) return SENSOR_UNAVAILABLE;
    if (temp) {
      if (value <= -2147483648.0 || value >= 2147483647.0) return SENSOR_UNAVAILABLE;
      result.value = static_cast<uint32_t>(static_cast<int32_t>(value));
    } else {
      if (value < 0 || value >= 4294967295.0) return SENSOR_UNAVAILABLE;
      result.value = static_cast<uint32_t>(value);
    }
    result.flags = r.fresh ? 3 : 1; result.ageMs = r.ageMs; return OK;
  }
 private:
  bool ready_; uint8_t next_;
  BMP280 bmp_[2]; DHT11 dht_[2]; MQ2 gas_[2];
  BMP280SoftwareBus buses_[2]; DHT11Hardware dhtReaders_[2]; MQ2Hardware gasReaders_[2];
};

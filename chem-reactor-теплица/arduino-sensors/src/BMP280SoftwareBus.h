#pragma once
#include "softwarewire/SoftwareWire.h"
#include "../BMP280.h"

// Each of the three buses may share SCL; SDA and calibration are independent.
class BMP280SoftwareBus : public BMP280::Bus {
 public:
  BMP280SoftwareBus(uint8_t sda, uint8_t scl, uint8_t address = 0x76)
      : wire_(sda, scl, false), address_(address) {}
  void begin() { wire_.begin(); wire_.setClock(50000); wire_.setTimeout(20); }
  bool readRegisters(uint8_t reg, uint8_t* data, uint8_t count) override {
    wire_.beginTransmission(address_);
    if (wire_.write(reg) != 1) { wire_.endTransmission(); return false; }
    if (wire_.endTransmission(false) != 0 || wire_.getTimeoutFlag()) {
      wire_.endTransmission(); return false;
    }
    if (wire_.requestFrom(address_, count) != count || wire_.getTimeoutFlag()) return false;
    for (uint8_t i = 0; i < count; ++i) {
      const int byte = wire_.read();
      if (byte < 0) return false;
      data[i] = static_cast<uint8_t>(byte);
    }
    return true;
  }
  bool writeRegister(uint8_t reg, uint8_t value) override {
    wire_.beginTransmission(address_);
    const bool written = wire_.write(reg) == 1 && wire_.write(value) == 1;
    const bool stopped = wire_.endTransmission() == 0;
    return written && stopped && !wire_.getTimeoutFlag();
  }
 private:
  SoftwareWire wire_;
  uint8_t address_;
};

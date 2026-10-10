#pragma once
#include <math.h>
#include <stdint.h>

// One physical BMP280, one bus, its own calibration and cached paired sample.
// No Arduino dependency: register transport is injected.
class BMP280 {
 public:
  class Bus {
   public:
    virtual ~Bus() {}
    virtual bool readRegisters(uint8_t reg, uint8_t* data, uint8_t count) = 0;
    virtual bool writeRegister(uint8_t reg, uint8_t value) = 0;
  };
  struct Reading { float value; bool valid; bool fresh; uint16_t ageMs; };
  struct Calibration {
    uint16_t T1; int16_t T2, T3;
    uint16_t P1; int16_t P2, P3, P4, P5, P6, P7, P8, P9;
  };
  explicit BMP280(Bus* bus = nullptr) : bus_(bus) { setup(); }
  void attach(Bus& bus, uint32_t now = 0) { bus_ = &bus; setup(now); }
  void setup(uint32_t now = 0) {
    stage_ = ReadId; connected_ = valid_ = freshT_ = freshP_ = retry_ = false;
    lastStep_ = now - 5UL; initStarted_ = now;
    lastAttempt_ = measuredAt_ = now; temperature_ = pressure_ = NAN;
    calibration_ = Calibration();
  }
  bool isConfigured() const { return bus_ != nullptr; }
  bool isConnected() const { return connected_; }

  // One synchronous register operation per poll; no conversion wait/delay loop.
  bool poll(uint32_t now) {
    if (!bus_ || static_cast<uint32_t>(now - lastStep_) < 5UL) return false;
    lastStep_ = now;
    if (retry_) {
      if (static_cast<uint32_t>(now - lastAttempt_) < 1000UL) return false;
      retry_ = false; stage_ = ReadId; initStarted_ = now;
    }
    if (!connected_ && static_cast<uint32_t>(now - initStarted_) >= 250UL)
      return fail(now);
    uint8_t value = 0;
    switch (stage_) {
      case ReadId:
        if (!bus_->readRegisters(0xd0, &value, 1) || value != 0x58) return fail(now);
        stage_ = WaitNvm; return false;
      case WaitNvm:
        if (!bus_->readRegisters(0xf3, &value, 1)) return fail(now);
        if (!(value & 1)) stage_ = ReadCalibration;
        return false;
      case ReadCalibration: {
        uint8_t raw[24];
        if (!bus_->readRegisters(0x88, raw, 24)) return fail(now);
        calibration_.T1 = u16(raw); calibration_.T2 = s16(raw + 2);
        calibration_.T3 = s16(raw + 4); calibration_.P1 = u16(raw + 6);
        calibration_.P2 = s16(raw + 8); calibration_.P3 = s16(raw + 10);
        calibration_.P4 = s16(raw + 12); calibration_.P5 = s16(raw + 14);
        calibration_.P6 = s16(raw + 16); calibration_.P7 = s16(raw + 18);
        calibration_.P8 = s16(raw + 20); calibration_.P9 = s16(raw + 22);
        if (!calibration_.P1) return fail(now);
        stage_ = Sleep; return false;
      }
      case Sleep:
        if (!bus_->writeRegister(0xf4, 0)) return fail(now);
        stage_ = WaitSleep; return false;
      case WaitSleep:
        if (!bus_->readRegisters(0xf3, &value, 1)) return fail(now);
        if (!(value & 9)) stage_ = Configure;
        return false;
      case Configure:
        // Config must be written in sleep: 0.5ms standby, filter/SPI-3wire off.
        if (!bus_->writeRegister(0xf5, 0)) return fail(now);
        stage_ = Start; return false;
      case Start:
        if (!bus_->writeRegister(0xf4, 0x27)) return fail(now);
        stage_ = Sample; connected_ = true; lastAttempt_ = now; return false;
      case Sample: {
        if (static_cast<uint32_t>(now - lastAttempt_) < 100UL) return false;
        lastAttempt_ = now;
        uint8_t raw[6];
        if (!bus_->readRegisters(0xf7, raw, 6)) return fail(now);
        const uint32_t adcP = (static_cast<uint32_t>(raw[0]) << 12) |
                             (static_cast<uint32_t>(raw[1]) << 4) | (raw[2] >> 4);
        const uint32_t adcT = (static_cast<uint32_t>(raw[3]) << 12) |
                             (static_cast<uint32_t>(raw[4]) << 4) | (raw[5] >> 4);
        float temperature, pressure;
        if (!compensate(calibration_, adcT, adcP, temperature, pressure)) return fail(now);
        // Commit BOTH quantities only after all checks succeed.
        temperature_ = temperature; pressure_ = pressure; measuredAt_ = now;
        valid_ = freshT_ = freshP_ = true; return true;
      }
    }
    return false;
  }
  Reading read() { return read(lastStep_); }
  Reading read(uint32_t now) { return reading(pressure_, freshP_, now); }
  Reading readTemperature() { return readTemperature(lastStep_); }
  Reading readTemperature(uint32_t now) { return reading(temperature_, freshT_, now); }

  // Bosch double compensation, adapted from the supplied sketch. Pressure is Pa.
  static bool compensate(const Calibration& c, uint32_t adcT, uint32_t adcP,
                         float& temperature, float& pressure) {
    if (!c.P1 || adcT > 0xfffffUL || adcP > 0xfffffUL ||
        adcT == 0x80000UL || adcP == 0x80000UL) return false;
    double v1 = (static_cast<double>(adcT) / 16384.0 - c.T1 / 1024.0) * c.T2;
    const double x = static_cast<double>(adcT) / 131072.0 - c.T1 / 8192.0;
    double v2 = x * x * c.T3;
    const double fine = v1 + v2;
    if (!isfinite(fine) || fine <= -2147483648.0 || fine >= 2147483647.0) return false;
    const int32_t tFine = static_cast<int32_t>(fine);
    temperature = static_cast<float>(fine / 5120.0);
    v1 = static_cast<double>(tFine) / 2.0 - 64000.0;
    v2 = v1 * v1 * c.P6 / 32768.0;
    v2 += v1 * c.P5 * 2.0;
    v2 = v2 / 4.0 + static_cast<double>(c.P4) * 65536.0;
    v1 = (c.P3 * v1 * v1 / 524288.0 + c.P2 * v1) / 524288.0;
    v1 = (1.0 + v1 / 32768.0) * c.P1;
    if (!isfinite(v1) || v1 == 0.0) return false;
    double p = (1048576.0 - adcP - v2 / 4096.0) * 6250.0 / v1;
    v1 = c.P9 * p * p / 2147483648.0; v2 = p * c.P8 / 32768.0;
    p += (v1 + v2 + c.P7) / 16.0;
    if (!isfinite(temperature) || !isfinite(p) || p <= 0 || p >= 4294967295.0) return false;
    pressure = static_cast<float>(p);
    return isfinite(pressure) && pressure < 4294967295.0;
  }
 private:
  enum Stage { ReadId, WaitNvm, ReadCalibration, Sleep, WaitSleep, Configure, Start, Sample };
  static uint16_t u16(const uint8_t* p) {
    return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
  }
  static int16_t s16(const uint8_t* p) {
    const uint16_t value = u16(p);
    return value < 0x8000 ? static_cast<int16_t>(value)
                         : static_cast<int16_t>(static_cast<int32_t>(value) - 65536L);
  }
  bool fail(uint32_t now) {
    connected_ = false; retry_ = true; stage_ = ReadId; lastAttempt_ = now;
    freshT_ = freshP_ = false; // Keep old valid data with increasing age.
    return false;
  }
  Reading reading(float value, bool& fresh, uint32_t now) {
    const uint32_t age = static_cast<uint32_t>(now - measuredAt_);
    const Reading result = {valid_ ? value : NAN, valid_, valid_ && fresh,
                            static_cast<uint16_t>(age > 65534UL ? 65534 : age)};
    fresh = false; return result;
  }
  Bus* bus_;
  Calibration calibration_;
  Stage stage_;
  bool connected_, valid_, freshT_, freshP_, retry_;
  uint32_t lastStep_, initStarted_, lastAttempt_, measuredAt_;
  float temperature_, pressure_;
};

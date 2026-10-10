#pragma once
#include <Arduino.h>
#include "../BoardConfig.h"
#include "ActuatorApi.h"

#include <PinChangeInterrupt.h>
class BoardIO : public hardware_api::FanHardware {
 public:
  BoardIO() : valid_(false), lastMs_(0) {
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i) { pulses_[i] = 0; rpm_[i] = 0; pwm_[i] = 0; }
  }
  bool setup() {
    instance_ = this;
    uint8_t used[5 + 3 * API_FAN_COUNT] = {
      API_LINK_MISO, API_LINK_MOSI, API_LINK_CLK, API_LINK_CS, API_LINK_IRQ
    };
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i) {
      used[5 + i] = pwmPins_[i];
      used[5 + API_FAN_COUNT + i] = tachPins_[i];
      used[5 + 2 * API_FAN_COUNT + i] = relayPins_[i];
    }
    for (uint8_t i = 0; i < sizeof(used); ++i) {
      if (used[i] >= NUM_DIGITAL_PINS) return false;
      for (uint8_t j = 0; j < i; ++j) if (used[i] == used[j]) return false;
    }
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i)
      if (!digitalPinHasPWM(pwmPins_[i]) || digitalPinToPCINT(tachPins_[i]) == NOT_AN_INTERRUPT)
        return false;
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i) {
      // Предзагрузка HIGH до OUTPUT исключает импульс включения реле LOW.
      digitalWrite(relayPins_[i], HIGH); pinMode(relayPins_[i], OUTPUT);
      digitalWrite(pwmPins_[i], LOW); pinMode(pwmPins_[i], OUTPUT);
      analogWrite(pwmPins_[i], 0);
      pinMode(tachPins_[i], INPUT_PULLUP);
    }
    void (*handlers[2])() = {pulse0, pulse1};
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i)
      attachPCINT(digitalPinToPCINT(tachPins_[i]), handlers[i], FALLING);
    valid_ = true; lastMs_ = millis(); return true;
  }
  void poll(uint32_t now) {
    const uint32_t elapsed = static_cast<uint32_t>(now - lastMs_);
    if (!valid_ || elapsed < 1000UL) return;
    uint32_t snapshot[API_FAN_COUNT];
    noInterrupts();
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i) { snapshot[i] = pulses_[i]; pulses_[i] = 0; }
    interrupts();
    for (uint8_t i = 0; i < API_FAN_COUNT; ++i) {
      const uint64_t value = static_cast<uint64_t>(snapshot[i]) * 60000ULL /
                            (static_cast<uint64_t>(API_PULSES_PER_REV) * elapsed);
      rpm_[i] = value > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(value);
    }
    lastMs_ = now;
  }
  bool available(uint8_t idx) const override { return valid_ && idx < API_FAN_COUNT; }
  bool writePwm(uint8_t idx, uint8_t pwm) override {
    if (!available(idx)) return false;
    digitalWrite(relayPins_[idx], pwm == 0 ? HIGH : LOW);
    analogWrite(pwmPins_[idx], pwm); pwm_[idx] = pwm; return true;
  }
  uint8_t readPwm(uint8_t idx) const override { return available(idx) ? pwm_[idx] : 0; }
  uint32_t readRpm(uint8_t idx) const override { return available(idx) ? rpm_[idx] : 0; }
  uint8_t randomPwm() override { return static_cast<uint8_t>(random(256)); }
 private:
  static void pulse(uint8_t idx) { if (instance_) ++instance_->pulses_[idx]; }
  static void pulse0() { pulse(0); } static void pulse1() { pulse(1); }
  static BoardIO* instance_;
  static const uint8_t pwmPins_[], tachPins_[], relayPins_[];
  bool valid_;
  volatile uint32_t pulses_[API_FAN_COUNT];
  uint32_t rpm_[API_FAN_COUNT], lastMs_;
  uint8_t pwm_[API_FAN_COUNT];
};
BoardIO* BoardIO::instance_ = nullptr;
const uint8_t BoardIO::pwmPins_[] = API_PWM_PINS;
const uint8_t BoardIO::tachPins_[] = API_TACH_PINS;
const uint8_t BoardIO::relayPins_[] = API_RELAY_PINS;

#pragma once
#include <stdio.h>
#include "SensorApi.h"
// Только текстовые диагностические команды Serial; бинарный API идёт по Austere.
class TestConsole {
 public:
  TestConsole() : used_(0), overflow_(false), tx_(0) {}
  void feed(char c, hardware_api::SensorApi& api) {
    if (c == '\r') return;
    if (c != '\n') {
      if (used_ + 1 < sizeof(line_)) line_[used_++] = c; else overflow_ = true;
      return;
    }
    line_[used_] = 0; used_ = 0;
    if (overflow_) { overflow_ = false; Serial.println(F("Command too long")); return; }
    if (!strcmp(line_, "help")) {
      Serial.println(F("temp 0..1 | pressure 0..1 | humidity 0..1 | gas 0..1")); return;
    }
    char name[12], extra; unsigned int idx;
    if (sscanf(line_, "%11s %u %c", name, &idx, &extra) != 2 || idx > 1) {
      Serial.println(F("Invalid command/index")); return;
    }
    using namespace hardware_api;
    Frame request = {}; request.version = kVersion; request.kind = kRequest;
    request.node = kSensorsNode; request.idx = idx; request.tx_id = ++tx_; request.session_id = 1;
    if (!strcmp(name, "temp")) request.command = kGetTemp;
    else if (!strcmp(name, "pressure")) request.command = kGetPressure;
    else if (!strcmp(name, "humidity")) request.command = kGetHumidity;
    else if (!strcmp(name, "gas")) request.command = kGetGas;
    else { Serial.println(F("Unknown command")); return; }
    const Frame reply = api.handle(request);
    Serial.print(F("status=")); Serial.println(reply.status);
    if (reply.status == OK) {
      if (request.command == kGetGas) {
        Serial.print(F("ADC=")); Serial.print(readU16(reply.data));
        Serial.print(F(" mV=")); Serial.println(readU16(reply.data + 2));
      } else {
        Serial.print(F("value="));
        if (request.command == kGetTemp) Serial.print(static_cast<int32_t>(readU32(reply.data)) / 100.0);
        else if (request.command == kGetHumidity) Serial.print(readU32(reply.data) / 100.0);
        else Serial.print(readU32(reply.data));
        Serial.println(request.command == kGetTemp ? F(" C") : request.command == kGetHumidity ? F(" %RH") : F(" Pa"));
      }
      Serial.print(F("fresh=")); Serial.print((reply.data[4] & 2) != 0);
      Serial.print(F(" age_ms=")); Serial.println(readU16(reply.data + 5));
    }
    uint8_t bytes[kFrameSize]; encode(reply, bytes); Serial.print(F("TX HEX:"));
    for (uint8_t i = 0; i < kFrameSize; ++i) {
      Serial.print(' '); if (bytes[i] < 16) Serial.print('0'); Serial.print(bytes[i], HEX);
    }
    Serial.println();
  }
 private:
  char line_[32]; uint8_t used_; bool overflow_; uint16_t tx_;
};

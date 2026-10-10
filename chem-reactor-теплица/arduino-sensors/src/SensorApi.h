#pragma once
#include "common/HardwareProtocol.h"
namespace hardware_api {
struct Measurement { uint32_t value; uint8_t flags; uint16_t ageMs; };
class SensorHardware {
 public:
  virtual ~SensorHardware() {}
  virtual Status measure(uint8_t command, uint8_t idx, Measurement& result) = 0;
};
class SensorApi {
 public:
  explicit SensorApi(SensorHardware& hardware) : hardware_(hardware) {}
  Frame handle(const Frame& request) {
    const Status checked = validateRequest(request, kSensorsNode);
    if (checked != OK) return response(request, checked);
    Measurement m = {}; const Status status = hardware_.measure(request.command, request.idx, m);
    if (status != OK) return response(request, status);
    if ((m.flags != 1 && m.flags != 3) || m.ageMs > 65534 ||
        (request.command == kGetHumidity && m.value > 10000) ||
        (request.command == kGetGas && (m.value >> 16) > 1023))
      return response(request, INTERNAL_ERROR);
    Frame reply = response(request); writeU32(reply.data, m.value);
    reply.data[4] = m.flags; writeU16(reply.data + 5, m.ageMs); return reply;
  }
 private:
  SensorHardware& hardware_;
};
}

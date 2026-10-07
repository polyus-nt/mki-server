#pragma once
#ifndef HARDWARE_API_PROTOCOL_V1_H
#define HARDWARE_API_PROTOCOL_V1_H
// Hardware API v1 draft codec. No Arduino dependencies; all integers are big-endian.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace hardware_api {
enum {
  kFrameSize = 20, kVersion = 1, kRequest = 1, kResponse = 2,
  kSensorsNode = 13, kActuatorNode = 14, kFanCount = 6,
  kGetTemp = 0x20, kGetVoc = 0x21, kGetDoor = 0x22,
  kGetPressure = 0x23, kGetHumidity = 0x24, kGetDust = 0x25,
  kGetPwm = 0x30, kGetRpm = 0x31, kSetPwm = 0x32,
  kGetMode = 0x40, kSetMode = 0x41, kSetDoorLock = 0x42
};
enum Status {
  OK = 0, INVALID_INDEX = 1, INVALID_ARGUMENT = 2, UNKNOWN_COMMAND = 3,
  NOT_IMPLEMENTED = 4, SENSOR_UNAVAILABLE = 5, BUSY = 6,
  DEVICE_TIMEOUT = 7, UNSUPPORTED_VERSION = 8, INTERNAL_ERROR = 9,
  REQUEST_ID_CONFLICT = 10, INVALID_NODE = 11, TRANSPORT_OVERFLOW = 12
};
struct Frame {
  uint8_t version, kind;
  uint16_t tx_id;
  uint8_t node, command, status, idx;
  uint8_t data[8];
};
inline uint16_t readU16(const uint8_t* p) {
  return static_cast<uint16_t>((static_cast<uint16_t>(p[0]) << 8) | p[1]);
}
inline uint32_t readU32(const uint8_t* p) {
  return (static_cast<uint32_t>(p[0]) << 24) |
         (static_cast<uint32_t>(p[1]) << 16) |
         (static_cast<uint32_t>(p[2]) << 8) | p[3];
}
inline void writeU16(uint8_t* p, uint16_t value) {
  p[0] = static_cast<uint8_t>(value >> 8); p[1] = static_cast<uint8_t>(value);
}
inline void writeU32(uint8_t* p, uint32_t value) {
  p[0] = static_cast<uint8_t>(value >> 24); p[1] = static_cast<uint8_t>(value >> 16);
  p[2] = static_cast<uint8_t>(value >> 8); p[3] = static_cast<uint8_t>(value);
}
inline uint16_t crc16(const uint8_t* bytes, size_t size) {
  uint16_t crc = 0xffff;
  for (size_t i = 0; i < size; ++i) {
    crc ^= static_cast<uint16_t>(bytes[i]) << 8;
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                          : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}
inline void encode(const Frame& frame, uint8_t* bytes) {
  bytes[0] = 0xa5; bytes[1] = 0x5a;
  bytes[2] = frame.version; bytes[3] = frame.kind;
  writeU16(bytes + 4, frame.tx_id);
  bytes[6] = frame.node; bytes[7] = frame.command;
  bytes[8] = frame.status; bytes[9] = frame.idx;
  memcpy(bytes + 10, frame.data, 8);
  writeU16(bytes + 18, crc16(bytes + 2, 16));
}
inline bool decode(const uint8_t* bytes, Frame& frame) {
  if (bytes[0] != 0xa5 || bytes[1] != 0x5a ||
      readU16(bytes + 18) != crc16(bytes + 2, 16)) return false;
  frame.version = bytes[2]; frame.kind = bytes[3];
  frame.tx_id = readU16(bytes + 4);
  frame.node = bytes[6]; frame.command = bytes[7];
  frame.status = bytes[8]; frame.idx = bytes[9];
  memcpy(frame.data, bytes + 10, 8);
  return true;
}
inline Frame response(const Frame& request, uint8_t status = OK) {
  Frame result = request;
  result.version = kVersion; result.kind = kResponse; result.status = status;
  memset(result.data, 0, sizeof(result.data));
  return result;
}
inline bool sameRequest(const Frame& request, const Frame& reply) {
  return reply.version == kVersion && reply.kind == kResponse &&
         request.tx_id == reply.tx_id && request.node == reply.node &&
         request.command == reply.command && request.idx == reply.idx;
}
inline bool zeros(const uint8_t* data, size_t length) {
  for (size_t i = 0; i < length; ++i) if (data[i]) return false;
  return true;
}
// Pure semantic validation. Door locks are NOT owned by either Arduino.
inline Status validateRequest(const Frame& request, uint8_t localNode) {
  if (request.version != kVersion) return UNSUPPORTED_VERSION;
  if (request.kind != kRequest || request.status != OK) return INVALID_ARGUMENT;
  uint8_t node = 0, count = 0;
  bool argument = false, modeRead = false;
  switch (request.command) {
    case kGetTemp: case kGetPressure: case kGetHumidity:
      node = kSensorsNode; count = 3; break;
    case kGetVoc: case kGetDoor: case kGetDust:
      node = kSensorsNode; count = 2; break;
    case kGetPwm: case kGetRpm:
      node = kActuatorNode; count = kFanCount; break;
    case kSetPwm: case kSetMode:
      node = kActuatorNode; count = kFanCount; argument = true; break;
    case kGetMode:
      node = kActuatorNode; modeRead = true; break;
    default: return UNKNOWN_COMMAND;
  }
  if (request.node != node || localNode != node) return INVALID_NODE;
  if (modeRead ? request.idx != 0xff : request.idx >= count) return INVALID_INDEX;
  if (!zeros(request.data + (argument ? 1 : 0), argument ? 7 : 8))
    return INVALID_ARGUMENT;
  if (request.command == kSetMode && request.data[0] > 2) return INVALID_ARGUMENT;
  return OK;
}

// Bounded, allocation-free sliding parser. Timers tolerate millis() wrap.
class StreamParser {
 public:
  StreamParser() : size_(0), firstMs_(0), lastMs_(0) {}
  void reset() { size_ = 0; }
  void tick(uint32_t now) {
    if (size_ && (static_cast<uint32_t>(now - lastMs_) >= 250UL ||
                  static_cast<uint32_t>(now - firstMs_) >= 1000UL)) reset();
  }
  bool feed(uint8_t byte, uint32_t now, Frame& result) {
    tick(now);
    if (!size_) {
      if (byte != 0xa5) return false;
      firstMs_ = now;
    }
    bytes_[size_++] = byte; lastMs_ = now;
    if (size_ == 2 && bytes_[1] != 0x5a) {
      size_ = byte == 0xa5 ? 1 : 0;
      if (size_) firstMs_ = now;
      return false;
    }
    if (size_ < kFrameSize) return false;
    if (decode(bytes_, result) &&
        (result.kind == kRequest || result.kind == kResponse)) {
      reset(); return true;
    }
    // Keep any possible magic inside a damaged candidate, not just its last byte.
    uint8_t start = 1;
    while (start < size_ &&
           !(bytes_[start] == 0xa5 &&
             (start + 1 == size_ || bytes_[start + 1] == 0x5a))) ++start;
    size_ = static_cast<uint8_t>(size_ - start);
    memmove(bytes_, bytes_ + start, size_);
    firstMs_ = now;
    return false;
  }
 private:
  uint8_t bytes_[kFrameSize], size_;
  uint32_t firstMs_, lastMs_;
};

// One complete reply awaiting TX. Does not overwrite an unsent reply.
class ReplyBuffer {
 public:
  ReplyBuffer() : position_(kFrameSize) {}
  bool pending() const { return position_ < kFrameSize; }
  bool enqueue(const Frame& frame) {
    if (pending()) return false;
    encode(frame, bytes_); position_ = 0; return true;
  }
  uint8_t nextByte() const { return bytes_[position_]; }
  void accepted() { if (pending()) ++position_; }
 private:
  uint8_t bytes_[kFrameSize], position_;
};
}  // namespace hardware_api
#endif  // HARDWARE_API_PROTOCOL_V1_H

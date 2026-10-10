#pragma once
#include "../common/HardwareProtocol.h"
namespace tjc_lmp8 {
const uint8_t kBusAddress = 16, kHubAddress = 12;
const uint8_t kLockPin = 1;
const uint8_t kRelayPins[6] = {2, 3, 4, 5, 6, 7};
const uint32_t kBreakMs = 250;
// Общий выход двух замков и шесть реле активны LOW.
// Начальный HIGH предзагружается до перехода GPIO в режим выхода.
inline bool inactiveHigh(uint8_t ledIndex) { return ledIndex < 7; }
inline uint8_t relayMask(uint8_t level) {
  const uint8_t masks[11] = {0, 49, 51, 55, 63, 33, 17, 35, 39, 47, 1};
  return level <= 10 ? masks[level] : 0;
}
}

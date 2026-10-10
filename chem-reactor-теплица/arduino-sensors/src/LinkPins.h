#pragma once
#include <Arduino.h>
#include "../BoardConfig.h"
// Validate transport pins even when sensor/fan outputs are unconfigured.
inline bool validLinkPins() {
  const int pins[5] = {API_LINK_MISO, API_LINK_MOSI, API_LINK_CLK, API_LINK_CS, API_LINK_IRQ};
  for (uint8_t i = 0; i < 5; ++i) {
    if (pins[i] < 0 || pins[i] >= NUM_DIGITAL_PINS) return false;
    for (uint8_t j = 0; j < i; ++j) if (pins[i] == pins[j]) return false;
  }
  return true;
}

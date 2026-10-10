#pragma once
#include "../common/HardwareProtocol.h"

namespace tjc_buttons {
// Пользовательские номера Button, не номера выводов STM32:
// Button(1) -> PA1, Button(2) -> PA8 в tjc-ms1-btn-a2/1.0.
static const uint8_t kDoorPins[2] = {1, 2};
static const uint8_t kBusAddress = hardware_api::kButtonsNode;  // 15
static const uint8_t kHubAddress = 12;
static const uint32_t kDebounceMs = 50;
// Временная договорённость: контакт замкнут на GND -> дверь закрыта (true).
// При проверке монтажа можно независимо инвертировать семантику каждого входа.
static const bool kClosedWhenPressed[2] = {true, true};
// Разомкнутый контакт должен иметь определённый HIGH, а не плавающий вход.
// У Button::pullup() в исходном классе пустое тело; настраиваем PUPDR явно.
static const bool kEnableInternalPullup = true;
}

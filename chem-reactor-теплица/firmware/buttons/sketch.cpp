#include <stdint.h>
#include "stm32g030xx.h"
#include "system.c"
#undef INLINE__
#include "UART.hpp"
#include "SimpleBus.hpp"
#include "Button.hpp"
#include "ButtonsApi.h"

// Исходный Button задаёт PA1/PA8 как входы без внутренних подтяжек.
// Используем аппаратную схему кнопочницы; не добавляем свои GPIO-драйверы.
Button door0(tjc_buttons::kDoorPins[0]);
Button door1(tjc_buttons::kDoorPins[1]);
SimpleBus bus;

int main() {
  using namespace hardware_api;
  bus.setAddress(tjc_buttons::kBusAddress);
  detail::hal::initUartWrapper();
  if (tjc_buttons::kEnableInternalPullup) {
    for (uint8_t idx = 0; idx < 2; ++idx) {
      // Используем mapping самого Button.hpp, не дублируем PA1/PA8 в коде.
      const Button_t& input = btns[tjc_buttons::kDoorPins[idx] - 1];
      const uint32_t shift = static_cast<uint32_t>(input.num) * 2;
      input.port->PUPDR = (input.port->PUPDR & ~(3UL << shift)) | (1UL << shift);
    }
  }
  door0.setDebounceDelay(tjc_buttons::kDebounceMs);
  door1.setDebounceDelay(tjc_buttons::kDebounceMs);
  tjc_buttons::ButtonsApi api(millis());
  StreamParser parser;
  uint8_t outgoing[kFrameSize];
  uint8_t position = kFrameSize;
  uint32_t last_end = 0;
  bool ever_sent = false;
  for (;;) {
    // Не блокируем опрос концевиков ожиданием запросов или полным кадром TX.
    door0.scan(); door1.scan();
    const uint32_t now = millis();
    api.sample(door0.isPressed(), door1.isPressed(), now);
    parser.tick(now);
    if (detail::simpleBusHelpers::Api::takeOverflow()) parser.reset();
    while (bus.packetReceived()) {
      Frame request;
      if (!parser.feed(bus.lastData, millis(), request)) continue;
      // Ответы не исполняем. Хаб сериализует запросы на общей шине.
      // Не перезаписываем ещё не отправленный ответ при постороннем запросе.
      if (request.kind != kRequest || position != kFrameSize) continue;
      encode(api.execute(request), outgoing);
      position = 0;
    }
    if (position < kFrameSize &&
        (!ever_sent || static_cast<uint32_t>(millis() - last_end) >= 2)) {
      bus.sendPacket(tjc_buttons::kHubAddress, outgoing[position++]);
      last_end = millis(); ever_sent = true;
    }
  }
}

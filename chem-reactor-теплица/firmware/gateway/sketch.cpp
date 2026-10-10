#include <stdint.h>
#include "stm32g030xx.h"
#include "system.c"
#undef INLINE__  // system.c and upstream macros.hpp use different definitions.
#include "UART.hpp"
#include "SimpleBus.hpp"
#include "ArduinoAdapter.hpp"
#include "Gateway.h"

#ifndef GATEWAY_NODE
#error "Build with GATEWAY_NODE=13 or 14"
#endif
static_assert(GATEWAY_NODE == 13 || GATEWAY_NODE == 14,
              "Unknown Arduino gateway address");

// Hardware runtime definitions and IRQ handlers are included in this single
// translation unit. Startup calls SystemInit before C++ global constructors.
SimpleBus bus;
ArduinoAdapter arduino;
tjc_gateway::Gateway gateway(GATEWAY_NODE);

static uint32_t lockInterrupts() {
  const uint32_t saved = __get_PRIMASK();
  __disable_irq();
  return saved;
}

static void unlockInterrupts(uint32_t saved) { __set_PRIMASK(saved); }

static void cancelArduinoQueues() {
  const uint32_t saved = lockInterrupts();
  sm.rx_tail = sm.rx_head;
  sm.tx_tail = sm.tx_head;
  refresh_irq_level();
  unlockInterrupts(saved);
}

static bool enqueueArduinoFrame(const uint8_t* bytes) {
  const uint32_t saved = lockInterrupts();
  const bool capacity = static_cast<uint16_t>(sm.tx_head - sm.tx_tail) +
                            hardware_api::kFrameSize <= LINK_TX_BUFFER_SIZE;
  // Do not enqueue a truncated request: the entire frame must fit atomically.
  if (capacity)
    for (uint8_t i = 0; i < hardware_api::kFrameSize; ++i)
      arduino.sendPacket(bytes[i]);
  unlockInterrupts(saved);
  return capacity;
}

int main() {
  bus.setAddress(GATEWAY_NODE);
  detail::hal::initUartWrapper();
  arduino.init();
  uint16_t last_rx_overrun = 0, last_tx_drop = 0;
  uint8_t outgoing[hardware_api::kFrameSize];
  uint8_t outgoing_pos = hardware_api::kFrameSize;
  uint32_t last_bus_tx_end = 0;
  bool ever_sent = false;

  for (;;) {
    const uint32_t now = millis();
    gateway.tick(now);

    // Patched upstream RX ring exposes overflow and is drained continuously.
    if (detail::simpleBusHelpers::Api::takeOverflow()) gateway.overflow(now);
    const uint16_t rx_overrun = arduino_link_rx_overrun_count();
    const uint16_t tx_drop = arduino_link_tx_drop_count();
    if (rx_overrun != last_rx_overrun || tx_drop != last_tx_drop) {
      last_rx_overrun = rx_overrun;
      last_tx_drop = tx_drop;
      gateway.overflow(now);
    }
    if (gateway.takeCancelArduino()) cancelArduinoQueues();
    while (bus.packetReceived()) gateway.receiveBus(bus.lastData, millis());
    while (arduino.packetReceived())
      gateway.receiveArduino(arduino.lastData, millis());

    hardware_api::Frame frame;
    if (gateway.takeArduinoRequest(frame)) {
      uint8_t bytes[hardware_api::kFrameSize];
      hardware_api::encode(frame, bytes);
      if (!enqueueArduinoFrame(bytes)) gateway.overflow(millis());
    }
    if (gateway.takeCancelArduino()) cancelArduinoQueues();
    if (outgoing_pos == hardware_api::kFrameSize &&
        gateway.takeHubResponse(frame)) {
      hardware_api::encode(frame, outgoing);
      outgoing_pos = 0;
    }
    if (outgoing_pos < hardware_api::kFrameSize &&
        (!ever_sent || static_cast<uint32_t>(millis() - last_bus_tx_end) >= 2)) {
      // Upstream sendPacket blocks only for one five-byte UART packet (~5ms).
      // Never block waiting for the complete transaction or interpacket pause.
      bus.sendPacket(12, outgoing[outgoing_pos++]);
      last_bus_tx_end = millis();
      ever_sent = true;
      if (outgoing_pos == hardware_api::kFrameSize)
        gateway.hubResponseSent(last_bus_tx_end);
    }
  }
}

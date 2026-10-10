#include <assert.h>
#include <stdio.h>
#ifndef ACTUATOR_BOARD_IO_HEADER
#define ACTUATOR_BOARD_IO_HEADER "../arduino-actuator/src/BoardIO.h"
#endif
#include ACTUATOR_BOARD_IO_HEADER
GpioEvent events[256];
unsigned eventCount = 0;
uint32_t fakeMillis = 0;
void (*interruptHandlers[NUM_DIGITAL_PINS])() = {};

int main() {
  BoardIO board;
#ifdef HARDWARE_API_BOARD_HEADER
  assert(!board.setup());
  assert(eventCount == 0);  // Полностью некорректная карта не трогает GPIO.
  assert(!board.writePwm(0, 100));
  puts("actuator: conflicting map rejected before GPIO access");
  return 0;
#endif
  assert(board.setup());
  assert(eventCount == 6 * API_FAN_COUNT);
  for (uint8_t i = 0; i < API_FAN_COUNT; ++i) {
    const unsigned base = 6 * i;
    assert(events[base].action == 'D');
    assert(events[base].pin == actuator_config::relayPins[i]);
    assert(events[base].value == HIGH);
    assert(events[base+1].action == 'M' && events[base+1].value == OUTPUT);
    assert(events[base+2].action == 'D' && events[base+2].value == LOW);
    assert(events[base+4].action == 'A' && events[base+4].value == 0);
    assert(board.readPwm(i) == 0 && board.readRpm(i) == 0);
    assert(interruptHandlers[actuator_config::tachPins[i]]);
  }
  eventCount = 0;
  for (uint8_t i = 0; i < API_FAN_COUNT; ++i) {
    assert(board.writePwm(i, 100));
    assert(events[2*i].pin == actuator_config::relayPins[i]);
    assert(events[2*i].value == LOW && events[2*i+1].value == 100);
    assert(board.readPwm(i) == 100);
    for (unsigned pulse=0; pulse<4u*(i+1); ++pulse)
      interruptHandlers[actuator_config::tachPins[i]]();
  }
  board.poll(999);
  assert(board.readRpm(0) == 0);
  board.poll(1000);
  for (uint8_t i=0; i<API_FAN_COUNT; ++i)
    assert(board.readRpm(i) == 120u*(i+1));
  // Тишина на тахометре даёт RPM=0, но не меняет PWM или реле.
  const unsigned beforePoll = eventCount;
  board.poll(2000);
  assert(eventCount == beforePoll && board.readPwm(0) == 100);
  assert(board.readRpm(0) == 0);
  eventCount = 0;
  assert(board.writePwm(0, 0));
  assert(events[0].value == HIGH && events[1].value == 0);
  assert(!board.available(API_FAN_COUNT));
  assert(!board.writePwm(API_FAN_COUNT, 1));
  assert(eventCount == 2);
  hardware_api::ActuatorApi api(board, API_LOCAL_NODE, API_FAN_COUNT);
  assert(api.begin(0));
  hardware_api::Frame frame = {};
  frame.version=2; frame.kind=1; frame.node=API_LOCAL_NODE;
  frame.command=hardware_api::kSetPwm; frame.data[0]=255;
  frame.idx=API_FAN_COUNT-1;
  assert(api.handle(frame,0).data[0] == 255);
  frame.idx=API_FAN_COUNT;
  assert(api.handle(frame,0).status == hardware_api::INVALID_INDEX);
  frame.idx=API_FAN_COUNT-1; frame.command=hardware_api::kSetMode;
  frame.data[0]=1;
  assert(api.handle(frame,0).data[1] == API_FAN_COUNT-1);
  assert(board.readPwm(frame.idx) == 230);
  assert(api.tick(30000) && board.readPwm(frame.idx) == 230);
  frame.command=hardware_api::kGetMode; frame.idx=0xff; frame.data[0]=0;
  assert(api.handle(frame,30000).data[0] == 1);
  frame.command=hardware_api::kSetMode; frame.idx=0; frame.data[0]=0;
  assert(api.handle(frame,30000).data[1] == 0xff);
  assert(board.readPwm(API_FAN_COUNT-1) == 230); // Возврат mode0 не меняет PWM.
  frame.idx=0; frame.node=API_LOCAL_NODE == 14 ? 17 : 14;
  assert(api.handle(frame,0).status == hardware_api::INVALID_NODE);
  puts("actuator: board pins, safe relays, PWM, RPM and local routing passed");
}

#include <assert.h>
#include <stdio.h>
#include "../../../arduino-actuator/src/ActuatorApi.h"
#include "../../../arduino-sensor/src/SensorApi.h"
#include "../../../tjc-sketch/firmware/gateway/Gateway.h"
using namespace hardware_api;
using tjc_gateway::Gateway;
struct Fans : FanHardware {
  uint8_t pwm[6] = {};
  unsigned writes = 0;
  bool available(uint8_t idx) const override { return idx < 6; }
  bool writePwm(uint8_t idx, uint8_t value) override { ++writes; pwm[idx] = value; return true; }
  uint8_t readPwm(uint8_t idx) const override { return pwm[idx]; }
  uint32_t readRpm(uint8_t) const override { return 771; }
  uint8_t randomPwm() override { return 230; }
};
struct Sensors : SensorHardware {
  Status temperature(uint8_t, Measurement&) override { return NOT_IMPLEMENTED; }
  Status pressure(uint8_t, Measurement&) override { return NOT_IMPLEMENTED; }
  Status door(uint8_t idx, bool& closed) override { closed = idx == 0; return OK; }
  Status unsupported(uint8_t, uint8_t) override { return NOT_IMPLEMENTED; }
};
static Frame request(uint8_t node, uint8_t command, uint8_t idx, uint8_t value = 0) {
  static uint16_t id = 0;
  Frame frame = {}; frame.version = 1; frame.kind = 1; frame.tx_id = ++id;
  frame.node = node; frame.command = command; frame.idx = idx; frame.data[0] = value;
  return frame;
}
static Frame exchange(Gateway& gateway, ActuatorApi& actuator, SensorApi& sensors,
                      Frame request, uint32_t& now, bool loseReply = false) {
  uint8_t bytes[20]; encode(request, bytes);
  for (uint8_t i = 0; i < 20; ++i) gateway.receiveBus(bytes[i], ++now);
  Frame forwarded; assert(gateway.takeArduinoRequest(forwarded));
  uint8_t forwardedBytes[20]; encode(forwarded, forwardedBytes);
  assert(memcmp(bytes, forwardedBytes, 20) == 0);
  StreamParser arduinoParser; Frame command;
  for (uint8_t i = 0; i < 20; ++i)
    assert(arduinoParser.feed(forwardedBytes[i], ++now, command) == (i == 19));
  Frame reply = command.node == kActuatorNode ? actuator.handle(command, now)
                                             : sensors.handle(command);
  encode(reply, bytes);
  if (!loseReply) {
    Frame foreign = reply; ++foreign.tx_id; encode(foreign, forwardedBytes);
    for (uint8_t i = 0; i < 20; ++i) gateway.receiveArduino(forwardedBytes[i], ++now);
    assert(gateway.state() == Gateway::Waiting);
    for (uint8_t i = 0; i < 20; ++i) gateway.receiveArduino(bytes[i], ++now);
  } else {
    now += 1500; gateway.tick(now);
  }
  Frame result; assert(gateway.takeHubResponse(result));
  assert(sameRequest(request, result));
  if (!loseReply) { encode(result, forwardedBytes); assert(memcmp(bytes, forwardedBytes, 20) == 0); }
  gateway.hubResponseSent(now);
  if (loseReply) {
    assert(result.status == DEVICE_TIMEOUT && gateway.takeCancelArduino());
    // The command WAS applied; its lost response cannot justify a retry.
    for (uint8_t i = 0; i < 20; ++i) gateway.receiveArduino(bytes[i], ++now);
    now += 250; gateway.tick(now); assert(gateway.state() == Gateway::Idle);
  }
  return result;
}
int main() {
  uint32_t now = 250;
  Gateway gateway13(13), gateway14(14);
  gateway13.tick(now); gateway14.tick(now);
  gateway13.takeCancelArduino(); gateway14.takeCancelArduino();
  Fans fans; ActuatorApi actuator(fans); assert(actuator.begin(0));
  Sensors hardware; SensorApi sensors(hardware);
  assert(exchange(gateway14, actuator, sensors, request(14, kSetMode, 3, 2), now).status == OK);
  Frame reply = exchange(gateway14, actuator, sensors, request(14, kSetPwm, 3, 100), now);
  assert(reply.status == OK && reply.data[0] == 230 && fans.pwm[3] == 230);
  reply = exchange(gateway14, actuator, sensors, request(14, kGetRpm, 3), now);
  assert(readU32(reply.data) == 771);
  assert(exchange(gateway14, actuator, sensors, request(14, kSetPwm, 6, 100), now).status == INVALID_INDEX);
  assert(exchange(gateway13, actuator, sensors, request(13, kGetTemp, 2), now).status == NOT_IMPLEMENTED);
  assert(exchange(gateway13, actuator, sensors, request(13, kGetTemp, 3), now).status == INVALID_INDEX);
  assert(exchange(gateway13, actuator, sensors, request(13, kGetDoor, 1), now).data[0] == 0);
  unsigned writes = fans.writes;
  exchange(gateway14, actuator, sensors, request(14, kSetPwm, 3, 100), now, true);
  assert(fans.writes == writes + 1 && fans.pwm[3] == 230);
  assert(exchange(gateway14, actuator, sensors, request(14, kGetPwm, 3), now).data[0] == 230);
  puts("integration: both gateways + Arduino handlers, CRC transport, applied PWM, lost replies passed");
}

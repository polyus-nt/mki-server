#include <assert.h>
#include <stdio.h>
#include <string>
#include <iostream>
#include "../firmware/hub/Hub.h"
#include "../firmware/gateway/Gateway.h"
#include "../firmware/buttons/ButtonsApi.h"
#include "../firmware/lmp8/Lmp8Api.h"
#include "../arduino-actuator/src/ActuatorApi.h"
#include "../arduino-sensors/src/SensorApi.h"
#include "../arduino-sensors/MQ2.h"
using namespace hardware_api;

struct Sensors : SensorHardware {
  Status measure(uint8_t command, uint8_t idx, Measurement& m) override {
    m.flags = 3; m.ageMs = 123;
    if (command == kGetTemp) m.value = static_cast<uint32_t>(-1234 + idx);
    else if (command == kGetPressure) m.value = 100653 + idx;
    else if (command == kGetHumidity) m.value = 5550 + idx;
    else if (command == kGetGas) m.value = (512UL << 16) | 2502;
    else return UNKNOWN_COMMAND;
    return OK;
  }
};
struct Fans : FanHardware {
  uint8_t pwm[2] = {}; unsigned writes = 0;
  bool available(uint8_t idx) const override { return idx < 2; }
  bool writePwm(uint8_t idx, uint8_t value) override {
    if (idx >= 2) return false;
    pwm[idx] = value; ++writes; return true;
  }
  uint8_t readPwm(uint8_t idx) const override { return pwm[idx]; }
  uint32_t readRpm(uint8_t idx) const override { return 1200 + idx; }
  uint8_t randomPwm() override { return 230; }
};
struct Outputs {
  bool locked = false; uint8_t mask = 0; unsigned writes = 0;
  void lock(bool state) { locked = state; }
  void relays(uint8_t state) { mask = state; ++writes; }
};
template<class Receiver> void feed(Receiver& receiver, const Frame& frame, uint32_t now) {
  uint8_t bytes[kFrameSize]; encode(frame, bytes);
  for (unsigned i = 0; i < kFrameSize; ++i) receiver.receiveBus(bytes[i], now);
}
Frame request(uint8_t node, uint8_t cmd, uint8_t idx = 0, uint8_t value = 0) {
  static uint16_t id = 0;
  Frame f = {}; f.version = 2; f.kind = kRequest; f.node = node;
  f.command = cmd; f.idx = idx; f.data[0] = value; f.tx_id = ++id; f.session_id = 123;
  return f;
}
// Production state machines and API classes, only physical GPIO replaced.
struct Chain {
  Sensors sensors; Fans fans; Outputs outputs;
  SensorApi sensorApi; ActuatorApi actuatorApi;
  tjc_lmp8::Lmp8Api<Outputs> lmp;
  tjc_buttons::ButtonsApi buttons;
  Chain() : sensorApi(sensors), actuatorApi(fans, 14, 2), lmp(outputs) {
    assert(actuatorApi.begin(0)); buttons.sample(true, false, 100);
  }
  Frame execute(Frame f) {
    tjc_hub::Hub hub; hub.tick(250); feed(hub, f, 300);
    Frame down, reply;
    if (hub.takeGatewayRequest(down)) {
      hub.gatewayRequestSent(300);
      if (down.node == 13 || down.node == 14) {
        tjc_gateway::Gateway gateway(down.node); gateway.tick(250);
        feed(gateway, down, 300); Frame arduino;
        assert(gateway.takeArduinoRequest(arduino));
        reply = down.node == 13 ? sensorApi.handle(arduino) : actuatorApi.handle(arduino, 300);
        uint8_t bytes[kFrameSize]; encode(reply, bytes);
        for (unsigned i = 0; i < kFrameSize; ++i) gateway.receiveArduino(bytes[i], 400);
        assert(gateway.takeHubResponse(reply)); gateway.hubResponseSent(400);
      } else if (down.node == 15) reply = buttons.execute(down);
      else {
        assert(down.node == 16); lmp.receive(down, 300);
        if (down.command == kSetRelayLevel) {
          assert(outputs.mask == 0); lmp.tick(549); assert(!lmp.takeReply(reply));
          lmp.tick(550);
        }
        assert(lmp.takeReply(reply));
      }
      feed(hub, reply, 600);
    }
    assert(hub.takeHostResponse(reply)); assert(sameRequest(f, reply));
    hub.hostResponseSent(700); return reply;
  }
};

void tests() {
  Chain chain;
  for (unsigned idx = 0; idx < 2; ++idx) {
    for (uint8_t cmd : {kGetTemp, kGetPressure, kGetHumidity, kGetGas}) {
      Frame r = chain.execute(request(13, cmd, idx)); assert(r.status == OK && r.data[4] == 3);
    }
    assert(chain.execute(request(15, kGetDoor, idx)).data[0] == (idx == 0));
    assert(chain.execute(request(14, kSetPwm, idx, 100)).data[0] == 100);
    assert(readU32(chain.execute(request(14, kGetRpm, idx)).data) == 1200 + idx);
  }
  assert(chain.execute(request(13, kGetTemp, 2)).status == INVALID_INDEX);
  assert(chain.execute(request(14, kGetPwm, 2)).status == INVALID_INDEX);
  assert(chain.execute(request(17, kGetPwm)).status == INVALID_NODE);
  for (uint8_t cmd : {kGetVoc, kGetDust, kGetVacuum, kSetVacuum})
    assert(chain.execute(request(cmd < 0x30 ? 13 : 16, cmd)).status == UNKNOWN_COMMAND);
  assert(chain.execute(request(16, kSetDoorLock, 1, 1)).status == INVALID_INDEX);
  assert(chain.execute(request(16, kSetDoorLock, 0, 1)).status == OK);
  assert(chain.outputs.locked && chain.execute(request(16, kGetDoorLock)).data[0] == 1);
  assert(chain.execute(request(16, kSetDoorLock, 0, 0)).status == OK && !chain.outputs.locked);
  const uint8_t masks[] = {0,49,51,55,63,33,17,35,39,47,1};
  for (uint8_t level = 0; level <= 10; ++level) {
    Frame r = chain.execute(request(16, kSetRelayLevel, 0, level));
    assert(r.status == OK && r.data[0] == level && chain.outputs.mask == masks[level]);
    assert(chain.execute(request(16, kGetRelayLevel)).data[0] == level);
  }
  assert(chain.execute(request(16, kSetRelayLevel, 0, 11)).status == INVALID_ARGUMENT);
  assert(chain.execute(request(14, kSetMode, 1, 2)).data[0] == 2);
  assert(chain.execute(request(14, kSetPwm, 1, 100)).data[0] == 230);
  assert(chain.execute(request(14, kGetPwm, 1)).data[0] == 230);
  assert(chain.execute(request(14, kSetMode, 0, 0)).data[1] == 255);
  assert(chain.fans.pwm[1] == 230);
  assert(chain.execute(request(14, kSetMode, 0, 1)).data[0] == 1);
  const unsigned writes = chain.fans.writes;
  assert(chain.execute(request(14, kSetMode, 0, 1)).status == OK && chain.fans.writes == writes);
  assert(chain.actuatorApi.tick(30300) && chain.fans.writes == writes + 1);

  // Пауза реле переносит wrap millis, дубликат не продлевает её.
  Outputs hw; tjc_lmp8::Lmp8Api<Outputs> api(hw);
  Frame f = request(16, kSetRelayLevel, 0, 7), r;
  api.receive(f, 0xffffff80UL); api.receive(f, 0xffffff90UL);
  api.tick(0x79); assert(!api.takeReply(r)); api.tick(0x7a);
  assert(api.takeReply(r) && r.data[0] == 7 && hw.mask == 35);
  const unsigned before = hw.writes;
  api.receive(f, 0x100); assert(api.takeReply(r) && hw.writes == before);
  f.data[0] = 8; api.receive(f, 0x101);
  assert(api.takeReply(r) && r.status == REQUEST_ID_CONFLICT && hw.writes == before);

  struct Reader : MQ2::Reader { bool read(uint16_t& adc) override { adc = 512; return true; } } reader;
  MQ2 gas; gas.attach(reader, 5000, 0); assert(gas.poll(0));
  auto value = gas.read(123); assert(value.valid && value.fresh && value.ageMs == 123);
  assert(value.adc == 512 && value.millivolts == 2502 && !gas.read(124).fresh);
  assert(!gas.poll(999) && gas.poll(1000)); assert(gas.read(70000).ageMs == 65534);

  tjc_hub::Hub hub; hub.tick(250); feed(hub, request(13, kGetPressure), 300);
  assert(hub.takeGatewayRequest(f)); hub.gatewayRequestSent(300); hub.tick(3300);
  assert(hub.takeHostResponse(r) && r.status == DEVICE_TIMEOUT);

  // CRC, чужая сессия и idx не должны дать ложный успешный ответ.
  tjc_hub::Hub checkedHub; checkedHub.tick(250);
  f = request(13, kGetPressure); feed(checkedHub, f, 300);
  assert(checkedHub.takeGatewayRequest(r)); checkedHub.gatewayRequestSent(300);
  Frame wrong = response(f); wrong.session_id += 1;
  feed(checkedHub, wrong, 400); assert(!checkedHub.takeHostResponse(r));
  wrong = response(f); wrong.idx = 1; feed(checkedHub, wrong, 401);
  assert(!checkedHub.takeHostResponse(r));
  StreamParser parser; uint8_t bytes[kFrameSize]; encode(f, bytes); bytes[27] ^= 1;
  for (uint8_t byte : bytes) assert(!parser.feed(byte, 500, r));
  encode(f, bytes); unsigned accepted = 0;
  for (uint8_t byte : bytes) if (parser.feed(byte, 501, r)) ++accepted;
  assert(accepted == 1);
  feed(checkedHub, response(f), 600); assert(checkedHub.takeHostResponse(r));

  // Посторонняя команда во время паузы не включает реле и не теряет исходную SET.
  Outputs busyHw; tjc_lmp8::Lmp8Api<Outputs> busyApi(busyHw);
  f = request(16, kSetRelayLevel, 0, 10); busyApi.receive(f, 1000);
  busyApi.receive(request(16, kGetRelayLevel), 1100);
  assert(busyApi.takeReply(r) && r.status == BUSY && busyHw.mask == 0);
  busyApi.tick(1250); assert(busyApi.takeReply(r) && r.data[0] == 10 && busyHw.mask == 1);
  puts("chemreactor: hub/gateway/sensors/fans/buttons/lmp8, all levels, modes, wrap and timeout passed");
}
int main(int argc, char**) {
  if (argc == 1) { tests(); return 0; }
  // HEX stdin/stdout bridge for HTTP -> pyserial -> production C++ chain tests.
  Chain chain; std::string line;
  while (std::getline(std::cin, line)) {
    assert(line.size() == kFrameSize * 2);
    uint8_t bytes[kFrameSize];
    for (unsigned i = 0; i < kFrameSize; ++i)
      bytes[i] = static_cast<uint8_t>(std::stoul(line.substr(i * 2, 2), 0, 16));
    Frame f; assert(decode(bytes, f)); const Frame r = chain.execute(f); encode(r, bytes);
    for (uint8_t b : bytes) printf("%02x", b);
    puts(""); fflush(stdout);
  }
}

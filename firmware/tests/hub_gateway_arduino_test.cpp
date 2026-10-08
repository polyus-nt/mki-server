#include <assert.h>
#include <stdio.h>
#include "../../../arduino-actuator/src/ActuatorApi.h"
#include "../../../arduino-sensor/src/SensorApi.h"
#include "../../../tjc-sketch/firmware/gateway/Gateway.h"
#include "../../../tjc-sketch/firmware/hub/Hub.h"

using namespace hardware_api;
using tjc_gateway::Gateway;
using tjc_hub::Hub;

struct TestFans : FanHardware {
  uint8_t pwm[6] = {};
  unsigned writes = 0;
  bool available(uint8_t idx) const override { return idx < 6; }
  bool writePwm(uint8_t idx, uint8_t value) override {
    ++writes; pwm[idx] = value; return true;
  }
  uint8_t readPwm(uint8_t idx) const override { return pwm[idx]; }
  uint32_t readRpm(uint8_t) const override { return 771; }
  uint8_t randomPwm() override { return 230; }
};

struct TestSensors : SensorHardware {
  Status temperature(uint8_t, Measurement& result) override {
    result.value = static_cast<uint32_t>(-1234); result.flags = 3; result.ageMs = 25;
    return OK;
  }
  Status pressure(uint8_t, Measurement& result) override {
    result.value = 101325; result.flags = 1; result.ageMs = 250;
    return OK;
  }
  Status voc(uint8_t, Measurement& result) override {
    result.value = 230; result.flags = 3; result.ageMs = 500;
    return OK;
  }
  Status humidity(uint8_t, Measurement& result) override {
    result.value = 5550; result.flags = 3; result.ageMs = 2000;
    return OK;
  }
  Status door(uint8_t idx, bool& closed) override { closed = idx == 0; return OK; }
  Status unsupported(uint8_t, uint8_t) override { return NOT_IMPLEMENTED; }
};

static void bus(Hub& hub, const Frame& frame, uint32_t& now) {
  uint8_t bytes[kFrameSize]; encode(frame, bytes);
  for (uint8_t i = 0; i < kFrameSize; ++i) hub.receiveBus(bytes[i], ++now);
}

static Frame command(uint16_t id, uint8_t code, uint8_t idx, uint8_t value = 0) {
  Frame frame = {};
  frame.version = kVersion; frame.kind = kRequest; frame.tx_id = id;
  frame.node = kActuatorNode; frame.command = code;
  frame.idx = idx; frame.data[0] = value;
  return frame;
}

// Потеря ответа после применения PWM: результат TIMEOUT, но не повтор команды.
static Frame exchange(Hub& hub, Gateway& gateway, ActuatorApi& arduino,
                      const Frame& request, uint32_t& now, bool loseReply = false,
                      SensorApi* sensors = 0) {
  bus(hub, request, now);
  Frame forwarded; assert(hub.takeGatewayRequest(forwarded));
  uint8_t bytes[kFrameSize]; encode(forwarded, bytes);
  for (uint8_t i = 0; i < kFrameSize; ++i) gateway.receiveBus(bytes[i], ++now);
  hub.gatewayRequestSent(now);
  Frame local; assert(gateway.takeArduinoRequest(local));
  const Frame applied = sensors ? sensors->handle(local) : arduino.handle(local, now);
  if (loseReply) {
    now += 1500; gateway.tick(now);
  } else {
    encode(applied, bytes);
    for (uint8_t i = 0; i < kFrameSize; ++i) gateway.receiveArduino(bytes[i], ++now);
  }
  Frame reply; assert(gateway.takeHubResponse(reply));
  bus(hub, reply, now); gateway.hubResponseSent(now);
  Frame result; assert(hub.takeHostResponse(result));
  assert(sameRequest(request, result)); hub.hostResponseSent(now);
  if (loseReply) {
    assert(gateway.takeCancelArduino());
    encode(applied, bytes);
    for (uint8_t i = 0; i < kFrameSize; ++i) gateway.receiveArduino(bytes[i], ++now);
    bus(hub, applied, now);  // Поздний ответ не превращается в новый запрос.
    now += 250; gateway.tick(now); hub.tick(now);
  }
  return result;
}

int main() {
  uint32_t now = 250;
  Hub hub; Gateway gateway(kActuatorNode);
  hub.tick(now); gateway.tick(now);
  assert(hub.takeCancelTransmission()); assert(gateway.takeCancelArduino());
  TestFans fans; ActuatorApi arduino(fans); assert(arduino.begin(0));
  Frame reply = exchange(hub, gateway, arduino, command(1, kSetPwm, 3, 100), now);
  assert(reply.status == OK && reply.data[0] == 100 && fans.pwm[3] == 100);
  reply = exchange(hub, gateway, arduino, command(2, kSetMode, 3, 2), now);
  assert(reply.status == OK && reply.data[0] == 2 && reply.data[1] == 3);
  reply = exchange(hub, gateway, arduino, command(3, kSetPwm, 3, 100), now);
  assert(reply.status == OK && reply.data[0] == 230 && fans.pwm[3] == 230);
  const unsigned writes = fans.writes;
  reply = exchange(hub, gateway, arduino, command(4, kSetPwm, 3, 100), now, true);
  assert(reply.status == DEVICE_TIMEOUT && fans.writes == writes + 1);
  reply = exchange(hub, gateway, arduino, command(5, kGetPwm, 3), now);
  assert(reply.status == OK && reply.data[0] == 230);
  // Неверный индекс отсекается в хабе, не доходит до аппаратуры.
  bus(hub, command(6, kSetPwm, 6, 100), now);
  assert(!hub.takeGatewayRequest(reply)); assert(hub.takeHostResponse(reply));
  assert(reply.status == INVALID_INDEX); hub.hostResponseSent(now);
  // Мёртвый шлюз: один таймаут, отмена передачи, затем восстановление.
  bus(hub, command(7, kGetRpm, 0), now);
  assert(hub.takeGatewayRequest(reply)); hub.gatewayRequestSent(now);
  now += Hub::kTransactionMs; hub.tick(now);
  assert(hub.takeCancelTransmission()); assert(hub.takeHostResponse(reply));
  assert(reply.status == DEVICE_TIMEOUT); hub.hostResponseSent(now);
  assert(hub.state() == Hub::Recovering);
  // Сервер обязан выдержать восстановление и после ОТВЕТА с тайм-аутом,
  // а не только после собственного deadline: слишком ранний кадр дренируется.
  bus(hub, command(8, kGetPwm, 0), now);
  assert(!hub.takeGatewayRequest(reply)); assert(hub.state() == Hub::Recovering);
  now += Hub::kQuietMs; hub.tick(now); assert(hub.state() == Hub::Idle);
  // Тот же хаб маршрутизирует второй шлюз, не изменяя единицы и quality.
  Gateway sensorGateway(kSensorsNode); sensorGateway.tick(now);
  sensorGateway.takeCancelArduino();
  TestSensors sensorHardware; SensorApi sensors(sensorHardware);
  const uint8_t commands[] = {kGetTemp, kGetPressure, kGetVoc, kGetHumidity,
                              kGetDoor, kGetDust};
  const uint32_t values[] = {static_cast<uint32_t>(-1234), 101325, 230, 5550, 1, 0};
  for (uint8_t i = 0; i < sizeof(commands); ++i) {
    Frame request = command(static_cast<uint16_t>(9 + i), commands[i], 0);
    request.node = kSensorsNode;
    reply = exchange(hub, sensorGateway, arduino, request, now, false, &sensors);
    if (commands[i] == kGetDust) {
      assert(reply.status == NOT_IMPLEMENTED && zeros(reply.data, 8));
    } else {
      assert(reply.status == OK);
      if (commands[i] == kGetDoor) assert(reply.data[0] == values[i]);
      else assert(readU32(reply.data) == values[i]);
    }
  }
  puts("end-to-end: hub + gateway + Arduino, applied PWM, lost replies, recovery passed");
}

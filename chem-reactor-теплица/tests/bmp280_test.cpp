#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "../arduino-sensors/BMP280.h"

static const BMP280::Calibration reference = {
  27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000
};
struct Bus : BMP280::Bus {
  BMP280::Calibration calibration = reference;
  uint32_t adcT = 519888, adcP = 415148;
  uint8_t id = 0x58, status = 0;
  bool failRead = false, failWrite = false;
  unsigned operations = 0, samples = 0, writes = 0;
  uint8_t writtenReg[16] = {}, writtenValue[16] = {};
  static void little(uint8_t* bytes, uint16_t value) {
    bytes[0] = static_cast<uint8_t>(value); bytes[1] = static_cast<uint8_t>(value >> 8);
  }
  static void raw20(uint8_t* bytes, uint32_t value) {
    bytes[0] = static_cast<uint8_t>(value >> 12);
    bytes[1] = static_cast<uint8_t>(value >> 4); bytes[2] = static_cast<uint8_t>(value << 4);
  }
  bool readRegisters(uint8_t reg, uint8_t* data, uint8_t count) override {
    ++operations; if (failRead) return false;
    if (reg == 0xd0 && count == 1) { data[0] = id; return true; }
    if (reg == 0xf3 && count == 1) { data[0] = status; return true; }
    if (reg == 0x88 && count == 24) {
      const uint16_t values[12] = {calibration.T1, static_cast<uint16_t>(calibration.T2),
        static_cast<uint16_t>(calibration.T3), calibration.P1,
        static_cast<uint16_t>(calibration.P2), static_cast<uint16_t>(calibration.P3),
        static_cast<uint16_t>(calibration.P4), static_cast<uint16_t>(calibration.P5),
        static_cast<uint16_t>(calibration.P6), static_cast<uint16_t>(calibration.P7),
        static_cast<uint16_t>(calibration.P8), static_cast<uint16_t>(calibration.P9)};
      for (uint8_t i = 0; i < 12; ++i) little(data + i * 2, values[i]);
      return true;
    }
    if (reg == 0xf7 && count == 6) { ++samples; raw20(data, adcP); raw20(data + 3, adcT); return true; }
    return false;
  }
  bool writeRegister(uint8_t reg, uint8_t value) override {
    ++operations; if (failWrite) return false;
    writtenReg[writes % 16] = reg; writtenValue[writes++ % 16] = value; return true;
  }
};
static void initialize(BMP280& sensor, uint32_t start) {
  for (uint8_t i = 0; i < 7; ++i) sensor.poll(static_cast<uint32_t>(start + i * 5UL));
  assert(sensor.isConnected());
}
int main() {
  float temperature, pressure;
  assert(BMP280::compensate(reference, 519888, 415148, temperature, pressure));
  assert(fabs(temperature - 25.0824779f) < 0.001f);
  assert(fabs(pressure - 100653.2668f) < 0.1f); // Pa, NOT hPa.
  BMP280::Calibration bad = reference; bad.P1 = 0;
  assert(!BMP280::compensate(bad, 519888, 415148, temperature, pressure));
  assert(!BMP280::compensate(reference, 0x80000, 415148, temperature, pressure));
  assert(!BMP280::compensate(reference, 519888, 0x80000, temperature, pressure));
  assert(!BMP280::compensate(reference, 0x100000, 415148, temperature, pressure));
  BMP280 unconfigured;
  assert(!unconfigured.isConfigured() && !unconfigured.poll(1000));
  assert(!unconfigured.read().valid && isnan(unconfigured.read().value));
  Bus bus; BMP280 sensor(&bus); sensor.setup(1000);
  initialize(sensor, 1000);
  assert(bus.writes == 3 && bus.writtenReg[0] == 0xf4 && bus.writtenValue[0] == 0);
  assert(bus.writtenReg[1] == 0xf5 && bus.writtenValue[1] == 0);
  assert(bus.writtenReg[2] == 0xf4 && bus.writtenValue[2] == 0x27);
  assert(!sensor.read(1030).valid);
  assert(sensor.poll(1130));
  BMP280::Reading t = sensor.readTemperature(1135), p = sensor.read(1135);
  assert(t.valid && t.fresh && p.valid && p.fresh && t.ageMs == 5 && p.ageMs == 5);
  assert(fabs(t.value - 25.0824779f) < .001f && fabs(p.value - 100653.2668f) < .1f);
  assert(!sensor.readTemperature(1140).fresh && !sensor.read(1140).fresh);
  const unsigned operations = bus.operations;
  sensor.poll(1131); assert(bus.operations == operations); // No redundant sample inside 100ms.
  assert(sensor.poll(1230));
  bus.failRead = true; assert(!sensor.poll(1330)); assert(!sensor.isConnected());
  p = sensor.read(1330); t = sensor.readTemperature(1330);
  assert(p.valid && t.valid && !p.fresh && !t.fresh && p.ageMs == 100);
  assert(sensor.read(80000).ageMs == 65534);
  unsigned failedOperations = bus.operations;
  sensor.poll(2000); assert(bus.operations == failedOperations);
  bus.failRead = false; initialize(sensor, 2330); assert(sensor.poll(2460));
  assert(sensor.read(2460).fresh);
  Bus first, second, third; second.calibration.T2 = 20000; third.adcT = 400000;
  BMP280 sensors[3] = {BMP280(&first), BMP280(&second), BMP280(&third)};
  for (uint8_t i = 0; i < 3; ++i) { initialize(sensors[i], 0); assert(sensors[i].poll(130)); }
  assert(sensors[0].readTemperature(130).value != sensors[1].readTemperature(130).value);
  assert(sensors[2].readTemperature(130).value < 0); // Negative Celsius remains signed.
  Bus broken; broken.id = 0x60; BMP280 wrongChip(&broken);
  assert(!wrongChip.poll(0) && !wrongChip.isConnected() && broken.writes == 0);
  broken.id = 0x58; broken.calibration.P1 = 0;
  for (uint32_t now = 1000; now <= 1010; now += 5) wrongChip.poll(now);
  assert(!wrongChip.isConnected() && broken.writes == 0);
  Bus busy; busy.status = 1; BMP280 nvm(&busy);
  nvm.poll(0); nvm.poll(5); assert(!nvm.isConnected());
  nvm.poll(250); assert(!nvm.isConnected() && busy.writes == 0);
  Bus wrappedBus; BMP280 wrapped(&wrappedBus);
  const uint32_t start = 0xfffffff0UL; wrapped.setup(start); initialize(wrapped, start);
  assert(wrapped.poll(static_cast<uint32_t>(start + 130UL)));
  assert(wrapped.read(static_cast<uint32_t>(start + 140UL)).ageMs == 10);
  puts("BMP280: compensation, calibration isolation, retries, stale/fresh, units, wrap passed");
}

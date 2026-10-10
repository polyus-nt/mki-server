#include <cassert>
#include <cstdio>
#include "../arduino-sensors/DHT11.h"
#include "../arduino-sensors/src/SensorApi.h"
struct Reader : DHT11::Reader {
  float value = 55.5f;
  bool ok = true;
  unsigned begins = 0, reads = 0;
  uint32_t duration = 25;
  void begin() override { ++begins; }
  bool humidity(float& percent) override { ++reads; percent = value; return ok; }
  uint32_t completedAt(uint32_t now) const override { return now + duration; }
};
struct Hardware : hardware_api::SensorHardware {
  uint32_t value = 5550;
  hardware_api::Status measure(uint8_t, uint8_t idx, hardware_api::Measurement& result) override {
    result = {value + idx, 3, 25}; return hardware_api::OK;
  }
};
int main() {
  DHT11 absent; assert(!absent.poll(2000) && !absent.read(2000).valid);
  Reader a, b, c; b.value = 0; c.value = 100;
  DHT11 sensors[3]; sensors[0].attach(a); sensors[1].attach(b); sensors[2].attach(c);
  assert(a.begins == 1 && b.begins == 1 && c.begins == 1);
  for (auto& sensor : sensors) assert(!sensor.poll(1999));
  for (auto& sensor : sensors) assert(sensor.poll(2000));
  auto first = sensors[0].read(2025);
  assert(first.valid && first.fresh && first.value == 55.5f && first.ageMs == 0);
  assert(!sensors[0].read(2025).fresh);
  assert(sensors[1].read(2025).value == 0 && sensors[2].read(2025).value == 100);
  assert(!sensors[0].poll(4024) && a.reads == 1);
  a.ok = false; assert(!sensors[0].poll(4025));
  auto stale = sensors[0].read(4050);
  assert(stale.valid && !stale.fresh && stale.value == first.value && stale.ageMs == 2025);
  assert(!sensors[0].isConnected() && sensors[1].isConnected());
  a.ok = true; a.value = NAN; assert(!sensors[0].poll(6050));
  a.value = 100.1f; assert(!sensors[0].poll(8075));
  a.value = -0.1f; assert(!sensors[0].poll(10100));
  a.value = 80; assert(sensors[0].poll(12125));
  assert(sensors[0].read(80000).ageMs == 65534);
  assert(sensors[0].poll(100000)); // один пропущенный интервал -> одно чтение
  assert(!sensors[0].poll(100025)); // реальное время после синхронного чтения
  Reader wrap; wrap.duration = 0; DHT11 wrapped;
  wrapped.attach(wrap, 0xffffff00UL);
  assert(!wrapped.poll(1743) && wrapped.poll(1744));
  assert(wrapped.read(1754).ageMs == 10);
  Hardware hardware; hardware_api::SensorApi api(hardware);
  hardware_api::Frame request = {};
  request.version = hardware_api::kVersion; request.kind = 1; request.node = 13; request.command = hardware_api::kGetHumidity;
  for (uint8_t idx = 0; idx < 2; ++idx) {
    request.idx = idx; auto reply = api.handle(request);
    assert(reply.status == hardware_api::OK && hardware_api::readU32(reply.data) == 5550U + idx);
    assert(reply.data[4] == 3 && hardware_api::readU16(reply.data + 5) == 25 && reply.data[7] == 0);
  }
  request.idx = 2; assert(api.handle(request).status == hardware_api::INVALID_INDEX);
  request.idx = 0; hardware.value = 0; assert(api.handle(request).status == hardware_api::OK);
  hardware.value = 10000; assert(api.handle(request).status == hardware_api::OK);
  hardware.value = 10001; assert(api.handle(request).status == hardware_api::INTERNAL_ERROR);
  std::puts("DHT11: three caches, startup, period, errors, completion timestamp, wrap, humidity wire passed");
}

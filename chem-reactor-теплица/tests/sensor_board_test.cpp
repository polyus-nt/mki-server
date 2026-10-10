#include <assert.h>
#include <stdio.h>
#include <Arduino.h>
uint32_t fakeMillis = 0;
unsigned adcReads = 0;
// Электрические I2C/DHT подменены, production BoardIO и кэши настоящие.
#define SoftwareWire_h
class SoftwareWire {
 public:
  SoftwareWire(uint8_t, uint8_t, bool) {}
  void begin() {}
  void setClock(uint32_t) {}
  void setTimeout(uint32_t) {}
  void beginTransmission(uint8_t) {}
  unsigned write(uint8_t) { return 1; }
  uint8_t endTransmission(bool = true) { return 0; }
  bool getTimeoutFlag() { return false; }
  uint8_t requestFrom(uint8_t, uint8_t count) { return count; }
  int read() { return 0; } // Нет правильного chip ID, не фабрикуем давление.
};
#define DHT_H
const uint8_t DHT_TYPE_11 = 11;
class DHT {
 public:
  DHT(uint8_t pin, uint8_t) : pin_(pin) {}
  void begin() {}
  float readHumidity(bool force) { assert(force); return pin_ == 2 ? 55.5f : 80.0f; }
 private:
  uint8_t pin_;
};
#include "../arduino-sensors/src/BoardIO.h"
int main() {
  BoardIO board; assert(board.setup());
  assert(API_BMP_SCL_PIN == 4 && API_BMP_SDA0_PIN == 5 && API_BMP_SDA1_PIN == 6);
  assert(API_DHT0_PIN == 2 && API_DHT1_PIN == 3);
  assert(API_GAS0_PIN == A1 && API_GAS1_PIN == A0);
  assert(API_LINK_MISO == 9 && API_LINK_IRQ == 13);
  for (fakeMillis = 0; fakeMillis < 5000; ++fakeMillis) board.poll(fakeMillis);
  hardware_api::Measurement m;
  assert(board.measure(hardware_api::kGetTemp, 0, m) == hardware_api::SENSOR_UNAVAILABLE);
  assert(board.measure(hardware_api::kGetHumidity, 0, m) == hardware_api::OK && m.value == 5550);
  assert(board.measure(hardware_api::kGetHumidity, 1, m) == hardware_api::OK && m.value == 8000);
  assert(board.measure(hardware_api::kGetGas, 0, m) == hardware_api::OK);
  assert(m.value == ((512UL << 16) | 2502) && m.flags == 3);
  const unsigned before = adcReads;
  assert(board.measure(hardware_api::kGetGas, 0, m) == hardware_api::OK && m.flags == 1);
  assert(adcReads == before); // GET берёт кэш, не дёргает ADC повторно.
  assert(board.measure(hardware_api::kGetGas, 1, m) == hardware_api::OK);
  assert(m.value == ((1023UL << 16) | 5000));
  assert(board.measure(hardware_api::kGetGas, 2, m) == hardware_api::INVALID_INDEX);
  puts("chem sensors: actual pin map, round robin, DHT, ADC, cache and missing BMP passed");
}

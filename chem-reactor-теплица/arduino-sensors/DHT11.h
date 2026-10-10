#pragma once
#include <math.h>
#include <stdint.h>

// Один DHT11, независимые период опроса и кэш. Физический Reader внедряется
// извне: класс не знает пинов, Arduino или способа побайтовой передачи.
class DHT11 {
 public:
  class Reader {
   public:
    virtual ~Reader() {}
    virtual void begin() = 0;
    // true только для нового чтения с правильной контрольной суммой.
    virtual bool humidity(float& percent) = 0;
    virtual uint32_t completedAt(uint32_t now) const { return now; }
  };
  struct Reading { float value; bool valid; bool fresh; uint16_t ageMs; };
  explicit DHT11(Reader* reader = nullptr) : reader_(reader) { setup(); }
  void setup(uint32_t now = 0) {
    value_ = NAN; valid_ = fresh_ = connected_ = false;
    // После подачи питания ждём 2 секунды; не читаем ещё не готовый DHT11.
    lastAttempt_ = measuredAt_ = now;
  }
  void attach(Reader& reader, uint32_t now = 0) {
    reader_ = &reader; setup(now); reader_->begin();
  }
  bool poll(uint32_t now) {
    if (!reader_ || static_cast<uint32_t>(now - lastAttempt_) < 2000UL) return false;
    float percent = NAN;
    const bool success = reader_->humidity(percent);
    const uint32_t completed = reader_->completedAt(now);
    // Интервал от конца попытки: не делаем burst и не возвращаем библиотечный
    // кэш как fresh. Следующее физическое чтение не раньше чем через 2 с.
    lastAttempt_ = completed;
    if (!success || !isfinite(percent) || percent < 0 || percent > 100) {
      connected_ = fresh_ = false; return false; // старый valid-кэш сохраняется
    }
    value_ = percent; measuredAt_ = completed;
    valid_ = fresh_ = connected_ = true; return true;
  }
  Reading read(uint32_t now) {
    const uint32_t age = static_cast<uint32_t>(now - measuredAt_);
    Reading result = {valid_ ? value_ : NAN, valid_, valid_ && fresh_,
                      static_cast<uint16_t>(age > 65534UL ? 65534 : age)};
    fresh_ = false; return result;
  }
  bool isConnected() const { return connected_; }
 private:
  Reader* reader_;
  float value_;
  uint32_t lastAttempt_, measuredAt_;
  bool valid_, fresh_, connected_;
};

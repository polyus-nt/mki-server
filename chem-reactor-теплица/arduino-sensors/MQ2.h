#pragma once
#include <stdint.h>
// Один аналоговый MQ-2. Не превращаем напряжение в ppm без калибровки.
class MQ2 {
 public:
  class Reader {
   public:
    virtual ~Reader() {}
    virtual bool read(uint16_t& adc) = 0;
  };
  struct Reading { uint16_t adc, millivolts, ageMs; bool valid, fresh; };
  MQ2() : reader_(0), adc_(0), referenceMv_(5000), measuredAt_(0),
          attemptedAt_(0), valid_(false), fresh_(false) {}
  void attach(Reader& reader, uint16_t referenceMv, uint32_t now) {
    reader_ = &reader; referenceMv_ = referenceMv; valid_ = fresh_ = false;
    measuredAt_ = now; attemptedAt_ = now - 1000UL;
  }
  bool poll(uint32_t now) {
    if (!reader_ || static_cast<uint32_t>(now - attemptedAt_) < 1000UL) return false;
    attemptedAt_ = now; uint16_t adc;
    if (!reader_->read(adc) || adc > 1023) { fresh_ = false; return false; }
    adc_ = adc; measuredAt_ = now; valid_ = fresh_ = true; return true;
  }
  Reading read(uint32_t now) {
    const uint32_t age = now - measuredAt_;
    Reading result = {adc_, static_cast<uint16_t>((static_cast<uint32_t>(adc_) * referenceMv_ + 511) / 1023),
                      static_cast<uint16_t>(age > 65534 ? 65534 : age), valid_, valid_ && fresh_};
    fresh_ = false; return result;
  }
 private:
  Reader* reader_; uint16_t adc_, referenceMv_;
  uint32_t measuredAt_, attemptedAt_; bool valid_, fresh_;
};

#pragma once
#include "../common/HardwareProtocol.h"

namespace tjc_hub {
// Химреактор: одна actuator Arduino, индексы 0/1 без перевода в node17.
// Включая режим неисправности: запрос идёт ровно на один конечный узел.
class Hub {
 public:
  enum State { Recovering, Idle, Forwarding, Waiting, Responding };
  static const uint32_t kTransactionMs = 3000, kQuietMs = 250, kRecoveryMaxMs = 1000;
  explicit Hub(uint32_t now = 0)
      : state_(Recovering), started_(now), quiet_(now), recovery_(now),
        request_pending_(false), response_pending_(false), busy_pending_(false),
        recover_after_(false), cancel_pending_(true) {}
  void tick(uint32_t now) {
    parser_.tick(now);
    if ((state_ == Forwarding || state_ == Waiting) &&
        static_cast<uint32_t>(now - started_) >= kTransactionMs)
      fail(hardware_api::DEVICE_TIMEOUT);
    if (state_ == Recovering && (static_cast<uint32_t>(now - quiet_) >= kQuietMs ||
                                static_cast<uint32_t>(now - recovery_) >= kRecoveryMaxMs)) {
      state_ = Idle; parser_.reset();
    }
  }
  void receiveBus(uint8_t byte, uint32_t now) {
    using namespace hardware_api;
    tick(now);
    if (state_ == Recovering) { quiet_ = now; return; }
    Frame frame;
    if (!parser_.feed(byte, now, frame)) return;
    if (frame.kind == kResponse) {
      if (state_ != Waiting || !sameRequest(request_, frame)) return;
      if (frame.status > TRANSPORT_OVERFLOW || frame.status == REQUEST_ID_CONFLICT ||
          (frame.status != OK && !zeros(frame.data, 8))) return;
      answer(frame, false); return;
    }
    if (state_ != Idle) {
      if (!busy_pending_) { busy_ = response(frame, BUSY); busy_pending_ = true; }
      return;
    }
    request_ = frame;
    const Status status = validateRequest(frame, frame.node);
    if (status != OK) { answer(response(frame, status), false); return; }
    started_ = now; request_pending_ = true; state_ = Forwarding;
  }
  bool takeGatewayRequest(hardware_api::Frame& frame) {
    if (state_ != Forwarding || !request_pending_) return false;
    frame = request_; request_pending_ = false; return true;
  }
  void gatewayRequestSent(uint32_t now) {
    tick(now);
    if (state_ == Forwarding && !request_pending_) state_ = Waiting;
  }
  bool takeHostResponse(hardware_api::Frame& frame) {
    if (state_ != Responding || !response_pending_) return false;
    frame = response_; response_pending_ = false; return true;
  }
  void hostResponseSent(uint32_t now) {
    if (state_ != Responding || response_pending_) return;
    if (busy_pending_) {
      response_ = busy_; busy_pending_ = false; response_pending_ = true; return;
    }
    parser_.reset();
    if (recover_after_) beginRecovery(now); else state_ = Idle;
  }
  void overflow(uint32_t now) {
    parser_.reset(); cancel_pending_ = true;
    if (state_ == Forwarding || state_ == Waiting) fail(hardware_api::TRANSPORT_OVERFLOW);
    else if (state_ == Responding) recover_after_ = true;
    else beginRecovery(now);
  }
  bool takeCancelTransmission() {
    const bool result = cancel_pending_; cancel_pending_ = false; return result;
  }
  State state() const { return state_; }
 private:
  void beginRecovery(uint32_t now) {
    state_ = Recovering; quiet_ = recovery_ = now; parser_.reset();
  }
  void answer(const hardware_api::Frame& frame, bool recover) {
    response_ = frame; response_pending_ = true; request_pending_ = false;
    recover_after_ = recover; state_ = Responding; parser_.reset();
  }
  void fail(uint8_t status) {
    cancel_pending_ = true; answer(hardware_api::response(request_, status), true);
  }
  State state_;
  uint32_t started_, quiet_, recovery_;
  bool request_pending_, response_pending_, busy_pending_, recover_after_, cancel_pending_;
  hardware_api::Frame request_, response_, busy_;
  hardware_api::StreamParser parser_;
};
}

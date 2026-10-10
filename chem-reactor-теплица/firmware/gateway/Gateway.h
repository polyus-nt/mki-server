#pragma once

#include "../common/HardwareProtocol.h"

namespace tjc_gateway {

// Transport-only state machine. No GPIO policy or hardware commands live here.
class Gateway {
 public:
  enum State { Recovering, Idle, Waiting, Responding };
  static const uint32_t kTransactionMs = 1500;
  static const uint32_t kQuietMs = 250;

  explicit Gateway(uint8_t node, uint32_t now = 0)
      : node_(node), state_(Recovering), started_(now), quiet_(now),
        request_pending_(false), response_pending_(false),
        deferred_busy_(false), recover_after_response_(false),
        cancel_pending_(true) {}

  void tick(uint32_t now) {
    bus_parser_.tick(now);
    arduino_parser_.tick(now);
    if (state_ == Waiting && elapsed(now, started_) >= kTransactionMs)
      fail(0x07, now);
    if (state_ == Recovering && elapsed(now, quiet_) >= kQuietMs) {
      state_ = Idle;
      bus_parser_.reset();
      arduino_parser_.reset();
    }
  }

  void receiveBus(uint8_t byte, uint32_t now) {
    tick(now);
    if (state_ == Recovering) {
      quiet_ = now;
      return;
    }
    hardware_api::Frame frame;
    if (!bus_parser_.feed(byte, now, frame)) return;
    if (frame.kind != hardware_api::kRequest) return;
    if (state_ != Idle) {
      // Bound memory. Do not put a BUSY frame on the half-duplex bus while
      // a response to the previously accepted request is still outstanding.
      if (!deferred_busy_) {
        busy_response_ = hardware_api::response(frame, 0x06);
        deferred_busy_ = true;
      }
      return;
    }
    request_ = frame;
    if (frame.version != hardware_api::kVersion) {
      answer(hardware_api::response(frame, 0x08), false, now);
    } else if (frame.status != 0) {
      answer(hardware_api::response(frame, 0x02), false, now);
    } else if (frame.node != node_) {
      answer(hardware_api::response(frame, 0x0B), false, now);
    } else {
      arduino_parser_.reset();
      request_pending_ = true;
      started_ = now;
      state_ = Waiting;
    }
  }

  void receiveArduino(uint8_t byte, uint32_t now) {
    tick(now);
    quiet_ = now;
    if (state_ != Waiting) {
      arduino_parser_.reset();
      return;
    }
    hardware_api::Frame frame;
    if (!arduino_parser_.feed(byte, now, frame)) return;
    if (frame.kind != hardware_api::kResponse ||
        frame.version != hardware_api::kVersion ||
        !hardware_api::sameRequest(request_, frame)) return;
    // Unknown/unused statuses and nonzero error payloads are not trustworthy
    // responses. Keep waiting for a valid response or the deadline.
    if (frame.status > 0x0C || frame.status == 0x0A) return;
    if (frame.status != 0)
      for (uint8_t i = 0; i < 8; ++i)
        if (frame.data[i] != 0) return;
    answer(frame, false, now);
  }

  bool takeArduinoRequest(hardware_api::Frame& frame) {
    if (state_ != Waiting || !request_pending_) return false;
    frame = request_;
    request_pending_ = false;
    return true;
  }

  // Exactly one outstanding response buffer; platform code owns byte pacing.
  bool takeHubResponse(hardware_api::Frame& frame) {
    if (state_ != Responding || !response_pending_) return false;
    frame = response_;
    response_pending_ = false;
    return true;
  }

  void hubResponseSent(uint32_t now) {
    if (state_ != Responding || response_pending_) return;
    if (deferred_busy_) {
      response_ = busy_response_;
      deferred_busy_ = false;
      response_pending_ = true;
      return;
    }
    state_ = recover_after_response_ ? Recovering : Idle;
    quiet_ = now;
    bus_parser_.reset();
    arduino_parser_.reset();
  }

  void overflow(uint32_t now) {
    bus_parser_.reset();
    arduino_parser_.reset();
    cancel_pending_ = true;
    if (state_ == Waiting) fail(0x0C, now);
    else if (state_ == Idle || state_ == Recovering) {
      state_ = Recovering;
      quiet_ = now;
    } else {
      recover_after_response_ = true;
    }
  }

  bool takeCancelArduino() {
    const bool pending = cancel_pending_;
    cancel_pending_ = false;
    return pending;
  }

  State state() const { return state_; }

 private:
  static uint32_t elapsed(uint32_t now, uint32_t before) {
    return static_cast<uint32_t>(now - before);
  }

  void answer(const hardware_api::Frame& frame, bool recover, uint32_t now) {
    response_ = frame;
    response_pending_ = true;
    request_pending_ = false;
    recover_after_response_ = recover;
    state_ = Responding;
    quiet_ = now;
    arduino_parser_.reset();
  }

  void fail(uint8_t status, uint32_t now) {
    cancel_pending_ = true;
    answer(hardware_api::response(request_, status), true, now);
  }

  uint8_t node_;
  State state_;
  uint32_t started_, quiet_;
  bool request_pending_, response_pending_, deferred_busy_;
  bool recover_after_response_, cancel_pending_;
  hardware_api::Frame request_, response_, busy_response_;
  hardware_api::StreamParser bus_parser_, arduino_parser_;
};

}  // namespace tjc_gateway

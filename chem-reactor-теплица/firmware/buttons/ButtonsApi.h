#pragma once
#include "BoardConfig.h"

namespace tjc_buttons {
// Отдельный низкоуровневый endpoint: только снимок двух концевиков.
// Никаких Arduino, правил управления дверями или выходов замков здесь нет.
class ButtonsApi {
 public:
  explicit ButtonsApi(uint32_t now = 0) : started_(now), ready_(false) {
    states_[0] = states_[1] = false;
  }
  // Состояния передаются из постоянно сканируемых Button::isPressed().
  void sample(bool first, bool second, uint32_t now) {
    states_[0] = first; states_[1] = second;
    ready_ = static_cast<uint32_t>(now - started_) > kDebounceMs;
  }
  hardware_api::Frame execute(const hardware_api::Frame& request) const {
    const hardware_api::Status status =
        hardware_api::validateRequest(request, kBusAddress);
    hardware_api::Frame reply = hardware_api::response(request, status);
    if (status != hardware_api::OK) return reply;
    if (!ready_) {
      reply.status = hardware_api::SENSOR_UNAVAILABLE;
      return reply;
    }
    // Ответ get_door_state: один bool (0/1), затем семь нулевых байтов.
    reply.data[0] = (states_[request.idx] == kClosedWhenPressed[request.idx]) ? 1 : 0;
    return reply;
  }
 private:
  uint32_t started_;
  bool ready_, states_[2];
};
}

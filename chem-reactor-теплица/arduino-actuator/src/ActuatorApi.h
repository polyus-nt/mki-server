#pragma once
#include "common/HardwareProtocol.h"

namespace hardware_api {
class FanHardware {
 public:
  virtual ~FanHardware() {}
  virtual bool available(uint8_t idx) const = 0;
  virtual bool writePwm(uint8_t idx, uint8_t pwm) = 0;
  virtual uint8_t readPwm(uint8_t idx) const = 0;
  virtual uint32_t readRpm(uint8_t idx) const = 0;
  virtual uint8_t randomPwm() = 0;
};

// Химреактор: низкоуровневый API одной Arduino, node14 — idx=0..1.
// Внешний и локальный индексы совпадают. Без PID, EEPROM,
// температурных решений, замков и watchdog связи. Потеря связи не меняет PWM.
// User/Admin — права интерфейса на компьютере: прошивка не аутентифицирует
// отправителя кадра. get_mode/set_mode НЕ должны экспортироваться в MCP.
class ActuatorApi {
 public:
  explicit ActuatorApi(FanHardware& hardware, uint8_t node = kActuatorNode,
                       uint8_t fanCount = kFanCount)
      : hardware_(hardware), node_(node), fanCount_(fanCount),
        mode_(0), idx_(0xff), lastTickMs_(0) {}
  bool begin(uint32_t now) {
    // Начальное состояние после загрузки: mode=0, нет выбранного вентилятора,
    // на доступных выходах PWM=0. Это не восстановление значений из EEPROM.
    mode_ = 0; idx_ = 0xff; lastTickMs_ = now;
    bool success = true;
    for (uint8_t i = 0; i < fanCount_; ++i)
      if (hardware_.available(i) && !hardware_.writePwm(i, 0)) success = false;
    return success;
  }
  bool tick(uint32_t now) {
    // Периодическая часть mode=1: новое физическое значение PWM каждые 30 с.
    // Вызывать из loop() независимо от наличия команд или связи с компьютером.
    // Беззнаковая разность времени корректно переживает переполнение millis().
    const uint32_t elapsed = static_cast<uint32_t>(now - lastTickMs_);
    if (mode_ != 1 || elapsed < 30000UL) return true;
    // Пропущенные интервалы не создают пачку записей: применяем один новый PWM.
    lastTickMs_ += (elapsed / 30000UL) * 30000UL;
    return hardware_.writePwm(idx_, hardware_.randomPwm());
  }
  Frame handle(const Frame& request, uint32_t now) {
    // Проверка типа кадра, адреса, команды, индекса и неиспользуемых байтов
    // выполняется ДО доступа к оборудованию. Некорректный запрос не меняет выходы.
    // Ответ сохраняет tx_id/node/command/idx запроса. Данные успешного ответа
    // нельзя интерпретировать при status != OK; CRC добавит кодек отправки.
    const Status checked = validateRequest(request, node_, fanCount_);
    Frame reply = response(request, checked);
    if (checked != OK) return reply;
    // GET_MODE (0x40, Admin): idx=0xFF, все байты data запроса нулевые.
    // Ответ: data[0]=mode (0/1/2), data[1]=idx выбранного вентилятора.
    // При mode=0 idx=0xFF; на уровне API компьютера это {mode:0, idx:null}.
    // Команда не меняет режим или PWM и не требует доступности GPIO каналов.
    if (request.command == kGetMode) { modeResult(reply); return reply; }
    if (request.command == kSetMode) {
      // SET_MODE (0x41, Admin): локальный idx, data[0]=mode, остальные data=0.
      // mode=0: выключить единственный сценарий; допустимый idx игнорируется.
      //         Последний реально применённый PWM остаётся на каждом выходе.
      // mode=1: сразу применить случайный PWM выбранному вентилятору, затем
      //         повторять каждые 30 с через tick(), даже при потере связи.
      // mode=2: не менять выход при включении; подменять только новые SET_PWM
      //         выбранному вентилятору случайными значениями в диапазоне 0..255.
      // Ответ имеет тот же формат mode/idx, что и GET_MODE.
      // Новый сценарий заменяет старый НА ЭТОЙ ПЛАТЕ. Единственный сценарий
      // во всей системе обеспечивается верхним API: при переносе между платами
      // сначала подтверждается mode=0 старой платы, затем включается новая.
      // Повтор того же mode/idx ничего не перезапускает и не рандомит заново.
      const uint8_t mode = request.data[0];
      if (mode == 0) { mode_ = 0; idx_ = 0xff; modeResult(reply); return reply; }
      if (!hardware_.available(request.idx)) return response(request, NOT_IMPLEMENTED);
      if (mode_ == mode && idx_ == request.idx) { modeResult(reply); return reply; }
      // Сначала выключаем предыдущий сценарий, не восстанавливая старый PWM.
      // Если начальная запись mode=1 не удалась, остаёмся в mode=0 и сообщаем
      // INTERNAL_ERROR, а не подтверждаем включение сценария.
      mode_ = 0; idx_ = 0xff;
      if (mode == 1 && !hardware_.writePwm(request.idx, hardware_.randomPwm()))
        return response(request, INTERNAL_ERROR);
      mode_ = mode; idx_ = request.idx; lastTickMs_ = now;
      modeResult(reply); return reply;
    }
    // Отсутствующая/неутверждённая карта канала: NOT_IMPLEMENTED, не ложный 0.
    if (!hardware_.available(request.idx)) return response(request, NOT_IMPLEMENTED);
    switch (request.command) {
      // GET_PWM (0x30, User): локальный idx, data запроса=0.
      // Ответ: data[0]=фактически применённый PWM (0..255), остальные data=0.
      // Это НЕ последнее запрошенное значение: запрос 100 в mode=2 мог дать 230.
      case kGetPwm: reply.data[0] = hardware_.readPwm(request.idx); break;
      // GET_RPM (0x31, User): локальный idx, data запроса=0.
      // Ответ: data[0..3]=измеренные обороты/минуту, unsigned u32 big-endian.
      // Без нормализации и подмены отдельных байтов; оставшиеся data=0.
      case kGetRpm: writeU32(reply.data, hardware_.readRpm(request.idx)); break;
      case kSetPwm: {
        // SET_PWM (0x32, User): локальный idx, data[0]=PWM 0..255, остальные data=0.
        // PWM=0: реле HIGH (питание выключено), analogWrite(...,0).
        // PWM>0: реле LOW (питание включено), analogWrite(...,PWM).
        // В mode=0/1 и для незатронутых каналов записываем переданное значение.
        // Только mode=2 для активного idx заменяет его случайным физическим PWM.
        // Ошибка записи -> INTERNAL_ERROR. Успех -> data[0]=применённый PWM.
        // После записи значение удерживается до следующей команды/тика mode=1;
        // SET_PWM не сдвигает 30-секундный таймер и не выключает сценарий.
        const uint8_t pwm = mode_ == 2 && idx_ == request.idx
            ? hardware_.randomPwm() : request.data[0];
        if (!hardware_.writePwm(request.idx, pwm)) return response(request, INTERNAL_ERROR);
        reply.data[0] = hardware_.readPwm(request.idx);
        break;
      }
      default: return response(request, UNKNOWN_COMMAND);
    }
    return reply;
  }
  uint8_t mode() const { return mode_; }
  uint8_t activeIdx() const { return idx_; }
 private:
  void modeResult(Frame& reply) const { reply.data[0] = mode_; reply.data[1] = idx_; }
  FanHardware& hardware_;
  uint8_t node_, fanCount_;
  uint8_t mode_, idx_;
  uint32_t lastTickMs_;
};
}  // namespace hardware_api

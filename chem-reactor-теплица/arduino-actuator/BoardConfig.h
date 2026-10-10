#pragma once
#include <Arduino.h>
// Химреактор: самостоятельная UNO, два вентилятора с индексами 0/1.
// Вся распиновка только здесь. Реле active LOW; тахометр — два импульса/оборот.
#define API_LOCAL_NODE 14
#define API_FAN_COUNT 2
#define API_GLOBAL_FAN_OFFSET 0
#define API_LINK_MISO 2
#define API_LINK_MOSI 3
#define API_LINK_CLK 4
#define API_LINK_CS 5
#define API_LINK_IRQ 7
#define API_PWM_PINS {10, 11}
#define API_TACH_PINS {8, 12}
#define API_RELAY_PINS {A0, A1}
#define API_PULSES_PER_REV 2
static_assert(API_PULSES_PER_REV > 0, "tach pulses/revolution must be positive");
namespace actuator_config {
constexpr uint8_t pwmPins[] = API_PWM_PINS;
constexpr uint8_t tachPins[] = API_TACH_PINS;
constexpr uint8_t relayPins[] = API_RELAY_PINS;
static_assert(sizeof(pwmPins) == API_FAN_COUNT, "wrong PWM pin count");
static_assert(sizeof(tachPins) == API_FAN_COUNT, "wrong tach pin count");
static_assert(sizeof(relayPins) == API_FAN_COUNT, "wrong relay pin count");
}

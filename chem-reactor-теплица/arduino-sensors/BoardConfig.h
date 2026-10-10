#pragma once
#include <Arduino.h>
// Включить для текстовой диагностики Serial. Austere продолжит работать.
// #define TEST
#define API_LINK_MISO 9
#define API_LINK_MOSI 10
#define API_LINK_CLK 11
#define API_LINK_CS 12
#define API_LINK_IRQ 13
#define API_BMP_SCL_PIN 4
#define API_BMP_SDA0_PIN 5
#define API_BMP_SDA1_PIN 6
#define API_DHT0_PIN 2
#define API_DHT1_PIN 3
#define API_GAS0_PIN A1
#define API_GAS1_PIN A0
// Без калибровки выдаём только ADC и напряжение при принятой опоре 5 В.
#define API_ADC_REFERENCE_MV 5000
static_assert(API_ADC_REFERENCE_MV > 0 && API_ADC_REFERENCE_MV <= 65535,
              "ADC reference must be 1..65535 mV");

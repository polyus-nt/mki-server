# Прошивки МС-ТЮК: переход на исходный код

Статус: рукописные Arduino-шлюзы и центральный хаб реализованы на feature-ветке tjc-sketch и собраны для ARM. Стенд не прошит; восьмилампочница остаётся отдельной задачей.

## Решение

В `tjc-sketch` исходниками прошивок становятся рукописные C++-файлы центральной платы и двух Arduino-шлюзов. GraphML и его генератор относятся к прежнему пути разработки; новая логика не должна требовать диаграммы или её генерации.

Причина перехода — ускорение разработки и изменения транспортного кода. Хаб и шлюзы по-прежнему выполняют маршрутизацию и транспорт, а не высокоуровневое управление аппаратурой.

Архитектурное решение: [ADR-0002](adr/ADR-0002-tjc-firmware-source-code.md).

## Источники для сборки

Изучен [kruzhok-team/lapki-compiler](https://github.com/kruzhok-team/lapki-compiler) на ревизии `c0e235f364f21b1a7ba8dbb598ef0bc983511af6`. Зафиксированная ревизия позволяет повторно проверить сведения, даже если master изменится.

| Назначение | Конфигурация платформы |
| --- | --- |
| Центральная плата | [tjc-ms1-main-a4/1.0](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/platforms/tjc-ms1-main-a4/1.0/tjc-ms1-main-a4-1.0.json) |
| Оба Arduino-шлюза | [tjc-ms1-ard-a1/1.0](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/platforms/tjc-ms1-ard-a1/1.0/tjc-ms1-ard-a1-1.0.json) |

Обе конфигурации указывают `language=cpp`, основной файл `sketch.cpp`, заголовки `.hpp` и две стадии сборки:

1. `arm-none-eabi-g++` компилирует/линкует `sketch.elf`.
2. `arm-none-eabi-objcopy -Obinary` создаёт `sketch.bin`.

Для сборки существенны `-mcpu=cortex-m0plus`, `-mthumb`, `-DSTM32G030x6`, `--specs=nosys.specs`, `-Os`, `-fno-exceptions`, `-ffunction-sections`, `-fdata-sections` и сборка мусора секций линкером. Полный порядок аргументов следует брать из `compilingSettings` выбранной платформы; будущий скрипт сборки должен явно отражать необходимые отличия.

Платформы также задают `defaultIncludeFiles`, `defaultBuildFiles` и `defaultSetupFunctions`. Их нужно учитывать при переходе на собственную точку входа, а не переносить только вызов компилятора.

## Аппаратный runtime

Файлы берутся из соответствующего `compiler/library/<platform>/1.0/source/`:

- `stm32g030xx.h`, CMSIS-заголовки и `Pins.hpp` — аппаратные определения.
- `system.c` — SystemInit, настройка системы, millis и SysTick; несмотря на расширение, файл содержит C++ и в существующем пути включается в C++-исходник.
- `startup_stm32g030xx.s` — запуск контроллера.
- `tjc-ms1-b4272ba421624bbe-usercode.ld` — размещение пользовательской прошивки.
- `UART.hpp`, `SimpleBus.hpp` — обмен по SimpleBus.
- На шлюзах дополнительно `ArduinoAdapter.hpp` и включаемый им `arduino_link.c` — Arduino-связь.

Эти роли установлены чтением [библиотеки центральной платы](https://github.com/kruzhok-team/lapki-compiler/tree/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-main-a4/1.0/source) и [библиотеки шлюза](https://github.com/kruzhok-team/lapki-compiler/tree/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-ard-a1/1.0/source).

Linker script размещает USERCODE с адреса `0x08003800`, а SystemInit использует этот адрес для таблицы векторов. При переносе нужно сохранить согласованность startup, linker script и загрузчика. Это установлено по [linker script](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-main-a4/1.0/source/tjc-ms1-b4272ba421624bbe-usercode.ld) и [system.c](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-main-a4/1.0/source/system.c).

Конфигурация сборки включает `qhsm.cpp`, относящийся к существующему пути генерации автоматов. Рукописный код не обязан использовать QHSM; исключение этой зависимости нужно подтвердить сборкой после выбора структуры прошивки. GraphML и QHSM — разные зависимости: отказ от диаграммы сам по себе не определяет необходимость библиотеки runtime.

## Доступные аппаратные интерфейсы

По исходникам, без предположения о графическом редакторе:

| Компонент | Операции |
| --- | --- |
| SimpleBus | setAddress(addr), sendPacket(addr, byte), packetReceived(), lastData |
| ArduinoAdapter | init(), sendPacket(byte), packetReceived(), lastData, isQueueOverflow() |

Источники: [SimpleBus.hpp](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-main-a4/1.0/source/SimpleBus.hpp), [ArduinoAdapter.hpp](https://github.com/kruzhok-team/lapki-compiler/blob/c0e235f364f21b1a7ba8dbb598ef0bc983511af6/compiler/library/tjc-ms1-ard-a1/1.0/source/ArduinoAdapter.hpp).

Интерфейсы передают отдельные байты; поверх них реализован [28-байтовый Hardware API v2](HARDWARE-PROTOCOL.md). Хаб использует одну USART1/SimpleBus-линию для компьютера и шлюзов, маршруты 12/13/14/99, один активный обмен и ограниченное восстановление.

В заголовках upstream есть определения функций, глобальных переменных и обработчиков прерываний. При разделении нашей прошивки на несколько единиц трансляции нужно проверить повторные определения; простое включение всех этих заголовков в каждый `.cpp` не является готовой схемой сборки.

## Реализованный путь сборки и следующие шаги

В tjc-sketch: `firmware/hub/sketch.cpp`, `firmware/hub/Hub.h` и
`firmware/gateway/` — актуальный C++ без генерации диаграмм.
Воспроизводимые скрипты: `scripts/build_hub.py` и
`scripts/build_gateways.py`, с проверкой pinned upstream и локальными runtime
поправками. Пользовательский `Скетч.graphml` не изменяется.

```sh
python3 scripts/build_hub.py --compiler-root /path/to/lapki-compiler
python3 scripts/build_gateways.py --compiler-root /path/to/lapki-compiler
```

Для toolchain вне PATH есть `--toolchain-bin /path/to/bin`.
ARM ELF/BIN/map построены для всех трёх узлов. Локальные host-тесты проверяют
Hub/Gateway, межрепозиторный тест — Hub → Gateway → Arduino.
Это не проверка контактов, полудуплекса, загрузчика или датчиков на стенде.
Описание ограничений:
[Hub](../../tjc-sketch/docs/HUB-FIRMWARE.md),
[шлюзы](../../tjc-sketch/docs/GATEWAY-FIRMWARE.md),
[общий статус](FIRMWARE-IMPLEMENTATION.md).

Осталось согласовать разводку и электрическую линию, проверить сквозной обмен
на стенде и реализовать восьмилампочницу без назначения неподтверждённых адресов.

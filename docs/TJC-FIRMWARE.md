# Прошивки МС-ТЮК: переход на исходный код

Статус: способ разработки принят; прошивки нового аппаратного API ещё не реализованы.

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

Текущие интерфейсы передают отдельные байты. Форматы нового аппаратного API, размеры ответов, тайм-ауты и маршрутизация должны быть определены отдельно от выбора языка и компилятора.

В заголовках upstream есть определения функций, глобальных переменных и обработчиков прерываний. При разделении нашей прошивки на несколько единиц трансляции нужно проверить повторные определения; простое включение всех этих заголовков в каждый `.cpp` не является готовой схемой сборки.

## Следующие шаги реализации

1. Зафиксировать запросы/ответы нового API и адресацию трёх TJC-плат.
2. Подготовить C++-точки входа для хаба и шлюзов в tjc-sketch, с общей транспортной частью там, где это уместно.
3. Подключить runtime выбранных платформ и воспроизводимую сборку по их настройкам.
4. Собрать ELF/BIN для каждого аппаратного узла; проверить память, адреса и обработчики прерываний.
5. Проверить транспорт на стенде вместе с Arduino и ПК.

Пока выполнено изучение upstream и фиксация пути перехода. Сборка МС-ТЮК, переписывание транспортных прошивок и прошивка оборудования в этой работе не выполнялись. Существующая пользовательская правка `Скетч.graphml` сохранена.

# Теплица-химреактор: FastAPI и HTTP-консоль

**Для передачи пользователям одним файлом:** [полное API теплицы и реактора](API-COMPLETE.md).
Содержит все HTTP-методы, аргументы, ответы, единицы, ошибки и примеры без
необходимости открывать остальные документы репозитория.

## Mega химического реактора

Встроена через отдельный Arduino-шлюз **17**: три шаговых двигателя (SPS/RPM,
stop, удержание, микрошаги, состояние), три STM32 (SET/GET U/I и уставки),
общий DS18B20. [Полное API и команды консоли](REACTOR-API.md).
Новые методы: `/api/v1/reactor/...`. Rust-приложение не менялось.
Ветка `feat/chemical-reactor-integration` в arduino-actuator, tjc-sketch и tjc-commutator.

> Общая шина теперь **115200, 8N1**, с READY → CLAIM → GRANT, без секундных пауз.
> Обновить все платы МС-ТЮК и сервис вместе. Arduino TEST Serial остаётся 9600.

Здесь только Python-сервис, HTTP-консоль, тесты и пользовательская документация.
Другие части химреактора находятся **в своих репозиториях**:

| Репозиторий | Папка / файл |
| --- | --- |
| `tjc-commutator` | `chem-reactor-теплица/` — этот сервис, API, HTTP-консоль |
| `arduino-sensor` | `chem-reactor-теплица/arduino-sensors/arduino-sensors.ino` |
| `arduino-actuator` | `chem-reactor-теплица/arduino-actuator/arduino-actuator.ino` |
| `tjc-sketch` | `chem-reactor-теплица/` — МС-ТЮК-прошивки, билд, PINOUT, протокол |

Для запуска сервиса и обычных тестов другие репозитории не нужны.
Фаблаб остаётся в корне и использует порт 8000; химреактор — 8001.
Стенды имеют разные физические шины и COM-порты: адреса узлов пересекаются.
Автоматического определения стенда нет — выберите правильные прошивки.

## Запуск на Windows

Сборка в один `.exe` с HTML-панелью и внешним `config.json`:
[WINDOWS-EXE.md](WINDOWS-EXE.md). Команда из этой папки:
`py -3 scripts/build_windows.py`. Python нужен только на компьютере сборщика.

Из корня `tjc-commutator`, Python 3.10+:

```powershell
cd "chem-reactor-теплица"
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m hub_service --serial-port COM15
```

Укажите свой USB-порт МС-ТЮК, не Arduino. Освободите порт от других программ.
Один экземпляр, один worker. Swagger: **http://127.0.0.1:8001/docs**.
Панель ручного управления: **http://127.0.0.1:8001/panel**.
Она работает через этот же API, без отдельного подключения к COM-порту.
На странице есть подсказки, чтение датчиков, управление двигателями и STM32,
замками и вентиляторами. Для запуска и изменения уставок сначала отметьте
«Разрешить управляющие команды»; stop доступен и без этой отметки.
При открытии страницы оборудование автоматически не включается.
Общий selftest проверяет только чтения, включая реактор: без Mega будут ошибки
её проверок. Для отдельной теплицы используйте чтение в разделе «Теплица».
Stop проходит общую очередь и не заменяет аппаратное аварийное отключение.
Полное руководство: [API.md](API.md). Настройки: --http-port, --host, --debug,
--queue-capacity, --queue-timeout, --exchange-timeout; MSTJUK_SERIAL_PORT вместо
--serial-port. Linux/macOS используют `.venv/bin/python`.
Токенов нет: при сетевом запуске любой доступный клиент управляет аппаратурой,
поэтому ограничьте доступ firewall. По умолчанию только localhost.

## Консоль

В другом терминале из той же папки: `py -3 scripts/api_console.py`.

```text
health
probe
temp 0
pressure 1
humidity 0
gas 1
door 0
pwm 1
rpm 1
lock
relay
selftest
```

Управляющие команды действительно переключают оборудование:

```text
set_pwm 1 100
set_relay_level 7
set_relay_level 0
set_door_lock 1
set_door_lock 0
```

Замок без индекса: set_door_lock 1 блокирует оба. Selftest — 38 GET + OpenAPI,
без переключения выходов. Разовый запуск: `--command selftest`.
Адрес меняется через --url, HTTP-таймаут по умолчанию 45 с.

## Прошивки

Сборка МС-ТЮК описана в `tjc-sketch/chem-reactor-теплица/README.md`:
`py -3 scripts/build.py --target all` выполняется **там**, не в этой папке.
Полная карта: `tjc-sketch/chem-reactor-теплица/PINOUT.md`,
аппаратный протокол: `tjc-sketch/chem-reactor-теплица/PROTOCOL.md`.
Arduino-скетчи из таблицы открыть в IDE как Arduino Uno; actuator требует
PinChangeInterrupt 1.2.9, библиотеки sensors включены локально.
Полярность выходов и силовую коммутацию нужно проверить на физическом стенде.
MQ-2 выдаёт ADC/напряжение, не некалиброванные ppm.

## Проверки

Обычные тесты сервиса не требуют C++ или других репозиториев:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Для необязательной проверки с production C++-классами соберите bridge по
инструкции tjc-sketch, затем выполните те же тесты:

```powershell
$env:CHEM_FIRMWARE_CHAIN = "C:\projects\tjc-sketch\chem-reactor-теплица\build\tests\firmware-chain.exe"
.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:CHEM_FIRMWARE_CHAIN
```

Без переменной используется самостоятельная wire-модель. С переменной —
реальные C++ API хаба/шлюза/конечных плат, с подменёнными GPIO.
PTY-проверка HTTP → pySerial → SimpleBus выполняется только на POSIX.

## Проверка по частям и полного стенда

[Пошаговый гайд диагностики](../docs/TESTING.md) — Arduino USB TEST,
отдельные аппаратные ветви, хаб, FastAPI, безопасные ручные проверки и автотесты.

Режимов отказа вентиляторов здесь нет: PWM применяется без случайной подмены.
[Оконные графики датчиков на Rust](sensor-plots/README.md):
`cargo run --release --locked` из sensor-plots/, после запуска FastAPI.
В окне есть панель ручных команд: `set_pwm 0 128`, `set_door_lock 1`,
`set_relay_level 4`. Команды реально переключают оборудование;
`set_door_lock` управляет общим выходом двух замков без индекса.

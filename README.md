# TJC Commutator — аппаратный API МС-ТЮК

Rust-приложение использует Hardware API v1: фиксированные CRC-кадры через
центральный хаб и Arduino-шлюзы. Старые PID/config-команды и 29-байтовая
телеметрия больше не отправляются. Работа требует совместимых feature-прошивок;
физический стенд пока не проверен.

- [Архитектура](docs/HARDWARE-API-ARCHITECTURE.md)
- [Человекочитаемый протокол](docs/HARDWARE-PROTOCOL.md)
- [Статус прошивок и ограничения](docs/FIRMWARE-IMPLEMENTATION.md)
- [JSON и golden-векторы](docs/protocol/hardware-v1.json)

## Работа

Один отдельный worker владеет COM-портом. Общая ограниченная FIFO обслуживает
GUI, MCP и фоновый опрос. Одновременно выполняется один запрос; ответ приходит
после оборудования или ошибки. Управляющие команды автоматически не повторяются.

GUI показывает три BMP280 (температуру/давление), два SGP40 (VOC Index),
три DHT11 (влажность), две двери, PWM/RPM шести вентиляторов и графики
температур/RPM. Сохранённые значения явно показывают возраст кэша и качество
датчика; физическое расположение каналов inside/outside не выдумывается.
В админке доступны PWM и режимы неисправностей. Пыль, замки и пылесос ещё
не подключены к серверным tools.

MCP: `http://127.0.0.1:8765/mcp`, bearer token из `TJC_MCP_TOKEN` либо
сгенерированный при старте (показывается по запросу в GUI).
Tools: get_temperature, get_voc, get_door_state, get_pressure, get_humidity,
get_fan_pwm, get_fan_rpm, set_fan_pwm; дополнительно кэш, история, события и
уведомления оператору. Ни get_mode/set_mode, ни старых PID tools нет.
MCP получает только UserApi. По умолчанию он только читает;
`TJC_AGENT_PHASE=full` разрешает PWM с обязательным decision_id.

## Сборка и проверки

Нужны Rust edition 2024, C/C++ linker, pkg-config/libudev на Linux
и графическое окружение для запуска GUI.

```sh
cargo run --bin tjc-commutator-v0-1-1
cargo fmt --check
cargo test
cargo check
python3 scripts/validate_hardware_protocol.py
python3 scripts/test_firmware.py --sanitize
```

В Nix-окружении без dev-shell (команда не прошивает платы):

```sh
tjc_udev_dev=$(nix --extra-experimental-features 'nix-command flakes' eval --raw nixpkgs#systemd.dev.outPath)
nix --extra-experimental-features 'nix-command flakes' shell nixpkgs#cargo nixpkgs#rustc nixpkgs#rustfmt nixpkgs#gcc nixpkgs#pkg-config nixpkgs#systemd.dev --command env PKG_CONFIG_PATH="$tjc_udev_dev/lib/pkgconfig" cargo test
```

SQLite сохраняет telemetry/events/tool_calls и старые таблицы/записи без
удаления. История новых температур адресуется idx=0..2 и не подменяет старые
inside/outside данными неизвестного размещения.

## Ограничения перед запуском стенда

Согласовать распиновку, полудуплекс общей линии и совместимые прошивки.
Порт 9600 8N1; хаб 12, компьютер 99, шлюзы 13/14.
История в памяти ограничена 1000 точками на канал; FIFO — 64 запроса,
результаты решений — 4096, одно решение — не более 64 ожидателей.
Очередь ждёт до 30 с, физический обмен до 5 с.

Без session/epoch tx_id в одном процессе не переиспользуется: после 65535
принятых запросов опрос останавливается. Фоновая телеметрия тоже расходует IDs.
Дедупликация decision_id защищает только текущий процесс, не перезапуск
устройства/сервера. После тайм-аута PWM результат может быть неизвестен —
нужно проверить фактический PWM, а не автоматически отправлять команду снова.

Python-файлы в корне — прежняя диагностика; они несовместимы с новым API,
не являются вторым production GUI и не должны отправлять команды на новый стенд.

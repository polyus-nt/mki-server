# МС-ТЮК Hub API — FastAPI

Самостоятельный Python-сервис для **фаблаб-теплицы**: HTTP → FIFO → COM-порт →
центральный хаб МС-ТЮК → аппаратный узел. Ветка `feat/fastapi-hub-service`
содержит только сервис, тесты и документацию. Предыдущий Rust-проект и прошивки
сохранены в истории и ветке `feat/hardware-api-firmware`, но не нужны для запуска сервиса.

Сейчас HTTP API **только читает**. Методов установки PWM, блокировки замков,
включения пылесоса, изменения fault-mode и сырых пакетов нет.
Управляющие команды замков/пылесоса реализованы в прошивке lmp8, но не экспортированы
этим сервисом. Релейный вентилятор относится к химреактору и отключён.

## Запуск на Windows

Python 3.10+; в каталоге репозитория:

```powershell
git fetch origin
git switch feat/fastapi-hub-service
git pull
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m hub_service --serial-port COM15
```

Откройте Swagger: **http://127.0.0.1:8000/docs**.
COM15 — рабочий порт текущего стенда; при другом назначении укажите свой.
Закройте sensor_console, hub_test_console, commutator и другие программы,
владеющие этим портом. Нужны **рабочие** прошивки API v2, не тестовый byte-response хаб.

Пример PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/hub/probe
Invoke-RestMethod http://127.0.0.1:8000/api/v1/pressure/0
Invoke-RestMethod http://127.0.0.1:8000/api/v1/doors/0/state
Invoke-RestMethod http://127.0.0.1:8000/api/v1/locks/1/state
Invoke-RestMethod http://127.0.0.1:8000/api/v1/vacuum/state
```

`hub/probe` ожидает INVALID_INDEX от самого хаба: нижние узлы не участвуют.
Он подтверждает обмен с хабом, но не наличие датчиков или состояние исполнительных устройств.
`/health` проверяет состояние сервиса/порта, не запускает аппаратный опрос.

## Эндпоинты

Все аппаратные операции — GET, префикс `/api/v1`.

| Путь | Индекс | Результат |
| --- | --- | --- |
| `/temperature/{idx}` | 0..2 | температура BMP280, °C |
| `/pressure/{idx}` | 0..2 | давление BMP280, Па |
| `/voc/{idx}` | 0..1 | VOC Index SGP40, не ppm |
| `/humidity/{idx}` | 0..2 | влажность DHT11, % RH |
| `/dust/{idx}` | 0..1 | ADC и напряжение GP2Y1010AU0F, не концентрация |
| `/doors/{idx}/state` | 0..1 | `closed`, концевики кнопочницы |
| `/fans/{idx}/pwm` | 0..5 | применённый PWM 0..255 на Arduino |
| `/fans/{idx}/rpm` | 0..5 | измеренные RPM на Arduino |
| `/locks/{idx}/state` | 0..1 | состояние независимого выхода замка на lmp8 |
| `/vacuum/state` | без индекса | состояние выхода пылесоса на lmp8 |
| `/hub/probe` | без индекса | ответ самого хаба |
| `/capabilities` | без индекса | состав поддерживаемой фаблаб-конфигурации |

Замки теперь **на разных пинах**, поэтому имеют индексы 0/1. Пылесос один,
его публичный API индекса не содержит. `lock_output_enabled` — состояние выхода
управления, **не подтверждение физического срабатывания**. `closed` читается
отдельно с концевика. `enabled` пылесоса также не доказывает вращение двигателя.

Пример ответа давления (иллюстрация, не измерение стенда):

```json
{"idx":0,"value":100653,"unit":"Pa","valid":true,"fresh":true,"age_ms":123,"received_at":"2026-10-10T00:00:00Z"}
```

`age_ms` — возраст показания на плате, `fresh` — ещё не выданное новое измерение.
Повторный GET может вернуть прежнее значение с `fresh=false`. `received_at` —
время получения ответа компьютером. HTTP-слой не подставляет фиктивные измерения.

## Очередь и ошибки

Один worker сериализует физические обмены; все операции с портом, включая
открытие/закрытие, выполняет один выделенный поток. Async HTTP не блокируется
на UART: `/health` доступен и во время ожидания оборудования.
По умолчанию: 64 ожидающих запроса + один активный, ожидание в очереди 30 с,
аппаратный обмен 5 с. Ответ возвращается после оборудования или ошибки,
а не сразу с `queued`. Каждый GET делает отдельное чтение; фонового опроса,
БД и общего snapshot всех каналов нет.

Ошибки оборудования возвращаются JSON, например HTTP 504:

```json
{"error":{"code":"DEVICE_TIMEOUT","message":"Оборудование вернуло ошибку","device_status":7,"node":13,"command":"get_pressure","idx":0}}
```

- Неверный HTTP индекс/тип — 422, запрос не отправляется в порт.
- `SENSOR_UNAVAILABLE`, `BUSY`, `QUEUE_FULL` — 503.
- `DEVICE_TIMEOUT`, `HOST_TIMEOUT`, `QUEUE_TIMEOUT` — 504.
- `NOT_IMPLEMENTED`/`UNKNOWN_COMMAND` — 501; прочие аппаратные ошибки — 502.
- `DISCONNECTED` — 503; после обрыва порта сервис нужно перезапустить.

Просроченный/отменённый запрос в очереди не передаётся. Отмена HTTP-ожидания
не прерывает уже начатый физический обмен: следующий запрос ждёт его окончания.
Повторов нет. После host timeout/транспортных ошибок worker дренирует поздние
байты и ждёт секунду тишины (не дольше 3 с); при непрерывном шуме отключается.

## Настройка и безопасность

```powershell
.venv\Scripts\python.exe -m hub_service --serial-port COM15 --http-port 8000 --debug
```

Дополнительные параметры: `--queue-capacity`, `--queue-timeout`, `--exchange-timeout`.
`--debug` печатает TX API и все RX RAW до разбора.
Вместо `--serial-port` можно задать `MSTJUK_SERIAL_PORT`.

По умолчанию слушает только localhost. Для доступа по сети CLI требует Bearer token:

```powershell
$env:MSTJUK_API_TOKEN = "замените-на-длинный-случайный-токен"
.venv\Scripts\python.exe -m hub_service --serial-port COM15 --host 0.0.0.0
```

Для токена используйте ASCII: он передаётся в HTTP-заголовке.
Заголовок клиента: `Authorization: Bearer <token>`; Swagger поддерживает Authorize.
Токен защищает GET-операции и health, но не превращает сервис в Admin API.
Для недоверенной сети нужны HTTPS/reverse proxy и отдельная настройка доступа.
Никогда не запускайте несколько workers/экземпляров на одном COM-порту.
При запуске напрямую через uvicorn ответственность за bind/token остаётся у оператора:
`MSTJUK_SERIAL_PORT` должен быть задан, `--workers 1`, без reload для рабочего стенда.

На Linux/macOS: `python3 -m venv .venv`, затем `.venv/bin/python -m pip install -r requirements.txt`
и `.venv/bin/python -m hub_service --serial-port /dev/ttyUSB0`.

## Проверки

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты проверяют HTTP, единицы измерения, read-only набор маршрутов, индексы,
ошибки, токен, FIFO, переполнение/отмену/тайм-аут, shutdown/disconnect, восстановление,
CRC/resync/session wrap и 44 ранее согласованных wire-вектора. В POSIX дополнительно
проверяется полный HTTP → pyserial → виртуальный PTY-хаб → HTTP. Старые векторы дверей
с node13 сохранены для проверки байтового формата, а **новый маршрут node15** проверен
отдельными тестами. Windows COM-драйвер и реальное железо эти тесты не заменяют.

[Аппаратный протокол](docs/PROTOCOL.md) · [Конфигурации](docs/GREENHOUSES.md)

Lifespan соответствует [рекомендованному механизму FastAPI](https://fastapi.tiangolo.com/advanced/events/);
serial I/O использует [pySerial API](https://pyserial.readthedocs.io/en/latest/pyserial_api.html).

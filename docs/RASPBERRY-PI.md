# ФабЛаб: автопоиск и запуск FastAPI на Raspberry Pi

Репозиторий сервиса: `tjc-commutator`, ветка `feat/fastapi-hub-service`.
Выполняйте команды из корня этой копии, а не из `mki-ms-tjc-firmwares/scripts`.
Нужна рабочая прошивка хаба, не `hub-test`. Скорость шины — 115200.

## Подготовка и ручная проверка

```bash
git pull --ff-only
sudo apt install python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sudo usermod -aG dialout "$USER"
```

После добавления в группу выйдите из SSH и войдите снова.
Закройте прежний FastAPI и sensor_console, затем:

```bash
.venv/bin/python -m hub_service --serial-port auto --host 0.0.0.0
```

`auto` используется по умолчанию. Поиск выбирает только CH344 `1a86:55d5`,
USB-интерфейс `06`, и не отправляет пробные команды в другие порты.
На Linux работает с `cdc_acm` (`ttyACM*`) и WCH (`ttyCH343USB*`): USB-идентичность
читается из sysfs, поэтому смена номера tty не мешает. На Windows используется
VID/PID и MI_06 из hardware ID, если драйвер предоставляет эту информацию.
При отсутствии устройства или нескольких подходящих каналах запуск завершается
с понятной ошибкой; произвольный порт не выбирается. Можно указать порт явно
через `--serial-port` или `MSTJUK_SERIAL_PORT`.

Проверка из другого терминала:

```bash
curl http://127.0.0.1:8000/api/v1/hub/probe
```

Swagger: `http://IP_RASPBERRY:8000/docs`. API доступен в локальной сети без токенов.
Остановите ручной сервис через Ctrl+C перед установкой systemd.

## Установка автозапуска

Генератор вписывает фактические путь репозитория и имя текущего пользователя.
Сам генератор запускайте без sudo; сервис работает от этого пользователя,
с дополнительной группой dialout.

```bash
.venv/bin/python scripts/systemd_unit.py > /tmp/mstjuk-api.service
sudo install -m 644 /tmp/mstjuk-api.service /etc/systemd/system/mstjuk-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now mstjuk-api.service
sudo systemctl status mstjuk-api.service --no-pager
```

При ошибке генератора не устанавливайте пустой файл. При переносе репозитория
повторите генерацию и установку. Один systemd-сервис владеет портом, поэтому
sensor_console одновременно с ним запускать нельзя. Для ручной работы через
HTTP используйте `python3 scripts/api_console.py` — она не открывает USB.

Сервис запускается при включении Pi, перезапускается через 5 секунд после выхода
процесса и повторяет запуск, если USB ещё не подключён. При ошибке порта во время
запроса worker завершается, CLI останавливает HTTP-сервер, systemd запускает
новый процесс и заново ищет порт. Старые запросы и управляющие команды не
воспроизводятся. Потеря USB во время простоя обнаруживается при следующем
аппаратном запросе. HOST_TIMEOUT без ошибки самого порта возвращается клиенту;
он не вызывает бесконечную перезагрузку сервиса.

## Конфигурация и журнал

Необязательный файл `/etc/default/mstjuk-api`:

```ini
MSTJUK_SERIAL_PORT=auto
MSTJUK_HTTP_HOST=0.0.0.0
MSTJUK_HTTP_PORT=8000
```

После изменения конфигурации:

```bash
sudo systemctl restart mstjuk-api
sudo journalctl -u mstjuk-api -f
```

Остановка и отключение автозапуска:

```bash
sudo systemctl disable --now mstjuk-api
```

Проверка восстановления: отключите USB, выполните аппаратный HTTP-запрос,
подключите USB обратно и повторите `hub/probe` после запуска сервиса.
Один запрос при обрыве может закончиться ошибкой; его SET автоматически не повторяется.

Установка этого сервиса **не делает экспериментальный драйвер WCH постоянным**.
Временный `insmod` и ручной bind исчезают после перезагрузки; обычный `cdc_acm`
снова может занять устройство. Автопоиск поддерживает оба драйвера, но не меняет их.
Постоянную настройку WCH следует делать после успешной проверки его работы.

Механизмы: [pySerial list_ports](https://pyserial.readthedocs.io/en/latest/tools.html)
и [systemd Restart](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

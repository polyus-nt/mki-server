# Raspberry Pi: теплица химреактора + химический реактор

Один FastAPI, одна шина МС-ТЮК, один USB-порт. Репозиторий `tjc-commutator`,
ветка `feat/chemical-reactor-integration`, рабочая папка `chem-reactor-теплица`.
HTTP-порт 8001, панель `/panel`, Swagger `/docs`.

## Код и зависимости

Клонируйте эту ветку по URL вашего репозитория `tjc-commutator`:

```bash
git clone --branch feat/chemical-reactor-integration URL_РЕПОЗИТОРИЯ tjc-commutator-chem
cd tjc-commutator-chem/chem-reactor-теплица
sudo apt update
sudo apt install python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`URL_РЕПОЗИТОРИЯ` замените своим адресом. Если уже есть копия для ФабЛаба,
её origin можно узнать командой `git -C /путь/к/копии remote get-url origin`.
Отдельная копия позволяет не переключать ветку работающего ФабЛаба.
Для сервиса прошивки клонировать не нужно. Рабочие образы аппаратуры должны
соответствовать химреактору и шине 115200; `hub-test` для сервиса не подходит.

## Драйвер CH344

Если драйвер WCH уже постоянно установлен на этом Pi, повторять установку
не нужно. На другой Raspberry сначала соберите его под её запущенное ядро:

```bash
uname -r
ls -l "/lib/modules/$(uname -r)/build/Makefile"
```

Если Makefile отсутствует:

```bash
sudo apt install build-essential git kmod "linux-headers-$(uname -r)"
```

Если пакет недоступен — остановитесь: нужны заголовки именно работающего ядра.

```bash
cd ~
git clone --depth 1 https://github.com/WCHSoftGroup/ch343ser_linux.git
cd ch343ser_linux/driver
make -j2
modinfo -F vermagic ./ch343.ko
```

Начало vermagic должно совпасть с `uname -r`. Если папка уже есть, используйте
её; для нового ядра выполните `make clean` и `make -j2`.

```bash
sudo install -D -m 644 ch343.ko "/lib/modules/$(uname -r)/extra/ch343.ko"
sudo depmod -a
sudo modprobe ch343
echo ch343 | sudo tee /etc/modules-load.d/mstjuk-ch343.conf
sudo tee /etc/udev/rules.d/99-mstjuk-ch343.rules > /dev/null <<'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d5", ATTR{bInterfaceNumber}=="06", DRIVER=="cdc_acm", RUN+="/bin/sh -c 'echo %k > /sys/bus/usb/drivers/cdc_acm/unbind'", RUN+="/bin/sh -c 'echo %k > /sys/bus/usb/drivers/usb_ch343/bind'"
SUBSYSTEM=="tty", KERNEL=="ttyCH343USB*", GROUP="dialout", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo usermod -aG dialout "$USER"
```

Закройте владельцев USB-порта и перезагрузите Pi. После входа:

```bash
lsmod | grep ch343
ls -l /dev/ttyCH343USB*
```

Установка относится к текущему ядру: после обновления ядра требуется пересборка.
Общий cdc_acm не отключается. Правило переключает только CH344 if06, если его
занял cdc_acm; оно основано на [правиле WCH](https://github.com/WCHSoftGroup/ch343ser_linux/blob/main/udev/99-ch34x.rules).

## Проверка и systemd

Вернитесь в `tjc-commutator-chem/chem-reactor-теплица`:

```bash
.venv/bin/python -m hub_service --serial-port auto --host 0.0.0.0
```

Автопоиск выбирает максимальный **числовой** суффикс `/dev/ttyCH343USB*`.
Если их нет — ищет единственный CH344 if06. При нескольких стендах подключите
один либо укажите нужный порт явно. Конфиг `config.json`, переменная
`MSTJUK_SERIAL_PORT` и `--serial-port` позволяют переопределить порт;
CLI имеет наивысший приоритет. Для systemd по умолчанию установлен `auto`.

```bash
curl http://127.0.0.1:8001/api/v1/hub/probe
```

После успешной проверки остановите ручной сервис Ctrl+C:

```bash
.venv/bin/python scripts/systemd_unit.py > /tmp/mstjuk-chem-api.service
sudo install -m 644 /tmp/mstjuk-chem-api.service /etc/systemd/system/mstjuk-chem-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now mstjuk-chem-api
sudo systemctl status mstjuk-chem-api --no-pager
sudo journalctl -u mstjuk-chem-api -f
```

Генератор запускается без sudo. При его ошибке не устанавливайте пустой unit.
Сервис работает от вашего пользователя с группой dialout. Путь к копии кода
сохраняется в unit; при переносе папки перегенерируйте его.
При завершении процесса systemd повторяет запуск через 5 секунд, включая
отсутствие порта при старте. При ошибке USB во время аппаратного запроса
worker заканчивается, HTTP-сервер завершается, новый процесс снова ищет порт.
Во время простоя отключение обнаруживается при следующем аппаратном запросе.
HOST_TIMEOUT сам по себе не перезапускает процесс. Старые действия не повторяются.

Панель с другого компьютера: `http://IP_RASPBERRY:8001/panel`. API без токенов.
HTTP-консоль из той же рабочей папки:

```bash
python3 scripts/api_console.py
```

В консоли: `probe`, `temp 0`, `pwm 0`, `rpm 0`, `help`.
Она может работать одновременно с сервисом, поскольку использует HTTP.
`selftest` также опрашивает химический реактор; если он не подключён, эти
проверки будут с ошибками. Полное описание: [API-COMPLETE.md](API-COMPLETE.md).

Не запускайте FabLab-сервис на том же физическом USB-порту. Для отмены:

```bash
sudo systemctl disable --now mstjuk-chem-api
```

Для удаления настройки WCH: удалите `/etc/udev/rules.d/99-mstjuk-ch343.rules`,
`/etc/modules-load.d/mstjuk-ch343.conf`, установленный `extra/ch343.ko`,
выполните `sudo depmod -a` и перезагрузитесь. Это также затронет ФабЛаб,
если он использует WCH на той же Raspberry.

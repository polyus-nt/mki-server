# Источники переиспользованного кода

Проект не требует соседних репозиториев при запуске или сборке.
В эту папку импортированы и адаптированы рабочие компоненты фаблаб-проекта:

| Компонент | Исходный проект | Изменения химреактора |
| --- | --- | --- |
| HTTP FIFO/serial/ошибки/настройки | `tjc-commutator/hub_service` | Транспорт и очередь сохранены; отдельные HTTP-методы, кодек результатов и порт 8001 |
| HTTP-консоль | `tjc-commutator/scripts/api_console.py` | Индексы 0/1, MQ-2, общий замок, релейный уровень |
| Hardware API v2 | `tjc-sketch/firmware/common` | GET_GAS 26, два датчика/вентилятора, один выход замка, без VOC/DUST/VACUUM |
| Хаб | `tjc-sketch/firmware/hub` | Одно actuator-устройство без node17 и двухплатной координации |
| Шлюзы/кнопочница | `tjc-sketch/firmware` | Переиспользованы state machines; два шлюза, концевики Button(1)/(2) |
| lmp8 | `tjc-sketch/firmware/lmp8` | Общий замок, включённая релейная матрица с паузой 250 мс |
| Arduino actuator | `arduino-actuator/arduino-actuator-2` | Node14, индексы 0/1, тахометры D8/D12 |
| BMP280/DHT11 и адаптеры | `arduino-sensor` | Те же классы, две независимые копии каждого датчика в новом BoardIO |

MQ2, его ADC-адаптер и chemreactor SensorApi/BoardIO добавлены здесь.
Три C++-копии `HardwareProtocol.h` должны совпадать; это проверяет
`scripts/test_firmware.py`.

Runtime МС-ТЮК — собственная разработка команды lapki-compiler,
ревизия `c0e235f364f21b1a7ba8dbb598ef0bc983511af6`. Неизменённые файлы и
лицензии лежат в `firmware/runtime` и `firmware/runtime-lmp8`, происхождение
описано в их `UPSTREAM.md`. Хеши закреплены в build-скриптах. Патчи UART/FIFO
и startup GPIO применяются только к копии runtime внутри `build`.

SoftwareWire, Adafruit DHT и Austere включены локально в Arduino-папки
с исходными LICENSE/UPSTREAM. PinChangeInterrupt 1.2.9 устанавливается отдельно
через Arduino Library Manager. Лицензии и copyright компонентов сохранены;
данный документ не переопределяет их лицензирование.

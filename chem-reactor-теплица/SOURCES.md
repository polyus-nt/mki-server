# Разделение кода химреактора

У каждого репозитория своя папка `chem-reactor-теплица`:

| Репозиторий | Код |
| --- | --- |
| tjc-commutator | FastAPI, Python-кодек, FIFO, serial, HTTP-консоль и тесты сервиса |
| arduino-sensor | BMP280/DHT11/MQ2, GPIO-адаптеры, SensorApi, Austere, тесты датчиков |
| arduino-actuator | PWM/RPM, GPIO/реле, ActuatorApi, Austere, тесты вентиляторов |
| tjc-sketch | МС-ТЮК-хаб/шлюзы/кнопочница/lmp8, runtime, ARM-билдеры, сквозной C++-тест |

Код адаптирован из фаблаб-проектов, но рабочие фаблаб-прошивки не меняются.
Запуск сервиса и сборка каждого набора прошивок независимы.
Только необязательный интеграционный тест tjc-sketch использует Arduino API
из соседних репозиториев. Он проверяет совпадение трёх C++-копий HardwareProtocol.h
и экспортирует bridge для расширенного режима HTTP-тестов.

Runtime МС-ТЮК и его лицензии находятся в tjc-sketch/chem-reactor-теплица/firmware.
В Arduino-репозиториях локальные библиотеки сохранены вместе с LICENSE/UPSTREAM.
PinChangeInterrupt 1.2.9 устанавливается через Arduino Library Manager.
Этот документ не переопределяет лицензии компонентов.

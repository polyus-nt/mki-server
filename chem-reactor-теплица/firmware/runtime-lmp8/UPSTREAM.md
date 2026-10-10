# Компонент LED восьмилампочницы

Неизменённые `LED.h`, `LEDController.hpp`, `const.h`, `UART.hpp`, `system.c` из
https://github.com/kruzhok-team/lapki-compiler,
ревизия `c0e235f364f21b1a7ba8dbb598ef0bc983511af6`,
`compiler/library/tjc-ms1-lmp8-a1/1.0/source/`.
SHA-256 закреплены в `scripts/build_lmp8.py`, CRLF нормализуется при проверке.
Используемые 11 общих файлов в `../runtime` побайтно совпадают с этой платформой.
UART.hpp и system.c функционально одинаковы, но отличаются whitespace;
они сохранены отдельно с точными хешами исходной платформы, а не объявлены
побайтно одинаковыми.
LED main-a4 здесь не применяется: он имеет другую распиновку и реализацию.

Build создаёт копию в `build/lmp8/platform`, проверяет хеши и только в копии
меняет init контроллера: безопасный неактивный latch ДО GPIO output-mode,
плюс значение PWM-кэша на соответствующий HIGH/LOW для стартового состояния.
Runtime UART получает обычные IRQ/FIFO/unaligned-access исправления проекта.
Оригиналы никогда не патчатся. Для химреактора состояние выхода задано
в `../lmp8/BoardConfig.h`: пины 1–7 HIGH при запуске.

Лицензия lapki-compiler GPLv3: текст в `../runtime/LICENSE`.
Copyright/лицензии общих ARM/ST файлов сохранены; см. `../runtime/UPSTREAM.md`.

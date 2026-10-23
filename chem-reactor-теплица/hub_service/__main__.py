import argparse
import asyncio
from contextlib import suppress
import logging
import os
import json
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .settings import Settings


DEFAULTS = dict(serial_port="auto", host="127.0.0.1", http_port=8001,
                queue_capacity=64, queue_timeout=30, exchange_timeout=5, debug=False)


def config_path():
    # Не cwd и не временная папка распаковки onefile: конфиг рядом с exe.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent.parent / "config.json"


def read_config(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Не удалось прочитать конфиг {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("Конфиг должен содержать JSON-объект")
    unknown = data.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError("Неизвестные настройки: " + ", ".join(sorted(unknown)))
    for key, value in data.items():
        expected = type(DEFAULTS[key])
        valid = (type(value) in (int, float) if key.endswith("timeout")
                 else type(value) is expected)
        if not valid:
            raise ValueError(f"Некорректный тип настройки {key}")
    return data


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description="User/Admin FastAPI поверх хаба МС-ТЮК")
    parser.add_argument("--config", type=Path, help="JSON-конфиг; по умолчанию рядом с exe / в папке сервиса")
    parser.add_argument("--list-ports", action="store_true", help="показать COM-порты без запуска сервера")
    parser.add_argument("--serial-port", help="например COM15; перекрывает конфиг")
    parser.add_argument("--host")
    parser.add_argument("--http-port", type=int)
    parser.add_argument("--queue-capacity", type=int)
    parser.add_argument("--queue-timeout", type=float)
    parser.add_argument("--exchange-timeout", type=float)
    parser.add_argument("--debug", action=argparse.BooleanOptionalAction, default=None,
                        help="журнал RX RAW и кадров TX API")
    args = parser.parse_args(argv)
    if args.list_ports:
        return args
    try:
        path = args.config if args.config is not None else config_path()
        options = dict(DEFAULTS)
        if args.config is not None or path.exists():
            options.update(read_config(path))
        if "MSTJUK_SERIAL_PORT" in os.environ:
            options["serial_port"] = os.environ["MSTJUK_SERIAL_PORT"]
        options.update({k: v for k, v in vars(args).items() if k in DEFAULTS and v is not None})
        args = argparse.Namespace(**options, list_ports=False)
        if not args.host.strip():
            raise ValueError("host не должен быть пустым")
        if not 1 <= args.http_port <= 65535:
            raise ValueError("HTTP порт должен быть 1..65535")
        settings = Settings(args.serial_port, args.queue_capacity, args.queue_timeout,
                            args.exchange_timeout)
    except ValueError as error:
        parser.error(str(error))
    args.settings = settings
    return args


async def serve(app, args):
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.http_port,
                                        workers=1, log_level="debug" if args.debug else "info"))

    async def watch_worker():
        while True:
            await asyncio.sleep(1)
            service = getattr(app.state, "service", None)
            if service is not None and service.task is not None and service.task.done():
                logging.error("Аппаратный worker остановлен; завершаем сервис для перезапуска systemd")
                server.should_exit = True
                return

    watcher = asyncio.create_task(watch_worker())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


def main():
    args = parse_options()
    if args.list_ports:
        from serial.tools.list_ports import comports
        ports = sorted(comports(), key=lambda port: port.device)
        for port in ports:
            print(f"{port.device}: {port.description}")
        if not ports:
            print("COM-порты не найдены. Проверьте USB-подключение и драйвер платы.")
        return
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        logging.warning("API без авторизации доступен по сети: любой клиент может управлять аппаратурой")
    # Несколько workers открыли бы один и тот же аппаратный порт.
    logging.info("Панель: http://%s:%s/panel; COM: %s. Остановка: Ctrl+C",
                 args.host, args.http_port, args.serial_port)
    asyncio.run(serve(create_app(args.settings), args))


if __name__ == "__main__":
    main()

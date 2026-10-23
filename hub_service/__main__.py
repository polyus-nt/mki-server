import argparse
import asyncio
from contextlib import suppress
import logging
import os

import uvicorn

from .app import create_app
from .settings import Settings


async def serve(app, host, port, debug):
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, workers=1,
                                        log_level="debug" if debug else "info"))

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
    parser = argparse.ArgumentParser(description="User/Admin FastAPI поверх хаба МС-ТЮК")
    parser.add_argument("--serial-port", default=os.getenv("MSTJUK_SERIAL_PORT", "auto"),
                        help="auto: CH344 if06; либо COM15, /dev/ttyACM6, /dev/ttyCH343USB0")
    parser.add_argument("--host", default=os.getenv("MSTJUK_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--http-port", type=int, default=os.getenv("MSTJUK_HTTP_PORT", "8000"))
    parser.add_argument("--queue-capacity", type=int, default=64)
    parser.add_argument("--queue-timeout", type=float, default=30)
    parser.add_argument("--exchange-timeout", type=float, default=5)
    parser.add_argument("--debug", action="store_true", help="журнал RX RAW и кадров TX API")
    args = parser.parse_args()
    if not 1 <= args.http_port <= 65535:
        parser.error("HTTP порт должен быть 1..65535")
    try:
        settings = Settings(args.serial_port, args.queue_capacity, args.queue_timeout,
                            args.exchange_timeout)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        logging.warning("API без авторизации доступен по сети: любой клиент может управлять аппаратурой")
    # Несколько workers открыли бы один и тот же аппаратный порт.
    asyncio.run(serve(create_app(settings), args.host, args.http_port, args.debug))


if __name__ == "__main__":
    main()

import argparse
import logging
import os

import uvicorn

from .app import create_app
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Read-only FastAPI поверх хаба МС-ТЮК")
    parser.add_argument("--serial-port", default=os.getenv("MSTJUK_SERIAL_PORT", ""), help="например COM15")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument("--queue-capacity", type=int, default=64)
    parser.add_argument("--queue-timeout", type=float, default=30)
    parser.add_argument("--exchange-timeout", type=float, default=5)
    parser.add_argument("--debug", action="store_true", help="журнал RX RAW и кадров TX API")
    args = parser.parse_args()
    token = os.getenv("MSTJUK_API_TOKEN") or None
    if args.host not in ("127.0.0.1", "::1", "localhost") and not token:
        parser.error("Для доступа не с localhost задайте MSTJUK_API_TOKEN")
    if not 1 <= args.http_port <= 65535:
        parser.error("HTTP порт должен быть 1..65535")
    try:
        settings = Settings(args.serial_port, args.queue_capacity, args.queue_timeout,
                            args.exchange_timeout, token)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    # Несколько workers открыли бы один и тот же аппаратный порт.
    uvicorn.run(create_app(settings), host=args.host, port=args.http_port, workers=1,
                log_level="debug" if args.debug else "info")


if __name__ == "__main__":
    main()

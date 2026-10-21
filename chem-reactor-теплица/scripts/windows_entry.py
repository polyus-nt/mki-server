"""Точка входа PyInstaller; импорт пакета остаётся абсолютным."""
from multiprocessing import freeze_support
import sys
import traceback


def run():
    """Сохраняем сообщение об ошибке в консоли exe, но не блокируем пайпы."""
    code = 0
    try:
        freeze_support()
        # Импорт внутри try: ошибка загрузки зависимостей тоже остаётся видимой.
        from hub_service.__main__ import main
        main()
    except KeyboardInterrupt:
        return 130
    except SystemExit as error:
        code = error.code
        if code is None:
            code = 0
        elif not isinstance(code, int):
            print(code, file=sys.stderr)
            code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    if code not in (0, 130) and sys.stdin is not None and sys.stdin.isatty():
        print("\nСервис завершился с ошибкой. Сообщение об ошибке показано выше.",
              file=sys.stderr, flush=True)
        try:
            input("Нажмите Enter, чтобы закрыть окно...")
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(run())

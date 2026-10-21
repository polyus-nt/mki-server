"""Точка входа PyInstaller; импорт пакета остаётся абсолютным."""
from multiprocessing import freeze_support

from hub_service.__main__ import main


if __name__ == "__main__":
    freeze_support()
    main()

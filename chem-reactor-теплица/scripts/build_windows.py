#!/usr/bin/env python3
"""Собрать Windows onefile EXE с HTML-панелью. Запускать на Windows."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def build_command(python, output):
    return [str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--console", "--noupx", "--name", "ChemicalReactor",
            "--distpath", str(output), "--workpath", str(ROOT / "build/windows/work"),
            "--specpath", str(ROOT / "build/windows"), "--paths", str(ROOT),
            "--add-data", str(ROOT / "hub_service/static") + ":hub_service/static",
            "--collect-submodules", "uvicorn",
            str(ROOT / "scripts/windows_entry.py")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true", help="не обновлять зависимости в .venv-build")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows EXE нужно собирать на Windows: py -3 scripts/build_windows.py")
    build_env = ROOT / ".venv-build"
    python = build_env / "Scripts/python.exe"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(build_env)
    if not args.skip_install:
        subprocess.run([str(python), "-m", "pip", "install", "-r",
                        str(ROOT / "requirements-build.txt")], check=True)
    output = ROOT / "dist/chemical-reactor"
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(build_command(python, output), cwd=ROOT, check=True)
    # Повторный билд не затирает уже настроенный COM-порт получателя.
    config = output / "config.json"
    if not config.exists():
        shutil.copy2(ROOT / "config.example.json", config)
    shutil.copy2(ROOT / "WINDOWS-EXE.md", output / "README.md")
    print(f"\nГотово: {output}\nПередайте всю папку; COM-порт задаётся в config.json.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)

"""Ручной HTTP-клиент: не открывает COM-порт, не требует сторонних библиотек."""
import argparse
from http.client import HTTPException
import json
import math
import shlex
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    # Смена адреса через Location не должна незаметно менять проверяемый сервер.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


COMMANDS = {
    "health": ("/health", None),
    "capabilities": ("/api/v1/capabilities", None),
    "probe": ("/api/v1/hub/probe", None),
    "temp": ("/api/v1/temperature/{idx}", 2),
    "pressure": ("/api/v1/pressure/{idx}", 2),
    "humidity": ("/api/v1/humidity/{idx}", 2),
    "gas": ("/api/v1/gas/{idx}", 2),
    "door": ("/api/v1/doors/{idx}/state", 2),
    "pwm": ("/api/v1/fans/{idx}/pwm", 2),
    "rpm": ("/api/v1/fans/{idx}/rpm", 2),
    "lock": ("/api/v1/lock/state", None),
    "relay": ("/api/v1/relay-fan/level", None),
}
ALIASES = {"get_temp": "temp", "get_pressure": "pressure", "get_gas": "gas",
           "get_humidity": "humidity", "get_door_state": "door",
           "get_pwm": "pwm", "get_rpm": "rpm",
           "get_relay_level": "relay", "get_door_lock": "lock", "set_pwm": "setpwm",
           "set_door_lock": "setlock", "set_relay_level": "setrelay"}
HELP = """health | capabilities | probe
temp 0..1 | pressure 0..1 | humidity 0..1 | gas 0..1
door 0..1 | pwm 0..1 | rpm 0..1 | lock | relay
set_pwm idx pwm — PWM 0..255, переключает вентилятор
set_door_lock 0|1 — 1 блокирует оба замка, 0 разблокирует (Admin)
set_relay_level 0..10 — уровень релейного вентилятора, 0 выключает
get /api/v1/... — произвольный GET на этом же сервере
selftest — последовательно все GET API и все допустимые индексы
help | exit
Selftest только читает; управляющие команды действительно переключают выходы.
User/Admin — группы методов без авторизации."""


class Client:
    def __init__(self, base_url, timeout=45):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("--url должен быть адресом сервера, например http://127.0.0.1:8001")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("--timeout должен быть конечным положительным числом")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def get(self, path):
        return self.request(path)

    def request(self, path, body=None):
        # Принимаем только пути на явно выбранном сервере.
        parsed = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("Ожидается путь на этом сервере, начинающийся с /")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = Request(self.base_url + path, data=data, headers=headers, method="GET" if data is None else "POST")
        started = time.monotonic()
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read()
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                payload = {"error": {"message": "Ответ не является JSON",
                                      "body": raw[:300].decode("utf-8", errors="replace")}}
            ok = (200 <= status < 300 and isinstance(payload, dict)
                  and "error" not in payload and "detail" not in payload
                  and payload.get("valid", True) is not False)
            return ok, status, payload, time.monotonic() - started
        except (URLError, OSError, TimeoutError, HTTPException) as error:
            return False, None, {"error": {"message": str(error)}}, time.monotonic() - started


def report(path, result, detailed=True, method="GET"):
    ok, status, payload, elapsed = result
    print(f"{'OK' if ok else 'FAIL'} {method} {path} — HTTP {status if status is not None else '-'}, {elapsed:.2f} с")
    if detailed or not ok:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def selftest_paths(schema):
    """OpenAPI — источник списка методов, чтобы новый GET не забыли проверить."""
    paths = []
    definitions = schema.get("paths")
    if not isinstance(definitions, dict) or not definitions:
        raise ValueError("OpenAPI не содержит paths")
    for path, item in definitions.items():
        if "get" not in item:
            continue
        expanded = [path]
        parameters = {p["name"]: p for p in item.get("parameters", [])}
        parameters.update({p["name"]: p for p in item["get"].get("parameters", [])})
        for name, parameter in parameters.items():
            if parameter.get("in") != "path":
                if parameter.get("required"):
                    raise ValueError(f"Нельзя автоматически заполнить параметр {name}: {path}")
                continue
            spec = parameter.get("schema", {})
            low, high = spec.get("minimum"), spec.get("maximum")
            if (spec.get("type") != "integer" or type(low) is not int or type(high) is not int
                    or not 0 <= high - low < 64):
                raise ValueError(f"Для {path} нужны целочисленные границы {name}")
            expanded = [p.replace("{" + name + "}", str(idx))
                        for p in expanded for idx in range(low, high + 1)]
            if len(expanded) > 512:
                raise ValueError("Слишком много комбинаций индексов")
        if any("{" in p or "}" in p for p in expanded):
            raise ValueError(f"Не удалось раскрыть параметры: {path}")
        paths.extend(expanded)
        if len(paths) > 512:
            raise ValueError("Слишком много запросов в selftest")
    if not paths:
        raise ValueError("В OpenAPI нет GET-методов")
    return paths


def selftest(client):
    print("Selftest: только последовательные чтения, без повторов и переключения выходов.")
    discovery = client.get("/openapi.json")
    report("/openapi.json", discovery, detailed=False)
    if not discovery[0]:
        print("Selftest: не удалось получить список API. FAIL.")
        return False
    try:
        paths = selftest_paths(discovery[2])
    except (ValueError, KeyError, TypeError) as error:
        print(f"Selftest: ошибка описания API: {error}")
        return False
    failures = []
    for path in paths:
        result = client.get(path)
        report(path, result, detailed=False)
        if not result[0]:
            failures.append(path)
    print(f"Итог: {len(paths)} запросов API, OK {len(paths) - len(failures)}, ошибок {len(failures)}.")
    if failures:
        print("Не прошли: " + ", ".join(failures))
    return not failures


def execute(client, line):
    parts = shlex.split(line)
    if not parts:
        return True
    command = parts[0].lower()
    command = ALIASES.get(command, command)
    if command == "help" and len(parts) == 1:
        print(HELP)
        return True
    if command == "selftest" and len(parts) == 1:
        return selftest(client)
    if command in ("setpwm", "setlock", "setrelay"):
        required = 2 if command in ("setlock", "setrelay") else 3
        if len(parts) != required:
            raise ValueError("Неверное число аргументов. Введите help.")
        values = [int(value) for value in parts[1:]]
        if command == "setpwm":
            idx, pwm = values
            if not (0 <= idx <= 1 and 0 <= pwm <= 255):
                raise ValueError("set_pwm: idx 0..1, PWM 0..255")
            path, body = f"/api/v1/fans/{idx}/pwm", {"pwm": pwm}
        elif command == "setlock":
            state = values[0]
            if state not in (0, 1):
                raise ValueError("set_door_lock: состояние 0/1, без индекса")
            path, body = "/api/v1/admin/lock", {"lock_state": bool(state)}
        else:
            level = values[0]
            if not 0 <= level <= 10:
                raise ValueError("set_relay_level: уровень 0..10")
            path, body = "/api/v1/relay-fan/level", {"level": level}
        result = client.request(path, body)
        report(path, result, method="POST")
        return result[0]
    if command == "get" and len(parts) == 2:
        path = parts[1]
    elif command in COMMANDS:
        path, count = COMMANDS[command]
        if count is None:
            if len(parts) != 1:
                raise ValueError(f"{command} не принимает индекс")
        else:
            if len(parts) != 2:
                raise ValueError(f"Использование: {command} 0..{count - 1}")
            idx = int(parts[1])
            if not 0 <= idx < count:
                raise ValueError(f"Индекс должен быть 0..{count - 1}")
            path = path.format(idx=idx)
    else:
        raise ValueError("Неизвестная команда. Введите help.")
    result = client.get(path)
    report(path, result)
    return result[0]


def main():
    parser = argparse.ArgumentParser(description="Ручная консоль МС-ТЮК: теплица-химреактор")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--timeout", type=float, default=45, help="таймаут HTTP, секунды")
    parser.add_argument("--command", help='одна команда, например "temp 0" или "selftest"')
    args = parser.parse_args()
    try:
        client = Client(args.url, args.timeout)
    except ValueError as error:
        parser.error(str(error))
    try:
        if args.command is not None:
            try:
                return 0 if execute(client, args.command) else 1
            except ValueError as error:
                print(f"Ошибка команды: {error}")
                return 2
        print(f"Сервер: {client.base_url}. COM-порт открывает только FastAPI.")
        print(HELP)
        while True:
            try:
                line = input("api> ").strip()
                if line.lower() in ("exit", "quit"):
                    return 0
                execute(client, line)
            except ValueError as error:
                print(f"Ошибка команды: {error}")
    except EOFError:
        return 0
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

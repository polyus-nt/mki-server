/* Локальный HTTP-клиент. Ни COM, ни Web Serial, ни автоматических управляющих команд. */
"use strict";
const C = window.PanelCore;
const $ = (id) => document.getElementById(id);
const field = (node, name) => node.querySelector('[data-field="' + name + '"]');
const fmt = value => typeof value === "number" && Number.isFinite(value)
  ? value.toLocaleString("ru-RU", {maximumFractionDigits: 3}) : "—";
const clock = time => new Date(time).toLocaleTimeString("ru-RU");
const motorCards = [], stmCards = [], sensorCells = new Map();
const journal = [];
let busy = false, stopPending = false, cancelled = false;
const greenhousePaths = [];
const reactorPaths = ["/health", "/api/v1/reactor/temperature"];

function notice(message, good = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (good ? " success" : "");
  $("notice").hidden = false;
}
function controls() {
  document.querySelectorAll("[data-write]").forEach(b => { b.disabled = !$("allow-writes").checked || busy || stopPending; });
  document.querySelectorAll("[data-read], #refresh, #selftest, #read-greenhouse").forEach(b => { b.disabled = busy || stopPending; });
  document.querySelectorAll("[data-action=stop], #stop-all").forEach(b => { b.disabled = stopPending; });
}
function log(entry) {
  journal.unshift(entry);
  if (journal.length > 100) journal.pop();
  $("log-list").replaceChildren();
  journal.forEach(e => {
    const row = document.createElement("details");
    row.className = "log-entry" + (e.ok ? "" : " failed");
    const summary = document.createElement("summary");
    for (const [text, cls] of [[e.method, ""], [e.path, "log-path"], [e.status === null ? "СЕТЬ" : String(e.status), "log-status"],
      [e.elapsed_ms + " мс · " + clock(e.at), ""]]) {
      const span = document.createElement("span"); span.textContent = text; span.className = cls; summary.append(span);
    }
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify({запрос: e.body, ответ: e.response}, null, 2);
    row.append(summary, pre); $("log-list").append(row);
  });
  $("log-count").textContent = journal.length + " запросов · последние 100";
}
async function api(path, body = undefined) {
  if (!(path === "/health" || path === "/openapi.json" || path.startsWith("/api/v1/")) || path.startsWith("//"))
    throw new Error("Разрешены только методы API этого сервера.");
  const method = body === undefined ? "GET" : "POST", started = performance.now();
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 120000);
  let status = null, response, ok = false;
  try {
    const result = await fetch(path, {
      method, signal: abort.signal, cache: "no-store", redirect: "error",
      headers: body === undefined ? {"Accept": "application/json"} : {"Accept": "application/json", "Content-Type": "application/json"},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    status = result.status;
    const text = await result.text();
    try { response = JSON.parse(text); } catch (_) { response = {error: {message: "Сервер вернул не JSON", body: text.slice(0, 300)}}; }
    ok = C.success(status, response);
  } catch (e) {
    response = {error: {message: e.name === "AbortError" ? "Истекло время ожидания HTTP." : "Нет ответа сервера: " + e.message}};
  } finally { clearTimeout(timer); }
  log({method, path, body: body === undefined ? null : body, response, status, ok,
    elapsed_ms: Math.round(performance.now() - started), at: Date.now()});
  if (method === "GET") render(path, response, ok);
  if (!ok && method === "POST") notice("Команда не подтверждена. Проверьте состояние устройства перед повтором. Остановка связи не отменяет уже отправленную команду.\n" + errorMessage(response));
  return {ok, response, status};
}
function errorMessage(body) {
  if (body && body.error) return (body.error.code ? body.error.code + ": " : "") + (body.error.message || "Ошибка устройства");
  if (body && body.detail) return "Некорректные параметры: " + JSON.stringify(body.detail);
  return "Нет корректного ответа";
}
function mark(card, ok) {
  card.classList.toggle("stale", !ok);
  const label = field(card, "updated");
  label.classList.toggle("error", !ok);
  label.textContent = ok ? "Прочитано " + clock(Date.now()) + " · без автообновления" : "Ошибка чтения · предыдущие показания устарели";
}
function render(path, body, ok) {
  if (path === "/health") {
    $("connection").className = "pill " + (ok && body.serial_open ? "ok" : "error");
    $("connection").textContent = ok && body.serial_open ? "● Сервис подключён" : "● Нет связи с сервисом / COM";
    $("queue-depth").textContent = ok ? fmt(body.queue_depth) : "—";
    $("queue-meta").textContent = ok ? (body.active ? "Выполняется запрос" : "Ожидает команды") : "Проверьте запуск FastAPI";
    return;
  }
  if (path === "/api/v1/reactor/temperature") {
    $("reactor-temperature").textContent = ok ? fmt(body.value) : "Ошибка";
    $("reactor-temperature-meta").textContent = ok ? "Возраст " + fmt(body.age_ms) + " мс · " + (body.fresh ? "свежие" : "не свежие") + " · " + clock(Date.now()) : errorMessage(body);
    return;
  }
  let match = path.match(/^\/api\/v1\/reactor\/motors\/(\d)\/state$/);
  if (match) {
    const card = motorCards[Number(match[1])]; mark(card, ok);
    if (!ok) { field(card, "mode").textContent = "Ошибка"; field(card, "mode").className = "pill error"; return; }
    field(card, "current-rpm").textContent = fmt(body.current_rpm);
    field(card, "target-rpm").textContent = fmt(body.target_rpm);
    field(card, "sps").textContent = fmt(body.current_sps) + " / " + fmt(body.target_sps);
    field(card, "config").textContent = (body.enabled ? "Включён" : "Выключен") + " / 1:" + body.microstep;
    field(card, "timing").textContent = fmt(body.max_late_us) + " мкс / " + fmt(body.resyncs);
    field(card, "mode").textContent = body.ramping ? "Разгон" : body.running ? "Вращение задано" : body.enabled ? "Удержание" : "Остановлен";
    field(card, "mode").className = "pill " + (body.running ? "ok" : "neutral");
    return;
  }
  match = path.match(/^\/api\/v1\/reactor\/timers\/(\d)$/);
  if (match) {
    const card = stmCards[Number(match[1])];
    const seconds = body.remaining_seconds;
    field(card, "timer").textContent = !ok ? errorMessage(body) : body.state === "idle" ? "не задан" :
      body.state === "finished" ? "время вышло" : Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0") + " · снимок, нажмите «Прочитать канал» для обновления";
    return;
  }
  match = path.match(/^\/api\/v1\/reactor\/stm\/(\d)\/(voltage|current)(\/setpoint)?$/);
  if (match) {
    const card = stmCards[Number(match[1])], name = match[2] + (match[3] ? "-setpoint" : "");
    field(card, name).textContent = ok ? fmt(body.value) : "Ошибка";
    field(card, name).classList.toggle("error-value", !ok);
    field(card, "updated").textContent = (ok ? "Чтение " : "Ошибка · ") + clock(Date.now()) + " · значения читаются раздельно";
    return;
  }
  if (sensorCells.has(path)) {
    const cell = sensorCells.get(path);
    let text = "Ошибка";
    if (ok) {
      if ("closed" in body) text = body.closed ? "Закрыта" : "Открыта";
      else if ("adc" in body) text = fmt(body.adc) + " ADC / " + fmt(body.voltage_v) + " В";
      else text = fmt(body.value) + " " + (body.unit === "degC" ? "°C" : body.unit || "");
      if (body.fresh === false) text += " · старое";
    }
    cell.textContent = text; cell.title = ok ? "Прочитано " + clock(Date.now()) + ("age_ms" in body ? "; возраст " + body.age_ms + " мс" : "") : errorMessage(body);
    cell.classList.toggle("error-value", !ok);
  }
  if (path === "/api/v1/lock/state") $("lock-value").textContent = ok ? (body.lock_output_enabled ? "Задана блокировка обоих замков" : "Задано открытие обоих замков") : errorMessage(body);
  if (path === "/api/v1/relay-fan/level") $("relay-value").textContent = ok ? "Прочитанный уровень: " + body.level : errorMessage(body);
}
async function task(fn, write = false) {
  if (busy || stopPending) { notice("Дождитесь текущего запроса или прервите опрос."); return; }
  if (write && !$("allow-writes").checked) { notice("Сначала включите разрешение управляющих команд."); return; }
  busy = true; cancelled = false; controls();
  try { await fn(); } catch (e) { notice(e.message); }
  finally { busy = false; $("cancel-batch").hidden = true; controls(); }
}
async function write(path, body) {
  await task(async () => {
    const result = await api(path, body);
    if (result.ok) notice("Команда принята. Нажмите «Прочитать», чтобы проверить состояние. Подтверждение не означает, что двигатель физически вращается.", true);
  }, true);
}
async function readPaths(paths, test = false) {
  $("progress-wrap").hidden = false; $("cancel-batch").hidden = false;
  let done = 0, failures = 0;
  const started = performance.now();
  for (const path of paths) {
    if (cancelled) break;
    const result = await api(path);
    done++; if (!result.ok) failures++;
    $("progress-bar").style.width = Math.round(done / paths.length * 100) + "%";
    $("progress-text").textContent = done + " / " + paths.length + " · ошибок " + failures + " · " + Math.round((performance.now() - started) / 1000) + " с";
  }
  if (test) {
    $("test-result").textContent = cancelled ? "Прерван" : failures ? "Есть ошибки" : "Чтения OK";
    $("test-meta").textContent = done + " / " + paths.length + " · ошибок " + failures + " · " + clock(Date.now());
  }
  if (!cancelled) notice((test ? "Selftest" : "Опрос") + " завершён: " + done + " чтений, ошибок " + failures + ". Проверка не подтверждает точность измерений и исправность механики.", failures === 0);
}
async function stopMotor(idx = null) {
  if (stopPending) return;
  cancelled = true; stopPending = true; controls();
  notice("Отправляем stop. Уже выполняющийся аппаратный обмен должен завершиться; это не аварийное аппаратное отключение.");
  try {
    const path = idx === null ? "/api/v1/reactor/motors/stop" : "/api/v1/reactor/motors/" + idx + "/stop";
    const result = await api(path, {});
    if (result.ok) {
      (idx === null ? motorCards : [motorCards[idx]]).forEach(card => {
        card.classList.add("stale"); field(card, "updated").textContent = "Stop принят · прочитайте новое состояние";
      });
      notice("Stop подтверждён сервером. Для проверки нажмите «Прочитать».", true);
    }
  } finally { stopPending = false; controls(); }
}
function bindForm(form, fn) {
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    try { fn(new FormData(form)); } catch (e) { notice(e.message); }
  });
}
function motor(idx) {
  const card = $("motor-template").content.firstElementChild.cloneNode(true);
  field(card, "title").textContent = "Двигатель " + (idx + 1);
  field(card, "node-label").textContent = "РЕАКТОР / ИНДЕКС API " + idx;
  const path = "/api/v1/reactor/motors/" + idx;
  const read = card.querySelector("[data-action=read]"); read.dataset.read = "";
  read.addEventListener("click", () => task(() => readPaths([path + "/state"])));
  card.querySelector("[data-action=stop]").addEventListener("click", () => stopMotor(idx));
  card.querySelector("[data-action=enable]").addEventListener("click", () => write(path + "/enable", {enabled: true}));
  card.querySelector("[data-action=disable]").addEventListener("click", () => write(path + "/enable", {enabled: false}));
  bindForm(card.querySelector("[data-form=speed]"), data => {
    const rate = C.number(data.get("rate"), -7000, 7000);
    write(path + "/" + data.get("unit"), {rate});
  });
  bindForm(card.querySelector("[data-form=micro]"), data => write(path + "/microstep", {divisor: Number(data.get("divisor"))}));
  $("motor-grid").append(card); motorCards.push(card); reactorPaths.push(path + "/state");
}
function stm(idx) {
  const card = $("stm-template").content.firstElementChild.cloneNode(true);
  field(card, "title").textContent = "STM32 " + (idx + 1) + " · idx " + idx;
  const root = "/api/v1/reactor/stm/" + idx;
  const paths = ["/voltage", "/current", "/voltage/setpoint", "/current/setpoint"].map(p => root + p);
  const timerPath = "/api/v1/reactor/timers/" + idx;
  paths.push(timerPath);
  bindForm(card.querySelector("[data-form=timer]"), data => write(timerPath, {minutes: C.number(data.get("minutes"), 0, 65535, 0)}));
  const button = card.querySelector("[data-action=read]"); button.dataset.read = "";
  button.addEventListener("click", () => task(() => readPaths(paths)));
  bindForm(card.querySelector("[data-form=voltage]"), data => write(root + "/voltage/setpoint", {millivolts: C.number(data.get("value"), 0, 6000, 0)}));
  bindForm(card.querySelector("[data-form=current]"), data => write(root + "/current/setpoint", {milliamps: C.number(data.get("value"), 0, 50)}));
  $("stm-grid").append(card); stmCards.push(card); reactorPaths.push(...paths);
}
function greenhouse() {
  for (const [label, path] of [["Температура", "/api/v1/temperature/"], ["Влажность", "/api/v1/humidity/"],
    ["Давление", "/api/v1/pressure/"], ["Газ MQ-2", "/api/v1/gas/"], ["Дверь", "/api/v1/doors/"]]) {
    const row = document.createElement("tr"), name = document.createElement("td"); name.textContent = label; row.append(name);
    for (let idx = 0; idx < 2; idx++) {
      const key = path + idx + (label === "Дверь" ? "/state" : ""), cell = document.createElement("td");
      cell.textContent = "—"; row.append(cell); sensorCells.set(key, cell); greenhousePaths.push(key);
    }
    $("sensors-table").append(row);
  }
  for (let idx = 0; idx < 2; idx++) {
    const row = document.createElement("div"); row.className = "output-row";
    const label = document.createElement("div"), title = document.createElement("strong"), reading = document.createElement("small");
    title.textContent = "Вентилятор " + (idx + 1);
    const pwm = document.createElement("span"), rpm = document.createElement("span");
    pwm.textContent = "—"; rpm.textContent = "—";
    reading.append("PWM ", pwm, " · обороты ", rpm); label.append(title, reading);
    for (const [name, cell] of [["pwm", pwm], ["rpm", rpm]]) {
      const path = "/api/v1/fans/" + idx + "/" + name; sensorCells.set(path, cell); greenhousePaths.push(path);
    }
    const form = document.createElement("form"); form.className = "inline-form";
    const input = document.createElement("input"); input.type = "number"; input.name = "pwm"; input.min = "0"; input.max = "255"; input.step = "1"; input.required = true; input.placeholder = "PWM";
    input.setAttribute("aria-label", "Мощность вентилятора " + (idx + 1) + ", от 0 до 255");
    const button = document.createElement("button"); button.dataset.write = ""; button.disabled = true; button.textContent = "Задать";
    form.append(input, button); row.append(label, form); $("fan-controls").append(row);
    bindForm(form, data => write("/api/v1/fans/" + idx + "/pwm", {pwm: C.number(data.get("pwm"), 0, 255, 0)}));
  }
  greenhousePaths.push("/api/v1/lock/state", "/api/v1/relay-fan/level");
  for (let n = 0; n <= 10; n++) { const o = document.createElement("option"); o.value = n; o.textContent = n; $("relay-level").append(o); }
  bindForm($("relay-form"), data => write("/api/v1/relay-fan/level", {level: Number(data.get("level"))}));
  for (const [id, state] of [["lock-on", true], ["lock-off", false]]) $(id).addEventListener("click", () => {
    if (window.confirm(state ? "Задать блокировку обоих магнитных замков?" : "Открыть оба магнитных замка?")) write("/api/v1/admin/lock", {lock_state: state});
  });
}
for (let idx = 0; idx < 3; idx++) { motor(idx); stm(idx); }
greenhouse();
$("allow-writes").addEventListener("change", () => {
  $("write-state").textContent = $("allow-writes").checked ? "УПРАВЛЕНИЕ РАЗРЕШЕНО" : "ТОЛЬКО ЧТЕНИЕ";
  $("write-state").className = "pill " + ($("allow-writes").checked ? "warn" : "neutral"); controls();
});
$("refresh").addEventListener("click", () => task(() => readPaths(reactorPaths)));
$("read-greenhouse").addEventListener("click", () => task(() => readPaths(greenhousePaths)));
$("selftest").addEventListener("click", () => task(async () => {
  $("test-result").textContent = "Проверяем…";
  const schema = await api("/openapi.json");
  if (!schema.ok) { $("test-result").textContent = "Ошибка API"; throw new Error("Не удалось получить список методов сервера."); }
  if (!cancelled) await readPaths(C.expandReads(schema.response), true);
}));
$("stop-all").addEventListener("click", () => stopMotor());
$("cancel-batch").addEventListener("click", () => { cancelled = true; notice("Опрос завершится после текущего запроса. Уже отправленные действия не отменяются."); });
$("clear-log").addEventListener("click", () => { journal.length = 0; $("log-list").replaceChildren(); $("log-count").textContent = "0 запросов"; });
$("export-log").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(journal, null, 2)], {type: "application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = "reactor-requests.json"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
controls();
api("/health").catch(e => notice(e.message));

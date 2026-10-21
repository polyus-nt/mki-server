(function (root) {
  "use strict";
  function number(text, min, max, decimals = 3) {
    const value = String(text).trim();
    const pattern = new RegExp("^[+-]?\\d+(?:\\.\\d{1," + Math.max(1, decimals) + "})?$");
    if (!pattern.test(value) || (decimals === 0 && value.includes(".")))
      throw new Error("Введите обычное число" + (decimals ? " с точкой, не более " + decimals + " знаков после неё." : " без дробной части."));
    const n = Number(value);
    if (!Number.isFinite(n) || n < min || n > max)
      throw new Error("Допустимый диапазон: от " + min + " до " + max + ".");
    return n;
  }
  function expandReads(schema) {
    if (!schema || !schema.paths || !Object.keys(schema.paths).length) throw new Error("Сервер не вернул описание API.");
    const result = [];
    for (const [path, item] of Object.entries(schema.paths)) {
      if (!item.get) continue;
      if (!(path === "/health" || path.startsWith("/api/v1/")) || path.includes("?") || path.includes("#"))
        throw new Error("Недопустимый адрес в описании API.");
      let expanded = [path];
      const parameters = new Map([...(item.parameters || []), ...(item.get.parameters || [])].map(p => [p.name, p]));
      for (const [name, p] of parameters) {
        if (p.in !== "path") {
          if (p.required) throw new Error("Нужен обязательный параметр " + name);
          continue;
        }
        const s = p.schema || {};
        if (s.type !== "integer" || !Number.isInteger(s.minimum) || !Number.isInteger(s.maximum) ||
            s.minimum > s.maximum || s.maximum - s.minimum >= 64) throw new Error("Нет безопасных границ индекса " + name);
        expanded = expanded.flatMap(value => Array.from({length: s.maximum - s.minimum + 1}, (_, i) =>
          value.replace("{" + name + "}", String(s.minimum + i))));
        if (expanded.length > 512) throw new Error("Слишком много комбинаций индексов.");
      }
      if (expanded.some(p => /[{}]/.test(p))) throw new Error("Не удалось определить индекс.");
      result.push(...expanded);
      if (result.length > 512) throw new Error("Слишком много методов для проверки.");
    }
    if (!result.length) throw new Error("Нет методов чтения.");
    return [...new Set(result)];
  }
  function success(status, body) {
    return status >= 200 && status < 300 && body !== null && typeof body === "object" &&
      !Array.isArray(body) && !("error" in body) && !("detail" in body) && body.valid !== false;
  }
  const core = Object.freeze({number, expandReads, success});
  root.PanelCore = core;
  if (typeof module !== "undefined" && module.exports) module.exports = core;
})(globalThis);

// Запускает настоящий docs/sw.js в изолированном контексте с поддельным окружением service worker и печатает результат JSON.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

function load() {
  const listeners = {};
  const log = { opened: [], navigated: [], focused: 0, shown: [], closed: 0 };
  let windows = [];
  const self = {
    location: { origin: "https://me.github.io" },
    registration: { scope: "https://me.github.io/bilet-dozor/", showNotification: async (title, options) => { log.shown.push({ title, options }); } },
    clients: {
      matchAll: async () => windows,
      openWindow: async (u) => { log.opened.push(u); },
      claim: async () => {},
    },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting: async () => {},
  };
  const ctx = { self, URL, Headers, Request, Response, Promise, setTimeout, clearTimeout, caches: { open: async () => ({ addAll: async () => {}, match: async () => null, put: async () => {} }), keys: async () => [], delete: async () => true }, fetch: async () => { throw new Error("offline"); }, console };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "docs", "sw.js"), "utf8"), ctx);
  return { listeners, log, setWindows: (w) => { windows = w; } };
}

async function click(data, action, windows) {
  const env = load();
  env.setWindows(windows || []);
  let waited;
  env.listeners.notificationclick({ action, notification: { data, close: () => { env.log.closed++; } }, waitUntil: (p) => { waited = p; } });
  await waited;
  return env.log;
}

(async () => {
  const data = { url: "./#w=train:2027-01-09", links: [{ label: "РЖД", url: "https://ticket.rzd.ru/x" }, { label: "Яндекс", url: "javascript:alert(1)" }] };
  const out = {};
  out.defaultNoWindow = await click(data, "");
  const win = { url: "https://me.github.io/bilet-dozor/", focus: async () => {}, navigate: async function (u) { out.navigatedTo = u; } };
  out.defaultWithWindow = await click(data, "", [win]);
  out.action0 = await click(data, "link0", [win]);
  out.unsafeAction = await click(data, "link1");
  out.missingData = await click(undefined, "");
  const env = load();
  let waited;
  env.listeners.push({ data: { json: () => { throw new Error("bad"); }, text: () => "просто текст" }, waitUntil: (p) => { waited = p; } });
  await waited;
  out.garbagePush = env.log.shown;
  console.log(JSON.stringify(out));
})();

/* Service worker: приложение открывается без сети и показывает пуш-уведомления. Классический скрипт, не модуль. */
"use strict";

const CACHE = "bilet-dozor-v1";
const SHELL = [
  "./",
  "index.html",
  "app.js",
  "style.css",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "icons/badge-96.png",
  "icons/favicon-48.png",
];
const NETWORK_TIMEOUT = 6000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) {
        if (key !== CACHE) await caches.delete(key);
      }
      await self.clients.claim();
    })()
  );
});

// Сначала сеть (цены должны быть свежими), без сети — последняя сохранённая копия.
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  event.respondWith(networkFirst(request, url));
});

async function networkFirst(request, url) {
  const cache = await caches.open(CACHE);
  const key = url.origin + url.pathname;
  const network = fetch(new Request(request, { cache: "no-cache" })).then((response) => {
    if (response.ok) cache.put(key, response.clone());
    return response;
  });
  network.catch(() => {});
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), NETWORK_TIMEOUT));
  try {
    return await Promise.race([network, timeout]);
  } catch (err) {
    const cached = (await cache.match(key)) || (request.mode === "navigate" ? await cache.match(new URL("./", self.registration.scope).href) : null);
    if (cached) return cached;
    return network;
  }
}

// Ссылки из уведомления открываем только если это https.
function safeUrl(value, base) {
  try {
    const u = new URL(value, base);
    return u.protocol === "https:" || u.origin === new URL(base).origin ? u.href : null;
  } catch (err) {
    return null;
  }
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = { body: event.data ? event.data.text() : "" };
  }
  const links = Array.isArray(data.links) ? data.links.filter((l) => l && l.label && l.url).slice(0, 2) : [];
  const options = {
    body: data.body || "",
    icon: "icons/icon-192.png",
    badge: "icons/badge-96.png",
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    silent: Boolean(data.silent),
    requireInteraction: Boolean(data.urgent),
    lang: "ru",
    data: { url: data.url || "./", links: links },
    actions: links.map((l, i) => ({ action: "link" + i, title: String(l.label).slice(0, 30) })),
  };
  event.waitUntil(self.registration.showNotification(data.title || "Дозор билетов", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const info = event.notification.data || {};
  let target = new URL(info.url || "./", self.registration.scope).href;
  const m = /^link(\d)$/.exec(event.action || "");
  if (m && info.links && info.links[Number(m[1])]) {
    target = safeUrl(info.links[Number(m[1])].url, self.registration.scope) || target;
  }
  event.waitUntil(
    (async () => {
      const inApp = target.startsWith(self.registration.scope);
      if (inApp) {
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        for (const client of windows) {
          if (client.url.startsWith(self.registration.scope)) {
            await client.focus();
            if ("navigate" in client) await client.navigate(target);
            return;
          }
        }
      }
      await self.clients.openWindow(target);
    })()
  );
});

// Браузер сам сменил подписку: приложение при следующем открытии покажет «подключите заново».
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      windows.forEach((c) => c.postMessage({ type: "pushsubscriptionchange" }));
    })
  );
});

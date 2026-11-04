/* Дозор билетов: интерфейс. Без сборки и без вставки разметки из строк: любой текст из данных попадает на страницу через textContent. */
(function () {
  "use strict";

  var NBSP = " ";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var STALE_AFTER = 60 * 60; // секунд: если запись данных старше, предупреждаем
  var summary = null;
  var history = { series: {} };
  var range = getStored("range") || "all";
  var installPrompt = null;
  var pushError = "";

  // ───────────────────────── помощники ─────────────────────────

  function getStored(key) {
    try { return localStorage.getItem("dozor." + key); } catch (err) { return null; }
  }
  function setStored(key, value) {
    try { localStorage.setItem("dozor." + key, value); } catch (err) { /* приватный режим — не запоминаем */ }
  }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (k) {
      var v = a[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { append(node, c); });
    else node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) node.appendChild(arguments[i]);
    return node;
  }

  function num(n) { return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP); }
  function rub(n) { return num(n) + NBSP + "₽"; }
  function signed(n) { var r = Math.round(n); return (r > 0 ? "+" : r < 0 ? "−" : "") + num(Math.abs(r)); }
  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    return b === 1 ? one : many;
  }
  function seats(n) { return n + NBSP + plural(n, "место", "места", "мест"); }

  var MSK = { timeZone: "Europe/Moscow" };
  function hm(sec) { return new Date(sec * 1000).toLocaleTimeString("ru-RU", Object.assign({ hour: "2-digit", minute: "2-digit" }, MSK)); }
  function dm(sec) { return new Date(sec * 1000).toLocaleDateString("ru-RU", Object.assign({ day: "2-digit", month: "2-digit" }, MSK)); }
  function stamp(sec) { return dm(sec) + " " + hm(sec); }
  function parseIso(text) { return text ? Math.floor(Date.parse(text) / 1000) : null; }
  function longDate(iso) {
    var d = new Date(iso + "T12:00:00Z");
    var wd = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"][d.getUTCDay()];
    return iso.slice(8) + "." + iso.slice(5, 7) + "." + iso.slice(0, 4) + " (" + wd + ")";
  }
  function localClock(text) { var m = /T(\d{2}:\d{2})/.exec(text || ""); return m ? m[1] : ""; }
  function mskClock(text) {
    var t = Date.parse(text || "");
    return isNaN(t) ? "" : hm(Math.floor(t / 1000));
  }
  function titleCase(s) { return String(s || "").toLowerCase().replace(/(^|[\s\-(])(\S)/g, function (m, p, c) { return p + c.toUpperCase(); }); }

  var toastTimer = null;
  function toast(text) {
    var t = document.getElementById("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 3500);
  }

  function nowSec() { return Math.floor(Date.now() / 1000); }

  // ───────────────────────── загрузка ─────────────────────────

  function load() {
    return Promise.all([
      fetch("data/summary.json", { cache: "no-cache" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }),
      fetch("data/history.json", { cache: "no-cache" }).then(function (r) { return r.ok ? r.json() : { series: {} }; }).catch(function () { return { series: {} }; }),
    ]).then(function (res) {
      summary = res[0];
      history = res[1] && res[1].series ? res[1] : { series: {} };
      render();
    }).catch(function (err) {
      document.getElementById("status").textContent = "Нет данных: сервис ещё не делал первую проверку или нет связи (" + err.message + ").";
      renderNothing();
    });
  }

  function renderNothing() {
    var w = document.getElementById("watches");
    w.replaceChildren(el("div", { class: "card" },
      el("p", { class: "card-title", text: "Данных пока нет" }),
      el("p", { class: "muted", text: "Первая проверка запускается автоматически в течение нескольких минут после настройки репозитория. Если вы только что всё настроили, откройте GitHub → Actions → «Проверка билетов» → Run workflow." })));
  }

  // ───────────────────────── график ─────────────────────────

  function seriesFor(watch) {
    return Object.keys(history.series).map(function (k) { return history.series[k]; }).filter(function (s) { return s.watch === watch; });
  }

  function rangePoints(points, since) {
    if (!since) return points.slice();
    var i = 0;
    while (i < points.length && points[i].t < since) i++;
    return points.slice(i > 0 ? i - 1 : 0); // последняя точка до окна: её цена действовала на левом краю
  }

  function chart(watches) {   // одна карточка или несколько (рейсы на разные даты): линии подписываются датой
    var wrap = el("div", { class: "chart" });
    var many = watches.length > 1;
    var all = [];
    watches.forEach(function (w) {
      seriesFor(w.key).forEach(function (s) { all.push(many ? { label: longDate(w.date).replace(/ \(.*\)$/, "").slice(0, 5) + (s.type === "train" ? " " + s.label : ""), points: s.points } : s); });
    });
    all = all.slice(0, 4);
    var seconds = range === "day" ? 86400 : range === "week" ? 7 * 86400 : 0;
    var since = seconds ? nowSec() - seconds : 0;
    var lines = all.map(function (s) { return { label: s.label, points: rangePoints(s.points, since) }; });
    var priced = lines.some(function (l) { return l.points.some(function (p) { return p.p !== null && p.p !== undefined; }); });

    var seg = el("div", { class: "seg", role: "radiogroup", "aria-label": "Период графика" });
    [["day", "Сутки"], ["week", "Неделя"], ["all", "Всё время"]].forEach(function (r) {
      seg.appendChild(el("button", { type: "button", role: "radio", "aria-checked": range === r[0] ? "true" : "false", text: r[1], onclick: function () { range = r[0]; setStored("range", range); render(); } }));
    });
    wrap.appendChild(el("div", { class: "chart-top" }, el("span", { class: "small muted", text: "Динамика цены" }), seg));

    if (!priced) {
      wrap.appendChild(el("p", { class: "empty-chart", text: "Цен пока нет: график появится, когда билеты поступят в продажу." }));
      return wrap;
    }

    var W = 320, H = 130, L = 46, R = 8, T = 8, B = 20;
    var tmin = Infinity, tmax = -Infinity, pmin = Infinity, pmax = -Infinity;
    lines.forEach(function (l) { l.points.forEach(function (p) {
      tmin = Math.min(tmin, p.t); tmax = Math.max(tmax, p.t);
      if (p.p !== null && p.p !== undefined) { pmin = Math.min(pmin, p.p); pmax = Math.max(pmax, p.p); }
    }); });
    var now = nowSec();
    tmax = Math.max(tmax, since ? now : tmax);
    if (since) tmin = Math.max(tmin, since); else tmax = Math.max(tmax, now);
    if (tmax - tmin < 60) tmin = tmax - 60;
    if (pmax - pmin < 1) { pmin -= 50; pmax += 50; }
    var pad = (pmax - pmin) * 0.12; pmin = Math.max(0, pmin - pad); pmax += pad;
    var x = function (t) { return L + ((Math.max(t, tmin) - tmin) / (tmax - tmin)) * (W - L - R); };
    var y = function (p) { return T + (1 - (p - pmin) / (pmax - pmin)) * (H - T - B); };

    var root = svg("svg", { class: "plot", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "График цены" });
    [0, 0.5, 1].forEach(function (f) {
      var v = pmin + (pmax - pmin) * f, yy = y(v);
      root.appendChild(svg("line", { class: "grid", x1: L, x2: W - R, y1: yy, y2: yy }));
      var lab = svg("text", { class: "axis", x: L - 5, y: yy + 3, "text-anchor": "end" }); lab.textContent = num(v);
      root.appendChild(lab);
    });
    [[tmin, "start"], [tmax, "end"]].forEach(function (a) {
      var lab = svg("text", { class: "axis", x: a[1] === "start" ? L : W - R, y: H - 5, "text-anchor": a[1] }); lab.textContent = stamp(a[0]);
      root.appendChild(lab);
    });

    lines.forEach(function (l, i) {
      var d = "", pen = false, last = null, lastT = null;
      l.points.forEach(function (p, idx) {
        if (p.p === null || p.p === undefined) { pen = false; return; }
        var px = x(p.t).toFixed(1), py = y(p.p).toFixed(1);
        if (!pen) { d += "M" + px + " " + py; pen = true; }
        else d += "H" + px + "V" + py; // ступенька: цена держится до следующего изменения
        last = [px, py]; lastT = idx;
      });
      if (last) {
        var endX = x(tmax).toFixed(1);
        if (pen && endX > last[0]) d += "H" + endX;
        root.appendChild(svg("path", { class: "line s" + i, d: d }));
        root.appendChild(svg("circle", { class: "dot s" + i, cx: pen ? endX : last[0], cy: last[1], r: 3.5 }));
      }
    });
    wrap.appendChild(root);
    if (lines.length > 1) {
      wrap.appendChild(el("div", { class: "legend" }, lines.map(function (l, i) { return el("span", {}, el("i", { class: "s" + i }), l.label); })));
    }
    return wrap;
  }

  // ───────────────────────── карточки ─────────────────────────

  function statusChip(w) {
    if (w.status === "config") return el("span", { class: "chip warn", text: "Не настроено" });
    if (w.status === "pending") return el("span", { class: "chip", text: "Ещё не проверял" });
    if (w.status === "error" && !w.last_ok_at) return el("span", { class: "chip bad", text: "Ошибка" });
    if (w.available) return el("span", { class: "chip good", text: "Есть билеты" });
    return el("span", { class: "chip" + (w.status === "error" ? " warn" : ""), text: w.status === "error" ? "Нет билетов · ошибка" : "Билетов нет" });
  }

  function trainRows(w) {
    if (!w.trains.length) {
      var all = w.all_numbers && w.all_numbers.length ? "Поезда в выдаче: " + w.all_numbers.join(", ") + "." : "РЖД не показал ни одного поезда на эту дату.";
      return el("p", { class: "muted", text: all + (w.watched_trains.length ? " Слежу за: " + w.watched_trains.join(", ") + "." : "") });
    }
    return el("ul", { class: "rows" }, w.trains.map(function (t) {
      var state = t.seats ? seats(t.seats) + (t.min_price != null ? ", от " + rub(t.min_price) : "") : (t.no_data ? "нет данных о местах" : "мест нет");
      return el("li", { class: "row" },
        el("div", { class: "row-head" }, el("span", { text: t.display_number || t.number }), el("span", { text: state })),
        el("div", { class: "row-sub", text: titleCase(t.from) + " " + localClock(t.depart_local) + " → " + titleCase(t.to) + " " + localClock(t.arrive_local) }),
        t.groups.map(function (g) {
          return el("div", { class: "row-sub", text: g.label + " — " + (g.approx ? "места есть" : seats(g.seats)) + (g.min_price != null ? ", от " + rub(g.min_price) : "") });
        }));
    }));
  }

  function offerRows(w) {
    if (!w.offers.length) return el("p", { class: "muted", text: "Прямых билетов в кэше Aviasales сейчас нет. Это не значит, что их нет в продаже: кэш обновляется по поискам пользователей." });
    return el("ul", { class: "rows" }, w.offers.slice(0, 5).map(function (o) {
      return el("li", { class: "row" },
        el("div", { class: "row-head" }, el("span", { text: [o.airline_name || o.airline, o.flight_number].filter(Boolean).join(" ") }), el("span", { text: rub(o.price) })),
        el("div", { class: "row-sub", text: "вылет " + mskClock(o.departure_at) + " МСК" + (o.destination_airport ? " · " + o.destination_airport : "") }),
        o.stale ? el("div", { class: "stale", text: "сейчас нет в кэше, держу в списке до 6 часов" }) : null);
    }));
  }

  function watchCard(w) {
    var hot = w.available;
    var big = null;
    if (w.available && w.min_price != null) {
      var c = w.change24h;
      big = el("div", { class: "big" }, el("span", { class: "price", text: "от " + rub(w.min_price) }),
        c && c.delta ? el("span", { class: "delta " + (c.delta < 0 ? "down" : "up"), text: (c.delta < 0 ? "▼ " : "▲ ") + signed(c.delta) + NBSP + "₽ " + (nowSec() - c.since >= 20 * 3600 ? "за сутки" : "с " + stamp(c.since)) }) : null);
    }
    var actions = w.links.length ? el("div", { class: "actions" }, w.links.map(function (l) {
      return el("a", { class: "btn" + (l.primary ? " primary" : ""), href: l.url, target: "_blank", rel: "noopener", text: l.label });
    })) : null;
    var note = [];
    if (w.error) note.push(el("p", { class: "small", text: (w.status === "config" ? "" : "Последняя проверка не удалась: ") + w.error }));
    if (w.status === "ok" && w.type === "train" && w.partial) note.push(el("p", { class: "small muted", text: "РЖД вернул неполный список поездов." }));
    if (w.first_available_at) note.push(el("p", { class: "small muted", text: "Билеты впервые появились " + stamp(w.first_available_at) + " МСК." }));
    var when = w.last_ok_at ? "Данные от " + stamp(w.last_ok_at) + " МСК" : "";
    var srcs = (w.sources || []).map(function (n) { return (summary.sources[n] && summary.sources[n].title) || n; }).join(", ");

    return el("article", { class: "card" + (hot ? " hot" : ""), id: "w-" + w.key.replace(":", "-"), "data-watch": w.key },
      el("div", { class: "card-head" },
        el("div", {}, el("h2", { class: "card-title", text: (w.type === "train" ? "🚆 " : "✈️ ") + w.title + " · " + longDate(w.date) }), el("div", { class: "card-sub", text: w.subtitle + (w.label ? " · " + w.label : "") })),
        statusChip(w)),
      big,
      w.type === "train" ? trainRows(w) : offerRows(w),
      chart([w]),
      actions,
      note,
      el("p", { class: "small muted", text: [when, srcs ? "источник: " + srcs : ""].filter(Boolean).join(" · ") }));
  }

  function bestOf(list) {
    var priced = list.filter(function (w) { return w.available && w.min_price != null; });
    return priced.sort(function (a, b) { return a.min_price - b.min_price; })[0] || null;
  }

  function flightGroupCard(list) {
    // рейсы на соседние даты — одна карточка: по разделу на дату, общий график
    if (list.length === 1) return watchCard(list[0]);
    var any = list.some(function (w) { return w.available; });
    var best = bestOf(list);
    var dates = list.map(function (w) { return longDate(w.date); });
    var chip = any ? el("span", { class: "chip good", text: "Есть билеты" })
      : list.some(function (w) { return w.status === "error"; }) ? el("span", { class: "chip warn", text: "Нет билетов · ошибка" })
      : list.every(function (w) { return w.status === "pending"; }) ? el("span", { class: "chip", text: "Ещё не проверял" })
      : el("span", { class: "chip", text: "Билетов нет" });
    var sections = list.map(function (w) {
      var links = w.links.length ? el("div", { class: "actions" }, w.links.map(function (l) {
        return el("a", { class: "btn" + (l.primary && w.available ? " primary" : ""), href: l.url, target: "_blank", rel: "noopener", text: l.label + " · " + dates[list.indexOf(w)].slice(0, 5) });
      })) : null;
      var state = w.status === "pending" ? "ещё не проверял" : w.available ? (w.offers.length + " вар., от " + rub(w.min_price)) : "билетов нет";
      return el("section", { class: "day", id: "w-" + w.key.replace(":", "-"), "data-watch": w.key },
        el("div", { class: "row-head" }, el("h3", { class: "day-title", text: longDate(w.date) }), el("span", { text: state })),
        w.available ? offerRows(w) : null,
        w.error ? el("p", { class: "small", text: "Последняя проверка не удалась: " + w.error }) : null,
        w.first_available_at ? el("p", { class: "small muted", text: "Билеты впервые появились " + stamp(w.first_available_at) + " МСК." }) : null,
        links);
    });
    var nobody = !any ? el("p", { class: "muted", text: "Прямых билетов в кэше Aviasales сейчас нет. Это не значит, что их нет в продаже: кэш обновляется по поискам пользователей." }) : null;
    var big = best ? el("div", { class: "big" }, el("span", { class: "price", text: "от " + rub(best.min_price) }), el("span", { class: "small muted", text: " · " + longDate(best.date) })) : null;
    var okAt = list.map(function (w) { return w.last_ok_at || 0; }).reduce(function (a, b) { return Math.max(a, b); }, 0);
    var srcs = (list[0].sources || []).map(function (n) { return (summary.sources[n] && summary.sources[n].title) || n; }).join(", ");
    return el("article", { class: "card" + (any ? " hot" : ""), id: "w-flights", "data-watch": list[0].key },
      el("div", { class: "card-head" },
        el("div", {}, el("h2", { class: "card-title", text: "✈️ Самолёт · " + dates.map(function (d) { return d.slice(0, 5); }).join(" или ") + "." + list[0].date.slice(0, 4) }), el("div", { class: "card-sub", text: list[0].subtitle })),
        chip),
      big,
      nobody,
      sections,
      chart(list),
      el("p", { class: "small muted", text: [okAt ? "Данные от " + stamp(okAt) + " МСК" : "", srcs ? "источник: " + srcs : ""].filter(Boolean).join(" · ") }));
  }

  function cards(watches) {
    var flights = watches.filter(function (w) { return w.type === "flight"; });
    var out = [];
    watches.forEach(function (w) {
      if (w.type !== "flight") out.push(watchCard(w));
      else if (w === flights[0]) out.push(flightGroupCard(flights));
    });
    return out;
  }

  // ───────────────────────── уведомления ─────────────────────────

  function isStandalone() { return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true; }
  function isIOS() { return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1); }

  function b64urlToBytes(text) {
    var padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
    return Uint8Array.from(atob(padded), function (c) { return c.charCodeAt(0); });
  }
  function bytesToB64url(buffer) {
    var s = ""; new Uint8Array(buffer).forEach(function (b) { s += String.fromCharCode(b); });
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function deviceId(endpoint) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint)).then(function (hash) {
      return Array.from(new Uint8Array(hash), function (b) { return b.toString(16).padStart(2, "0"); }).join("").slice(0, 12);
    });
  }
  function activeRegistration() {
    if (!("serviceWorker" in navigator)) return Promise.resolve(null);
    return Promise.race([navigator.serviceWorker.ready, new Promise(function (r) { setTimeout(function () { r(null); }, 8000); })]);
  }

  function pushState() {
    var conf = summary && summary.push;
    if (!conf || !conf.vapid || !summary.repo) return Promise.resolve({ kind: "off" });
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return Promise.resolve({ kind: isIOS() && !isStandalone() ? "ios-install" : "unsupported" });
    }
    if (Notification.permission === "denied") return Promise.resolve({ kind: "denied" });
    return activeRegistration().then(function (reg) {
      if (!reg) return { kind: "unsupported" };
      return reg.pushManager.getSubscription().then(function (sub) {
        if (!sub) return { kind: "none" };
        return deviceId(sub.endpoint).then(function (id) {
          var key = sub.options && sub.options.applicationServerKey ? bytesToB64url(sub.options.applicationServerKey) : conf.vapid;
          if (key !== conf.vapid) return { kind: "stale", sub: sub, id: id };
          if ((conf.devices || []).indexOf(id) >= 0) return { kind: "on", sub: sub, id: id };
          return { kind: "pending", sub: sub, id: id };
        });
      });
    });
  }

  function enablePush(button) {
    button.disabled = true;
    pushError = "";
    Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") return null;
      return activeRegistration().then(function (reg) {
        if (!reg) throw new Error("фоновый обработчик не запустился, обновите страницу");
        return reg.pushManager.getSubscription().then(function (old) { return old ? old.unsubscribe() : null; }).then(function () {
          return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlToBytes(summary.push.vapid) });
        });
      });
    }).catch(function (err) {
      var msg = err && err.message ? err.message : String(err);
      pushError = "Не получилось включить уведомления: " + msg + (/abort|private|incognito/i.test(msg) ? " (в режиме инкогнито уведомления не работают)" : "");
    }).then(renderNotify);
  }

  function githubNewFile(sub, id) {
    return "https://github.com/" + summary.repo + "/new/main?filename=" + encodeURIComponent("push/" + id + ".json") + "&value=" + encodeURIComponent(JSON.stringify(sub.toJSON(), null, 2));
  }

  function renderNotify() {
    var box = document.getElementById("notify");
    if (!summary) return;
    pushState().catch(function () { return { kind: "unsupported" }; }).then(function (state) {
      var items = [el("h2", { text: "Уведомления" })];
      if (!isStandalone()) {
        if (installPrompt) {
          items.push(el("p", { text: "Установите приложение: иконка на главном экране, открывается без браузера и присылает уведомления." }),
            el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", text: "Установить", onclick: function () {
              var p = installPrompt; installPrompt = null; p.prompt();
              Promise.resolve(p.userChoice).catch(function () {}).then(renderNotify);
            } })));
        } else if (isIOS()) {
          items.push(el("p", { text: "iPhone: в Safari нажмите «Поделиться» (квадрат со стрелкой) → «На экран „Домой“», затем откройте приложение с экрана «Домой». Уведомления работают только так (iOS 16.4 и новее)." }));
        } else {
          items.push(el("p", { class: "muted", text: "Лучше установить приложение: меню браузера → «Установить приложение» или «Добавить на главный экран»." }));
        }
      }
      var k = state.kind;
      if (k === "off") {
        items.push(el("p", { class: "muted", text: "Пуш-уведомления будут доступны после первой проверки в репозитории на GitHub (нужен публичный ключ в данных)." }));
      } else if (k === "none") {
        items.push(el("p", { text: "Сообщу сразу, как только появятся билеты, и пришлю изменения цены." }),
          el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", text: "Включить уведомления", onclick: function (ev) { enablePush(ev.currentTarget); } })));
      } else if (k === "pending") {
        var json = JSON.stringify(state.sub.toJSON(), null, 2);
        var copy = el("button", { class: "link-btn", type: "button", text: "Скопировать" });
        copy.addEventListener("click", function () {
          (navigator.clipboard ? navigator.clipboard.writeText(json) : Promise.reject()).then(function () { copy.textContent = "Скопировано"; }, function () { copy.textContent = "Выделите текст и скопируйте вручную"; });
        });
        items.push(el("p", {}, el("b", { text: "Остался один шаг. " }), "Откроется GitHub с готовым файлом подписки: нажмите «Commit changes». Через несколько минут придёт пробное уведомление, а здесь появится галочка."),
          el("div", { class: "actions" }, el("a", { class: "btn primary", href: githubNewFile(state.sub, state.id), target: "_blank", rel: "noopener", text: "Открыть GitHub" })),
          el("details", {}, el("summary", { text: "Если файл открылся пустым" }),
            el("p", { class: "small muted", text: "Создайте в репозитории файл push/" + state.id + ".json и вставьте в него этот текст:" }),
            el("pre", { text: json }), copy));
      } else if (k === "stale") {
        items.push(el("p", { text: "Ключ уведомлений сменился — подключите их заново." }),
          el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", text: "Подключить заново", onclick: function (ev) { enablePush(ev.currentTarget); } })));
      } else if (k === "ios-install") {
        items.push(el("p", { text: "На iPhone уведомления работают только в установленном приложении." }));
      } else if (k === "denied") {
        items.push(el("p", { text: "Уведомления запрещены. Разрешить их можно в настройках телефона или браузера для этого сайта." }));
      } else if (k === "unsupported") {
        items.push(el("p", { class: "muted", text: "Этот браузер не поддерживает пуш-уведомления. Попробуйте Chrome на Android или установленное приложение на iPhone." }));
      } else if (k === "on") {
        items.push(el("p", {}, "✓ Уведомления приходят на этот телефон. ", el("button", { class: "link-btn", type: "button", text: "Отключить", onclick: function () {
          Promise.resolve(state.sub.unsubscribe()).catch(function () {}).then(function () { toast("Отключено. Файл подписки в папке push/ можно удалить на GitHub."); renderNotify(); });
        } })));
      }
      if (pushError) items.push(el("p", { class: "small", text: pushError }));
      box.replaceChildren.apply(box, items);
    });
  }

  // ───────────────────────── журнал и источники ─────────────────────────

  function renderEvents() {
    var box = document.getElementById("events");
    var list = summary.events || [];
    if (!list.length) { box.replaceChildren(el("h2", { text: "Журнал" }), el("p", { class: "muted", text: "Событий пока нет. Когда появятся билеты или изменится цена, запись появится здесь." })); return; }
    box.replaceChildren(el("h2", { text: "Журнал" }), el("ul", { class: "log" }, list.slice(0, 15).map(function (e) {
      return el("li", { class: e.urgent ? "urgent" : "" }, el("div", { class: "t", text: stamp(e.t) + " МСК" }), el("div", { class: "title", text: e.title }), el("div", { class: "small", text: e.body }));
    })));
  }

  var KIND_HINT = {
    blocked: "РЖД закрыл доступ с адресов GitHub. Нужен российский прокси: секрет RZD_PROXY (см. README).",
    network: "Нет связи с источником. Для сайта РЖД с зарубежных серверов это типично: см. README, «РЖД не отвечает».",
    schema: "Формат ответа изменился. Откройте Actions → Run workflow → probe и пришлите вывод разработчику.",
    config: "Не хватает настройки (секрета).",
  };

  function renderSources() {
    var box = document.getElementById("sources");
    var items = [el("h2", { text: "Источники данных" })];
    Object.keys(summary.sources || {}).forEach(function (name) {
      var s = summary.sources[name];
      if (!s.enabled) return;
      var okAt = s.last_ok && s.last_ok.at, errAt = s.last_error && s.last_error.at;
      var failing = errAt && (!okAt || errAt >= okAt);
      var status = !s.configured ? "не настроен" : failing ? "ошибка" : okAt ? "отвечает" : "ещё не проверялся";
      var node = el("div", { class: "src" },
        el("div", { class: "row-head" }, el("span", { text: s.title }), el("span", { class: "chip " + (failing || !s.configured ? "bad" : okAt ? "good" : ""), text: status })),
        okAt ? el("div", { class: "row-sub", text: "Последний успешный ответ: " + stamp(okAt) + " МСК" + (s.last_ok.ms != null ? " (" + s.last_ok.ms + " мс)" : "") }) : null);
      if (failing) {
        node.appendChild(el("div", { class: "small", text: s.last_error.message }));
        if (KIND_HINT[s.last_error.kind]) node.appendChild(el("div", { class: "small muted", text: KIND_HINT[s.last_error.kind] }));
        if (s.last_error.excerpt) node.appendChild(el("details", {}, el("summary", { text: "Фрагмент ответа" }), el("pre", { text: s.last_error.excerpt })));
      }
      items.push(node);
    });
    box.replaceChildren.apply(box, items);
  }

  function renderFoot() {
    var f = document.getElementById("foot");
    f.replaceChildren();
    if (!summary || !summary.repo) return;
    var base = "https://github.com/" + summary.repo;
    [["Настройки", base + "/edit/main/config.yaml"], ["Запустить проверку", base + "/actions/workflows/check.yml"], ["Репозиторий", base]].forEach(function (l) {
      f.appendChild(el("a", { href: l[1], target: "_blank", rel: "noopener", text: l[0] }));
    });
  }

  // ───────────────────────── страница ─────────────────────────

  function render() {
    var checked = parseIso(summary.checked_at);
    var age = checked ? nowSec() - checked : null;
    document.getElementById("status").textContent = checked
      ? "Обновлено " + stamp(checked) + " МСК · GitHub проверяет примерно раз в 10 минут"
      : "Ждём первую проверку";
    var banners = [];
    if (age !== null && age > STALE_AFTER) {
      banners.push(el("div", { class: "banner bad", text: "Данные не обновлялись больше " + Math.floor(age / 3600) + " ч. Проверьте GitHub → Actions: расписание могло быть отключено (после 60 дней без изменений GitHub выключает его) или проверка падает с ошибкой." }));
    }
    document.getElementById("banners").replaceChildren.apply(document.getElementById("banners"), banners);
    var box = document.getElementById("watches");
    if (!summary.watches.length) renderNothing();
    else box.replaceChildren.apply(box, cards(summary.watches));
    renderNotify(); renderEvents(); renderSources(); renderFoot();
    focusFromHash();
  }

  function focusFromHash() {
    var m = /w=([^&]+)/.exec(location.hash || "");
    if (!m) return;
    var key = decodeURIComponent(m[1]).replace(":", "-");
    var node = document.getElementById("w-" + key);
    if (node) { node.classList.add("focus"); node.scrollIntoView({ block: "start" }); }
  }

  // ───────────────────────── запуск ─────────────────────────

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(function () {});
    navigator.serviceWorker.addEventListener("message", function (ev) { if (ev.data && ev.data.type === "pushsubscriptionchange") renderNotify(); });
  }
  window.addEventListener("beforeinstallprompt", function (ev) { ev.preventDefault(); installPrompt = ev; if (summary) renderNotify(); });
  window.addEventListener("appinstalled", function () { installPrompt = null; if (summary) renderNotify(); });
  window.addEventListener("hashchange", focusFromHash);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) load(); });
  load();
  setInterval(function () { if (!document.hidden) load(); }, 5 * 60 * 1000);
})();

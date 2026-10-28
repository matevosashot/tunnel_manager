import { icon } from "./icons.js";

// ------------------------------------------------------------------ helpers

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
const now = () => Date.now() / 1000;

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Tunnel-Manager": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function fmtDur(s) {
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
function fmtAgo(ts) {
  const s = now() - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}d ago`;
  if (s < 86400 * 60) return `${Math.floor(s / 604800)}w ago`;
  return `${Math.floor(s / 2592000)}mo ago`;
}
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
}
function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function hydrateIcons(root = document) {
  $$("[data-icon]", root).forEach((el) => { el.innerHTML = icon(el.dataset.icon); });
}

// ------------------------------------------------------------------ state

const S = {
  state: null,
  hosts: new Map(),       // alias -> {hostname, user, port, proxyjump}
  hostList: [],
  keys: null,
  open: new Set(),        // ids with the details drawer open
  renaming: null,
  histOrder: [],          // frozen order of history rows (no jumping while you click)
  sort: store.get("tm-sort", "recent"),
  query: "",
  lastSeq: null,
  watch: new Map(),       // id -> deadline: report failure of a start the user just clicked
  logs: new Map(),        // id -> {offset}
  dragging: false,
  connected: false,
};

const ACTIVE = new Set(["starting", "up", "retrying", "offline"]);
const ERR_SHORT = {
  auth: "key refused", hostkey: "host key problem", config: "configuration error",
  local_port: "local port in use", remote_port: "remote port busy", dns: "DNS lookup failed",
  network: "network error", target: "target refused",
};

const tunnelsById = () => new Map((S.state?.tunnels || []).map((t) => [t.id, t]));

// ------------------------------------------------------------------ toasts & notifications

function toast(kind, text, sub = "", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const ic = { ok: "check", err: "triangle-alert", warn: "triangle-alert", info: "info" }[kind] || "info";
  el.innerHTML = `${icon(ic)}<div>${esc(text)}${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; }, ms);
  setTimeout(() => el.remove(), ms + 350);
}

const notifyEnabled = () => store.get("tm-notify", "0") === "1" && "Notification" in window && Notification.permission === "granted";

function syncNotifyButton() {
  const on = notifyEnabled();
  const b = $("#btn-notify");
  b.classList.toggle("on", on);
  b.innerHTML = icon(on ? "bell" : "bell-off");
  b.dataset.tip = on ? "Browser notifications: on" : "Browser notifications: off";
}

async function toggleNotifications() {
  if (!("Notification" in window)) return toast("err", "This browser does not support notifications");
  if (notifyEnabled()) {
    store.set("tm-notify", "0");
  } else {
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") {
      toast("warn", "Notifications are blocked", "Allow them for this site in the browser's site settings.");
      store.set("tm-notify", "0");
    } else {
      store.set("tm-notify", "1");
      toast("ok", "Notifications on", "You'll be alerted when a tunnel drops, recovers or gives up.");
    }
  }
  syncNotifyButton();
}

function handleEvents(events) {
  const maxSeq = events.reduce((m, e) => Math.max(m, e.seq), 0);
  if (S.lastSeq === null) { S.lastSeq = maxSeq; return; }
  if (maxSeq < S.lastSeq) S.lastSeq = 0; // manager restarted
  const fresh = events.filter((e) => e.seq > S.lastSeq);
  S.lastSeq = Math.max(S.lastSeq, maxSeq);
  if (!fresh.length) return;
  const title = (e) => ({ down: `${e.name} dropped`, up: `${e.name} is back`, failed: `${e.name} failed` })[e.kind];
  const kind = (e) => ({ down: "warn", up: "ok", failed: "err" })[e.kind];
  const background = document.hidden || !document.hasFocus();
  if (notifyEnabled() && background) {
    if (fresh.length === 1) {
      new Notification(title(fresh[0]), { body: fresh[0].message, tag: fresh[0].tunnel_id, icon: "favicon.svg" });
    } else {
      new Notification(`${fresh.length} tunnel events`, { body: fresh.map(title).join("\n"), icon: "favicon.svg" });
    }
  }
  fresh.slice(-3).forEach((e) => toast(kind(e), title(e), e.message));
}

// ------------------------------------------------------------------ rendering: pieces

function hostLabel(t) {
  const r = S.hosts.get(t.host);
  const jump = t.jump ? ` <span title="ProxyJump">via ${esc(t.jump)}</span>` : "";
  if (r && r.hostname) {
    const user = t.user || r.user;
    const port = t.port || (r.port && r.port !== "22" ? r.port : "");
    const target = `${user ? esc(user) + "@" : ""}${esc(r.hostname)}${port ? ":" + esc(port) : ""}`;
    if (r.hostname === t.host && (!t.user || t.user === r.user)) return `${icon("server")}<b>${target}</b>${jump}`;
    return `${icon("server")}<b>${esc(t.host)}</b><span>→ ${target}</span>${jump}`;
  }
  return `${icon("server")}<b>${t.user ? esc(t.user) + "@" : ""}${esc(t.host)}${t.port ? ":" + t.port : ""}</b>${jump}`;
}

function fwdChip(f, t) {
  const bind = f.bind && !["localhost", "127.0.0.1"].includes(f.bind) ? `${esc(f.bind)}` : "";
  if (f.type === "D") {
    return `<span class="fwd D" title="SOCKS5 proxy on ${esc(f.bind || "localhost")}:${f.listen_port} through ${esc(t.host)}"><span class="t">D</span><span>${bind}:${f.listen_port} <span class="arrow">SOCKS</span></span></span>`;
  }
  if (f.type === "L") {
    if (f.target_socket) {
      const base = f.target_socket.split("/").pop();
      return `<span class="fwd L" title="Local port ${f.listen_port} → unix socket ${esc(f.target_socket)} on ${esc(t.host)}"><span class="t">L</span><span>${bind}:${f.listen_port} <span class="arrow">→</span> <span class="sock">…/</span>${esc(base)}</span></span>`;
    }
    return `<span class="fwd L" title="Local port ${f.listen_port} → ${esc(f.target_host)}:${f.target_port} as seen from ${esc(t.host)}"><span class="t">L</span><span>${bind}:${f.listen_port} <span class="arrow">→</span> ${esc(f.target_host)}:${f.target_port}</span></span>`;
  }
  return `<span class="fwd R" title="Port ${f.listen_port} on ${esc(t.host)} → ${esc(f.target_host)}:${f.target_port} on this machine"><span class="t">R</span><span>${bind}:${f.listen_port} <span class="arrow">→</span> ${esc(f.target_host)}:${f.target_port}</span></span>`;
}

function statusHTML(t) {
  const rt = t.runtime || {};
  const err = rt.last_error;
  switch (rt.status) {
    case "up": {
      const subs = [];
      if (rt.reconnects) subs.push(`<span title="Automatic reconnects">↻ ${rt.reconnects}</span>`);
      if (rt.connect_ms != null) subs.push(`<span title="Time to connect and authenticate">${fmtMs(rt.connect_ms)}</span>`);
      if (rt.target_warning) subs.push(`<span title="${esc(rt.target_warning.message)}" style="color:var(--amber)">${icon("triangle-alert")} target</span>`);
      return `<span class="st up">${icon("check")}up <span data-since="${rt.connected_at}"></span></span><span class="sub">${subs.join("")}</span>`;
    }
    case "starting":
      return `<span class="st starting">${icon("loader-circle", "spin")}connecting…</span><span class="sub">${rt.reconnects ? `attempt ${rt.attempts + 1}` : ""}</span>`;
    case "retrying":
      return `<span class="st retrying">${icon("refresh-cw")}retry in&nbsp;<span data-until="${rt.next_retry_at}"></span></span><span class="sub" title="${esc(err?.message)}">${esc(ERR_SHORT[err?.kind] || "")}</span>`;
    case "offline":
      return `<span class="st offline">${icon("wifi-off")}waiting for network</span><span class="sub"></span>`;
    case "failed":
      return `<span class="st failed" title="${esc(err?.message)}">${icon("triangle-alert")}failed</span><span class="sub" title="${esc(err?.hint)}">${esc(ERR_SHORT[err?.kind] || "")}</span>`;
    default:
      return `<span class="st stopped">stopped</span><span class="sub">used <span data-ago="${t.last_used_at}"></span></span>`;
  }
}

function btn(act, ic, tip, cls = "", disabled = false, pos = "") {
  return `<button class="icon-btn ${cls}" data-act="${act}" data-tip="${esc(tip)}"${pos ? ` data-tip-pos="${pos}"` : ""}${disabled ? " disabled" : ""} aria-label="${esc(tip)}">${icon(ic)}</button>`;
}

function rowHTML(t) {
  const st = t.runtime?.status || "stopped";
  const active = ACTIVE.has(st);
  const powerCls = "power " + (st === "up" ? "on" : st === "failed" ? "bad" : active ? "busy" : "");
  const powerTip = st === "up" ? "Turn off" : active ? "Stop (connecting…)" : st === "failed" ? "Retry" : "Turn on";
  const name = S.renaming === t.id
    ? `<input class="rename-input" value="${esc(t.name)}" maxlength="120" aria-label="New name">`
    : `<span class="name" data-act="details" title="${esc(t.name)}">${esc(t.name)}</span>`;
  return `
    <div class="grip" data-tip="${t.favorite ? "Drag to reorder" : "Drag to Favourites"}">${icon("grip-vertical")}</div>
    <div class="dot ${st}"></div>
    <div class="main">
      <div class="title-line">${name}<span class="host">${hostLabel(t)}</span></div>
      <div class="fwds">${t.forwards.map((f) => fwdChip(f, t)).join("")}</div>
    </div>
    <div class="status">${statusHTML(t)}</div>
    <div class="actions">
      ${btn("power", "power", powerTip, powerCls)}
      ${btn("keep", "heart-pulse", t.keep_alive ? "Keep-alive on: reconnects automatically" : "Keep-alive off", "keep" + (t.keep_alive ? " on" : ""))}
      ${btn("copy", "copy", "Copy ssh command")}
      ${btn("details", "scroll-text", S.open.has(t.id) ? "Hide details & log" : "Details & log", S.open.has(t.id) ? "on" : "")}
      <span class="sep"></span>
      ${btn("rename", "type", "Rename")}
      ${btn("edit", "pencil", "Edit")}
      ${btn("star", "star", t.favorite ? "Remove from favourites" : "Add to favourites", "star" + (t.favorite ? " on" : ""))}
      ${btn("delete", "trash-2", "Delete", "del")}
      <span class="sep"></span>
      ${btn("open", "external-link", t.open ? `Open ${t.open}` : "Nothing to open (no -L forward)", "open" + (st === "up" ? " ready" : ""), !t.open, "top-end")}
    </div>`;
}

function detailsHTML(t) {
  const rt = t.runtime || {};
  const r = S.hosts.get(t.host) || {};
  const callouts = [];
  if (rt.last_error && rt.status !== "up" && rt.status !== "stopped") {
    callouts.push(`<div class="callout err">${icon("triangle-alert")}<div><b>${esc(rt.last_error.message)}</b><p>${esc(rt.last_error.hint)}</p></div></div>`);
  } else if (rt.last_error && rt.status === "stopped" && rt.last_error.permanent) {
    callouts.push(`<div class="callout err">${icon("triangle-alert")}<div><b>Last attempt: ${esc(rt.last_error.message)}</b><p>${esc(rt.last_error.hint)}</p></div></div>`);
  }
  if (rt.target_warning && rt.status === "up") {
    callouts.push(`<div class="callout warn">${icon("triangle-alert")}<div><b>${esc(rt.target_warning.message)}</b><p>${esc(rt.target_warning.hint)}</p></div></div>`);
  }
  if (t.warning) callouts.push(`<div class="callout warn">${icon("triangle-alert")}<div><b>${esc(t.warning)}</b></div></div>`);
  const kv = (k, v) => `<div><dt>${k}</dt><dd>${v}</dd></div>`;
  const resolved = r.hostname
    ? `${esc(t.user || r.user || "")}${(t.user || r.user) ? "@" : ""}${esc(r.hostname)}:${esc(t.port || r.port || 22)}`
    : `${esc(t.host)}`;
  return `
    <div class="cmd"><code>${esc(t.command)}</code>${btn("copy", "copy", "Copy command")}</div>
    ${callouts.join("")}
    <dl class="kv">
      ${kv("Status", esc(rt.status || "stopped"))}
      ${kv("Connects to", resolved + (t.jump ? ` via ${esc(t.jump)}` : r.proxyjump ? ` via ${esc(r.proxyjump)}` : ""))}
      ${kv("Process", rt.pid ? `pid ${rt.pid}${rt.adopted ? " (re-attached)" : ""}` : "—")}
      ${kv("Connected", rt.connected_at ? fmtDate(rt.connected_at) : "—")}
      ${kv("Connect time", rt.connect_ms != null ? fmtMs(rt.connect_ms) : "—")}
      ${kv("Reconnects", rt.reconnects ?? 0)}
      ${kv("Last session", rt.last_transfer ? `↑ ${fmtBytes(rt.last_transfer.sent)} · ↓ ${fmtBytes(rt.last_transfer.received)}` : "—")}
      ${kv("Keep-alive", t.keep_alive ? "on" : "off")}
      ${kv("Identity", esc(t.identity_file || "from ssh config / agent"))}
      ${kv("Used", `${t.use_count}× · last ${fmtAgo(t.last_used_at)}`)}
      ${kv("Created", `${fmtDate(t.created_at)}${t.source && t.source !== "manual" ? ` · from ${esc(t.source.replace("_", " "))}` : ""}`)}
      ${kv("Open URL", t.open ? esc(t.open) : "—")}
    </dl>`;
}

// ------------------------------------------------------------------ rendering: lists

const rowCache = new Map(); // id -> {el, sig, dsig}

function matches(t, q) {
  if (!q) return true;
  const hay = [t.name, t.host, t.user, t.jump, t.command, S.hosts.get(t.host)?.hostname,
    ...t.forwards.flatMap((f) => [f.listen_port, f.target_host, f.target_port, f.target_socket, `-${f.type}`])]
    .filter(Boolean).join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

function sortHistory(list) {
  const by = S.sort;
  const cmp = {
    recent: (a, b) => b.last_used_at - a.last_used_at,
    name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }),
    host: (a, b) => a.host.localeCompare(b.host) || a.name.localeCompare(b.name, undefined, { numeric: true }),
  }[by];
  return [...list].sort(cmp);
}

function historyOrder(hist) {
  const ids = new Set(hist.map((t) => t.id));
  if (!S.histOrder.length) {
    S.histOrder = sortHistory(hist).map((t) => t.id);
    return S.histOrder;
  }
  const known = new Set(S.histOrder);
  const fresh = sortHistory(hist.filter((t) => !known.has(t.id))).map((t) => t.id);
  S.histOrder = [...fresh, ...S.histOrder.filter((id) => ids.has(id))];
  return S.histOrder;
}

function ensureRow(t) {
  let c = rowCache.get(t.id);
  if (!c) {
    const el = document.createElement("div");
    el.className = "item";
    el.dataset.id = t.id;
    el.innerHTML = `<div class="row"></div><div class="details" hidden><div class="info"></div><div class="log-head">${icon("scroll-text")} ssh output <span class="path"></span></div><pre class="log"></pre></div>`;
    c = { el, sig: "", dsig: "" };
    rowCache.set(t.id, c);
  }
  const sig = JSON.stringify([t.name, t.host, t.user, t.port, t.jump, t.forwards, t.keep_alive, t.favorite, t.open,
    t.runtime, S.renaming === t.id, S.open.has(t.id), S.hosts.get(t.host)?.hostname, t.last_used_at]);
  // never re-render a row while its name is being edited
  const editingName = S.renaming === t.id && c.el.querySelector(".rename-input");
  if (sig !== c.sig && !editingName) {
    c.el.querySelector(".row").innerHTML = rowHTML(t);
    c.sig = sig;
  }
  const isOpen = S.open.has(t.id);
  c.el.classList.toggle("open", isOpen);
  const det = c.el.querySelector(".details");
  det.hidden = !isOpen;
  if (isOpen) {
    const dsig = sig + t.use_count;
    if (dsig !== c.dsig) {
      det.querySelector(".info").innerHTML = detailsHTML(t);
      c.dsig = dsig;
    }
  }
  return c.el;
}

function placeRows(container, ids, byId) {
  let prev = null;
  for (const id of ids) {
    const el = ensureRow(byId.get(id));
    const want = prev ? prev.nextSibling : container.firstChild;
    if (el !== want) container.insertBefore(el, want);
    prev = el;
  }
  // drop rows that no longer belong here
  const keep = new Set(ids);
  [...container.children].forEach((el) => { if (!keep.has(el.dataset.id)) el.remove(); });
}

function render() {
  const st = S.state;
  if (!st) return;
  const byId = tunnelsById();
  const all = st.tunnels;
  const favs = all.filter((t) => t.favorite).sort((a, b) => a.order - b.order);
  const hist = all.filter((t) => !t.favorite);
  const histIds = historyOrder(hist);
  const q = S.query.trim();
  const favShown = favs.filter((t) => matches(t, q)).map((t) => t.id);
  const histShown = histIds.filter((id) => matches(byId.get(id), q));

  if (!S.dragging) {
    placeRows($("#fav-list"), favShown, byId);
    placeRows($("#hist-list"), histShown, byId);
  } else {
    [...favShown, ...histShown].forEach((id) => ensureRow(byId.get(id)));
  }
  for (const id of rowCache.keys()) if (!byId.has(id)) { rowCache.get(id).el.remove(); rowCache.delete(id); S.open.delete(id); }

  $("#fav-count").textContent = q ? `${favShown.length}/${favs.length}` : favs.length;
  $("#hist-count").textContent = q ? `${histShown.length}/${hist.length}` : hist.length;
  $("#fav-empty").hidden = favShown.length > 0;
  $("#fav-empty").innerHTML = q && favs.length
    ? "No favourites match your search."
    : `No favourites yet. Click ${icon("star")} on a tunnel, or drag one here.`;
  $("#hist-empty").hidden = histShown.length > 0;
  if (q) $("#hist-empty").textContent = "No tunnels in history match your search.";
  favSortable && favSortable.option("disabled", !!q);
  histSortable && histSortable.option("disabled", !!q);

  renderPills(all);
  $("#offline-banner").hidden = st.online;
  $("#footer-info").innerHTML = `Tunnel Manager ${esc(st.version)}${st.demo ? " · <b>demo mode</b>" : ""} · key-only SSH · data in <code>${esc(st.home || "~/.cache/tunnel-manager")}</code>`;
  updateTimes();
}

function renderPills(all) {
  const count = (s) => all.filter((t) => t.runtime?.status === s).length;
  const up = count("up"), conn = count("starting") + count("retrying"), off = count("offline"), bad = count("failed");
  const pills = [];
  if (up) pills.push(`<span class="pill"><span class="dot up"></span>${up} up</span>`);
  if (conn) pills.push(`<span class="pill"><span class="dot starting"></span>${conn} connecting</span>`);
  if (off) pills.push(`<span class="pill"><span class="dot offline"></span>${off} waiting</span>`);
  if (bad) pills.push(`<span class="pill"><span class="dot failed"></span>${bad} failed</span>`);
  if (!pills.length) pills.push(`<span class="pill"><span class="dot"></span>no tunnels running</span>`);
  $("#pills").innerHTML = pills.join("");
  document.title = up ? `(${up}) Tunnel Manager` : "Tunnel Manager";
}

function updateTimes() {
  const t = now();
  $$("[data-since]").forEach((el) => { el.textContent = fmtDur(t - Number(el.dataset.since)); });
  $$("[data-until]").forEach((el) => { el.textContent = `${Math.max(0, Math.ceil(Number(el.dataset.until) - t))}s`; });
  $$("[data-ago]").forEach((el) => { el.textContent = fmtAgo(Number(el.dataset.ago)); });
}

// ------------------------------------------------------------------ live updates

function applyState(st) {
  S.state = st;
  handleEvents(st.events || []);
  // report failures of starts the user just clicked
  for (const [id, deadline] of S.watch) {
    const t = st.tunnels.find((x) => x.id === id);
    const status = t?.runtime?.status;
    if (!t || now() > deadline) { S.watch.delete(id); continue; }
    if (status === "up") { S.watch.delete(id); continue; }
    if ((status === "failed" || status === "retrying") && t.runtime.last_error) {
      S.watch.delete(id);
      toast(status === "failed" ? "err" : "warn", `${t.name}: ${t.runtime.last_error.message}`, t.runtime.last_error.hint, 8000);
    }
  }
  render();
}

function connect() {
  const es = new EventSource("/api/events");
  let bannerTimer = null;
  es.onmessage = (ev) => {
    clearTimeout(bannerTimer);
    if (!S.connected) {
      S.connected = true;
      $("#conn-banner").hidden = true;
    }
    applyState(JSON.parse(ev.data));
  };
  es.onerror = () => {
    S.connected = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => { if (!S.connected) $("#conn-banner").hidden = false; }, 2500);
  };
}

async function loadHosts() {
  try {
    S.hostList = await api("GET", "/api/hosts");
    S.hosts = new Map(S.hostList.map((h) => [h.alias, h]));
    render();
  } catch (e) {
    console.warn("could not load hosts", e);
  }
}

// ------------------------------------------------------------------ row actions

async function act(name, id, el) {
  const t = tunnelsById().get(id);
  if (!t) return;
  try {
    switch (name) {
      case "power": {
        const st = t.runtime?.status;
        if (ACTIVE.has(st)) {
          await api("POST", `/api/tunnels/${id}/stop`);
        } else {
          S.watch.set(id, now() + 20);
          await api("POST", `/api/tunnels/${id}/start`);
        }
        break;
      }
      case "keep":
        await api("PUT", `/api/tunnels/${id}`, { keep_alive: !t.keep_alive });
        toast("ok", t.keep_alive ? `Keep-alive off for ${t.name}` : `Keep-alive on for ${t.name}`,
          t.keep_alive ? "" : "It will reconnect automatically after network loss or sleep.", 2600);
        break;
      case "open":
        if (/^https?:/i.test(t.open)) window.open(t.open, "_blank", "noopener");
        else await api("POST", `/api/tunnels/${id}/open`);
        if (t.runtime?.status !== "up") toast("warn", `${t.name} is not running`, "Turn it on first if the page does not load.", 3500);
        break;
      case "copy":
        await navigator.clipboard.writeText(t.command);
        toast("ok", "Copied ssh command", t.command, 2600);
        break;
      case "details":
        if (S.open.has(id)) { S.open.delete(id); stopLog(id); } else { S.open.add(id); startLog(id); }
        render();
        break;
      case "rename":
        startRename(id);
        break;
      case "edit":
        openEditor(t);
        break;
      case "star":
        await api("PUT", `/api/tunnels/${id}`, { favorite: !t.favorite });
        break;
      case "delete":
        confirmDelete(t);
        break;
    }
  } catch (e) {
    toast("err", e.message);
  }
}

function startRename(id) {
  S.renaming = id;
  render();
  const input = $(`.item[data-id="${id}"] .rename-input`);
  if (!input) return;
  input.focus();
  input.select();
  const finish = async (save) => {
    if (S.renaming !== id) return;
    S.renaming = null;
    const value = input.value.trim();
    const t = tunnelsById().get(id);
    rowCache.get(id).sig = "";
    if (save && t && value !== t.name) {
      try {
        await api("PUT", `/api/tunnels/${id}`, { name: value });
        toast("ok", value ? `Renamed to “${value}”` : "Name reset to automatic", "", 2200);
      } catch (e) { toast("err", e.message); }
    }
    render();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// ------------------------------------------------------------------ logs

const logTimers = new Map();

function colorLog(text) {
  return text.replace(/\r/g, "").split("\n").map((line) => {
    const l = esc(line);
    if (line.startsWith("=== ")) return `<span class="hdr">${l}</span>`;
    if (/Authenticated to|Transferred:/.test(line)) return `<span class="ok">${l}</span>`;
    if (/denied|failed|refused|error|timed out|not known|closed by|reset by|Address already in use|unreachable|Could not|Bad /i.test(line)) return `<span class="err">${l}</span>`;
    return l;
  }).join("\n");
}

async function pollLog(id) {
  const det = $(`.item[data-id="${id}"] .details`);
  if (!det) return;
  const pre = det.querySelector(".log");
  const st = S.logs.get(id) || { offset: -1 };
  try {
    const r = await api("GET", `/api/tunnels/${id}/logs?offset=${st.offset}`);
    det.querySelector(".path").textContent = r.path;
    if (st.offset < 0) pre.innerHTML = r.text ? colorLog(r.text.replace(/^\n+/, "")) : `<span style="opacity:.6">No output yet — start the tunnel to see ssh's messages here.</span>`;
    else if (r.text) {
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      if (pre.querySelector("span[style]")) pre.innerHTML = "";
      pre.insertAdjacentHTML("beforeend", colorLog(r.text));
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    if (st.offset < 0) pre.scrollTop = pre.scrollHeight;
    S.logs.set(id, { offset: r.offset });
  } catch {}
}

function startLog(id) {
  S.logs.delete(id);
  setTimeout(() => pollLog(id), 0);
  logTimers.set(id, setInterval(() => pollLog(id), 1500));
}
function stopLog(id) {
  clearInterval(logTimers.get(id));
  logTimers.delete(id);
  S.logs.delete(id);
}

// ------------------------------------------------------------------ modal plumbing

const modal = $("#modal");
function showModal(html, { onClose } = {}) {
  modal.innerHTML = html;
  hydrateIcons(modal);
  modal.onclose = () => { onClose && onClose(); modal.innerHTML = ""; };
  modal.showModal();
}
modal.addEventListener("click", (e) => {
  if (e.target === modal) modal.close(); // backdrop
  if (e.target.closest("[data-close]")) modal.close();
});

function confirmDelete(t) {
  const active = ACTIVE.has(t.runtime?.status);
  showModal(`
    <div class="wrap" style="max-width:520px">
      <div class="m-head"><h3>Delete tunnel?</h3><button class="icon-btn" data-close data-tip="Close"><span data-icon="x"></span></button></div>
      <div class="m-body">
        <p style="margin:0">“<b>${esc(t.name)}</b>” will be removed from ${t.favorite ? "your favourites" : "history"}${active ? " and <b>stopped</b>" : ""}. A later history import will not bring it back.</p>
        <div class="cmd"><code>${esc(t.command)}</code></div>
      </div>
      <div class="m-foot"><span class="spacer"></span>
        <button class="btn" data-close>Cancel</button>
        <button class="btn danger" id="confirm-delete"><span data-icon="trash-2"></span>Delete</button>
      </div>
    </div>`);
  $("#confirm-delete").focus();
  $("#confirm-delete").onclick = async () => {
    try {
      await api("DELETE", `/api/tunnels/${t.id}`);
      toast("ok", `Deleted “${t.name}”`, "", 2500);
    } catch (e) { toast("err", e.message); }
    modal.close();
  };
}

// ------------------------------------------------------------------ editor

function blankForward(type = "L") {
  return type === "R"
    ? { type, bind: "", listen_port: "", target_host: "127.0.0.1", target_port: "", target_socket: "", mode: "tcp" }
    : { type, bind: "", listen_port: "", target_host: type === "L" ? "localhost" : "", target_port: "", target_socket: "", mode: "tcp" };
}

const FW_DESC = {
  L: "Local forward: a port on this machine reaches a service on (or behind) the server",
  R: "Remote forward: a port on the server reaches a service on this machine",
  D: "Dynamic forward: a SOCKS5 proxy on this machine that tunnels through the server",
};

function forwardRowHTML(f, i, total) {
  const seg = ["L", "R", "D"].map((v) => `<button type="button" data-v="${v}" class="${f.type === v ? "active" : ""}">-${v}</button>`).join("");
  const mode = f.type === "L" ? `
      <span class="seg small" data-mode>
        <button type="button" data-m="tcp" class="${f.mode !== "socket" ? "active" : ""}">host:port</button>
        <button type="button" data-m="socket" class="${f.mode === "socket" ? "active" : ""}">unix socket</button>
      </span>` : "";
  const port = (field, ph) => `<input class="input mono w-port" data-f="${field}" inputmode="numeric" placeholder="${ph}" value="${esc(f[field])}">`;
  const bind = (title) => `<input class="input mono w-bind" data-f="bind" placeholder="localhost" value="${esc(f.bind)}" title="${title}">`;
  let fields;
  if (f.type === "L") {
    const tgt = f.mode === "socket"
      ? `<input class="input mono w-sock" data-f="target_socket" placeholder="/home/user/app.sock" value="${esc(f.target_socket)}">`
      : `<input class="input mono w-host" data-f="target_host" placeholder="localhost" value="${esc(f.target_host)}"><span>:</span>${port("target_port", "port")}`;
    fields = `<span class="lbl">local</span>${bind("Bind address on this machine (optional)")}<span>:</span>${port("listen_port", "8080")}
      <span class="arrow">→</span><span class="lbl">server side</span>${tgt}`;
  } else if (f.type === "R") {
    fields = `<span class="lbl">server</span>${bind("Bind address on the server (optional)")}<span>:</span>${port("listen_port", "9000")}
      <span class="arrow">→</span><span class="lbl">this machine</span>
      <input class="input mono w-host" data-f="target_host" placeholder="127.0.0.1" value="${esc(f.target_host)}"><span>:</span>${port("target_port", "port")}`;
  } else {
    fields = `<span class="lbl">SOCKS on</span>${bind("Bind address (optional)")}<span>:</span>${port("listen_port", "1080")}`;
  }
  return `
    <div class="fw-row" data-i="${i}">
      <div class="fw-top">
        <span class="seg" data-type>${seg}</span>${mode}
        <span class="fw-desc">${FW_DESC[f.type]}</span>
        <button type="button" class="icon-btn" data-rm data-tip="Remove forward" data-tip-pos="top-end" ${total < 2 ? "disabled" : ""}>${icon("x")}</button>
      </div>
      <div class="fw-fields">${fields}</div>
    </div>`;
}

function draftFromTunnel(t) {
  if (!t) {
    return { id: null, name: "", host: "", user: "", port: "", identity_file: "", jump: "",
      forwards: [blankForward("L")], keep_alive: true, favorite: false, open_url: "", start: true };
  }
  return {
    id: t.id, name: t.name, host: t.host, user: t.user || "", port: t.port || "", identity_file: t.identity_file || "",
    jump: t.jump || "", keep_alive: t.keep_alive, favorite: t.favorite, open_url: t.open_url || "", start: false,
    forwards: t.forwards.map((f) => ({
      type: f.type, bind: f.bind || "", listen_port: f.listen_port, target_host: f.target_host || "",
      target_port: f.target_port || "", target_socket: f.target_socket || "", mode: f.target_socket ? "socket" : "tcp",
    })),
  };
}

function draftPayload(d) {
  const num = (v) => (v === "" || v == null ? null : Number(v));
  return {
    name: d.name.trim(), host: d.host.trim(), user: d.user.trim() || null, port: num(d.port),
    identity_file: d.identity_file || null, jump: d.jump.trim() || null, keep_alive: d.keep_alive,
    favorite: d.favorite, open_url: d.open_url.trim() || null,
    forwards: d.forwards.map((f) => {
      const out = { type: f.type, bind: f.bind.trim() || null, listen_port: num(f.listen_port) };
      if (f.type === "L" && f.mode === "socket") out.target_socket = f.target_socket.trim();
      else if (f.type !== "D") { out.target_host = (f.target_host || "").trim() || (f.type === "R" ? "127.0.0.1" : "localhost"); out.target_port = num(f.target_port); }
      return out;
    }),
  };
}

async function openEditor(t) {
  const d = draftFromTunnel(t);
  const editing = !!t;
  if (!S.keys) S.keys = await api("GET", "/api/keys").catch(() => []);
  const hostOptions = S.hostList.map((h) => `<option value="${esc(h.alias)}">${esc([h.user, h.hostname].filter(Boolean).join("@"))}</option>`).join("");
  const keyOptions = [`<option value="">Default (ssh config / agent)</option>`]
    .concat(S.keys.map((k) => `<option value="${esc(k.path)}" ${d.identity_file === k.path ? "selected" : ""}>${esc(k.path)}${k.type ? ` (${esc(k.type.replace("ssh-", ""))})` : ""}</option>`))
    .concat(d.identity_file && !S.keys.some((k) => k.path === d.identity_file) ? [`<option value="${esc(d.identity_file)}" selected>${esc(d.identity_file)}</option>`] : [])
    .join("");

  showModal(`
    <form method="dialog" id="editor" autocomplete="off">
      <div class="m-head"><h3>${editing ? "Edit tunnel" : "New tunnel"}</h3><button type="button" class="icon-btn" data-close data-tip="Close (Esc)"><span data-icon="x"></span></button></div>
      <div class="m-body">
        <div class="m-section">
          <h4>Connection</h4>
          <div class="grid2">
            <label class="field"><span>Host</span>
              <input class="input mono" name="host" list="dl-hosts" placeholder="alias from ~/.ssh/config or hostname" value="${esc(d.host)}" required spellcheck="false">
              <datalist id="dl-hosts">${hostOptions}</datalist>
            </label>
            <label class="field"><span>User</span><input class="input mono" name="user" placeholder="from config" value="${esc(d.user)}" spellcheck="false"></label>
            <label class="field"><span>Port</span><input class="input mono" name="port" inputmode="numeric" placeholder="22" value="${esc(d.port)}"></label>
          </div>
          <div class="resolved" id="resolved"></div>
          <details class="adv" ${d.identity_file || d.jump ? "open" : ""}>
            <summary><span data-icon="chevron-down"></span>Identity file &amp; jump host</summary>
            <div class="grid2b">
              <label class="field"><span>Identity file</span><select class="input" name="identity_file">${keyOptions}</select></label>
              <label class="field"><span>Jump host (ProxyJump)</span><input class="input mono" name="jump" placeholder="e.g. bastion or user@bastion:22" value="${esc(d.jump)}" spellcheck="false"></label>
            </div>
          </details>
          <div class="resolved" style="margin-top:8px">🔒 Key-based authentication only — password and keyboard-interactive logins are disabled.</div>
        </div>
        <div class="m-section">
          <h4>Forwards</h4>
          <div class="fw-rows" id="fw-rows"></div>
          <button type="button" class="btn small ghost" id="fw-add" style="margin-top:8px"><span data-icon="plus"></span>Add forward</button>
        </div>
        <div class="m-section">
          <h4>Options</h4>
          <div class="grid2b">
            <label class="field"><span>Name</span><input class="input" name="name" placeholder="automatic" value="${esc(d.name)}" maxlength="120"></label>
            <label class="field"><span>Open URL</span><input class="input mono" name="open_url" placeholder="automatic" value="${esc(d.open_url)}" spellcheck="false"></label>
          </div>
          <div class="checks" style="margin-top:12px">
            <label class="check"><input type="checkbox" name="keep_alive" ${d.keep_alive ? "checked" : ""}>Keep alive (auto-reconnect)</label>
            <label class="check"><input type="checkbox" name="favorite" ${d.favorite ? "checked" : ""}>Favourite</label>
            ${editing ? "" : `<label class="check"><input type="checkbox" name="start" ${d.start ? "checked" : ""}>Start now</label>`}
          </div>
        </div>
        <div class="preview" id="preview"></div>
      </div>
      <div class="m-foot">
        <span class="spacer"></span>
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="submit" class="btn primary" id="save">${editing ? "Save" : "Create tunnel"}</button>
      </div>
    </form>`);

  const form = $("#editor");
  const rows = $("#fw-rows");

  const renderForwards = () => {
    rows.innerHTML = d.forwards.map((f, i) => forwardRowHTML(f, i, d.forwards.length)).join("");
  };
  renderForwards();

  const resolve = debounce(async () => {
    const el = $("#resolved");
    if (!el) return;
    const host = d.host.trim();
    if (!host) { el.textContent = ""; return; }
    const known = S.hosts.get(host);
    const qs = new URLSearchParams({ host });
    if (d.user.trim()) qs.set("user", d.user.trim());
    if (String(d.port).trim()) qs.set("port", String(d.port).trim());
    try {
      const r = await api("GET", `/api/resolve?${qs}`);
      if (r.error) { el.className = "resolved err"; el.textContent = r.error; return; }
      el.className = "resolved";
      const via = d.jump.trim() || r.proxyjump;
      const keys = (r.identityfile || []).map((k) => k.replace(/^.*\//, "")).join(", ");
      el.innerHTML = `${icon("arrow-right")} ${known ? "" : "<i>not in ~/.ssh/config</i> · "}connects to <b class="mono">${esc(r.user)}@${esc(r.hostname)}:${esc(r.port)}</b>${via ? ` via ${esc(via)}` : ""}${keys ? ` · keys: ${esc(keys)}` : ""}`;
      form.user.placeholder = r.user || "from config";
      form.port.placeholder = r.port || "22";
    } catch { el.textContent = ""; }
  }, 250);

  const preview = debounce(async () => {
    const el = $("#preview");
    if (!el) return;
    try {
      const body = draftPayload(d);
      if (editing) body.id = d.id;
      const r = await api("POST", "/api/preview", body);
      const save = $("#save");
      if (!r.ok) {
        el.innerHTML = `<div class="callout err">${icon("triangle-alert")}<div>${esc(r.error)}</div></div>`;
        save.disabled = true;
        return;
      }
      save.disabled = false;
      form.name.placeholder = r.name;
      form.open_url.placeholder = r.open || "nothing to open";
      el.innerHTML = `<div class="cmd"><code>${esc(r.command)}</code></div>` +
        (r.warnings.length ? `<div class="warn-list">${r.warnings.map((w) => `<div class="callout warn">${icon("triangle-alert")}<div>${esc(w)}</div></div>`).join("")}</div>` : "");
    } catch (e) {
      el.innerHTML = `<div class="callout err">${icon("triangle-alert")}<div>${esc(e.message)}</div></div>`;
    }
  }, 200);

  const sync = () => { resolve(); preview(); };

  form.addEventListener("input", (e) => {
    const el = e.target;
    const row = el.closest(".fw-row");
    if (row && el.dataset.f) {
      const f = d.forwards[Number(row.dataset.i)];
      const old = f[el.dataset.f];
      f[el.dataset.f] = el.value;
      // mirror the local port into the target port while they match
      if (el.dataset.f === "listen_port" && f.type === "L" && f.mode !== "socket" && (f.target_port === "" || f.target_port === old)) {
        f.target_port = el.value;
        const tp = row.querySelector('[data-f="target_port"]');
        if (tp) tp.value = el.value;
      }
    } else if (el.name) {
      d[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
    if (el.name === "host" || el.name === "user" || el.name === "port" || el.name === "jump") resolve();
    preview();
  });
  form.addEventListener("change", (e) => {
    if (e.target.name === "identity_file") { d.identity_file = e.target.value; preview(); }
  });
  rows.addEventListener("click", (e) => {
    const row = e.target.closest(".fw-row");
    if (!row) return;
    const i = Number(row.dataset.i);
    const typeBtn = e.target.closest("[data-type] button");
    const modeBtn = e.target.closest("[data-mode] button");
    if (typeBtn) {
      const keep = d.forwards[i];
      const nf = blankForward(typeBtn.dataset.v);
      nf.listen_port = keep.listen_port;
      if (typeBtn.dataset.v !== "D") nf.target_port = keep.target_port || keep.listen_port;
      d.forwards[i] = nf;
      renderForwards(); preview();
    } else if (modeBtn) {
      d.forwards[i].mode = modeBtn.dataset.m;
      renderForwards(); preview();
      rows.children[i]?.querySelector(modeBtn.dataset.m === "socket" ? '[data-f="target_socket"]' : '[data-f="target_host"]')?.focus();
    } else if (e.target.closest("[data-rm]")) {
      d.forwards.splice(i, 1);
      renderForwards(); preview();
    }
  });
  $("#fw-add").onclick = () => {
    d.forwards.push(blankForward(d.forwards[d.forwards.length - 1]?.type === "R" ? "R" : "L"));
    renderForwards(); preview();
    rows.lastElementChild?.querySelector('[data-f="listen_port"]')?.focus();
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = draftPayload(d);
    try {
      if (editing) {
        const r = await api("PUT", `/api/tunnels/${d.id}`, body);
        toast("ok", `Saved “${r.name}”`, r.restarted ? "The tunnel is restarting with the new settings." : "", 2800);
      } else {
        body.start = d.start;
        const r = await api("POST", "/api/tunnels", body);
        if (d.start) S.watch.set(r.id, now() + 20);
        toast(r.created ? "ok" : "info", r.created ? `Created “${r.name}”` : `“${r.name}” already exists`,
          r.created ? (d.start ? "Connecting…" : "") : "The existing entry was reused.", 3000);
        flashRow(r.id);
      }
      modal.close();
    } catch (err) {
      toast("err", err.message);
    }
  });

  setTimeout(() => (editing ? form.name : form.host).focus(), 30);
  sync();
}

function flashRow(id) {
  setTimeout(() => {
    const el = $(`.item[data-id="${id}"]`);
    if (!el) return;
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }, 250);
}

// ------------------------------------------------------------------ settings

async function openSettings() {
  const st = S.state;
  let svc = {};
  try { svc = await api("GET", "/api/service"); } catch {}
  const s = st.settings;
  const svcText = !svc.supported ? "Not supported on this system."
    : svc.loaded ? `Installed and running${svc.pid ? ` (pid ${svc.pid})` : ""}.`
    : svc.installed ? "Installed, not running." : "Not installed.";
  showModal(`
    <div class="wrap">
      <div class="m-head"><h3>Settings</h3><button class="icon-btn" data-close data-tip="Close"><span data-icon="x"></span></button></div>
      <div class="m-body">
        <div class="m-section">
          <h4>Notifications</h4>
          <div class="switch-row">
            <div class="txt"><b>Browser notifications</b><small>Alert when a tunnel drops, recovers or gives up, while this tab is open (also in the background).</small></div>
            <label class="switch"><input type="checkbox" id="set-notify" ${notifyEnabled() ? "checked" : ""}><span></span></label>
          </div>
        </div>
        <div class="m-section">
          <h4>Start at login</h4>
          <div class="switch-row">
            <div class="txt"><b>Run the manager in the background</b><small>${esc(svcText)} ${svc.kind === "launchd" ? "Uses a launchd LaunchAgent." : svc.kind === "systemd" ? "Uses a systemd user unit." : ""} Keep-alive tunnels are restored after a reboot.</small></div>
            ${svc.supported ? `<button class="btn small" id="svc-btn">${svc.installed ? "Uninstall" : "Install"}</button>` : ""}
          </div>
        </div>
        <div class="m-section">
          <h4>SSH</h4>
          <div class="switch-row">
            <div class="txt"><b>Accept new host keys automatically</b><small>Uses <code>StrictHostKeyChecking=accept-new</code>: unknown hosts are trusted on first use, changed keys are still rejected.</small></div>
            <label class="switch"><input type="checkbox" id="set-acceptnew" ${s.accept_new_host_keys ? "checked" : ""}><span></span></label>
          </div>
          <div class="switch-row">
            <div class="txt"><b>Dead-connection detection</b><small>A tunnel is considered dead after <i>interval × count</i> seconds without an answer from the server.</small></div>
            <label class="field"><span>Interval (s)</span><input class="input num" type="number" min="1" max="600" id="set-sai" value="${s.server_alive_interval}"></label>
            <label class="field"><span>Count</span><input class="input num" type="number" min="1" max="20" id="set-sac" value="${s.server_alive_count_max}"></label>
            <label class="field"><span>Connect timeout</span><input class="input num" type="number" min="1" max="120" id="set-ct" value="${s.connect_timeout}"></label>
          </div>
        </div>
        <div class="m-section">
          <h4>History</h4>
          <div class="switch-row">
            <div class="txt"><b>Import from shell history</b><small>Scans <code>~/.zsh_history</code> and <code>~/.bash_history</code> for ssh -L/-R/-D commands. Duplicates are merged; tunnels you deleted stay deleted.</small></div>
            <button class="btn small" id="import-btn"><span data-icon="history"></span>Import now</button>
          </div>
        </div>
        <div class="m-section">
          <h4>Manager</h4>
          <div class="switch-row">
            <div class="txt"><b>Quit the manager</b><small>Stops all tunnels and the background process. Data stays in <code>${esc(st.home || "")}</code>.</small></div>
            <button class="btn small" id="quit-btn">Quit</button>
          </div>
        </div>
      </div>
      <div class="m-foot"><span class="hint">Tunnel Manager ${esc(st.version)}</span><span class="spacer"></span><button class="btn" data-close>Done</button></div>
    </div>`);

  $("#set-notify").onchange = async (e) => {
    await toggleNotifications();
    e.target.checked = notifyEnabled();
  };
  $("#set-acceptnew").onchange = (e) => saveSetting({ accept_new_host_keys: e.target.checked });
  const nums = { "set-sai": "server_alive_interval", "set-sac": "server_alive_count_max", "set-ct": "connect_timeout" };
  Object.entries(nums).forEach(([id, key]) => {
    $(`#${id}`).onchange = (e) => saveSetting({ [key]: Number(e.target.value) });
  });
  $("#import-btn").onclick = async (e) => {
    e.currentTarget.disabled = true;
    try {
      const r = await api("POST", "/api/import-history");
      toast("ok", `Found ${r.found} tunnels in shell history`, `${r.added} new · ${r.updated} refreshed · ${r.ignored} previously deleted`);
    } catch (err) { toast("err", err.message); }
    e.currentTarget && (e.currentTarget.disabled = false);
  };
  const svcBtn = $("#svc-btn");
  if (svcBtn) svcBtn.onclick = async () => {
    svcBtn.disabled = true;
    try {
      if (svc.installed) {
        await api("POST", "/api/service/uninstall");
        toast("ok", "Removed the login item", "The manager keeps running until you quit it.");
      } else {
        const r = await api("POST", "/api/service/install");
        toast("ok", "Installed: the manager now starts at login", r.handover ? "Handing over to the background service — this page reconnects in a moment." : "");
      }
      modal.close();
    } catch (err) { toast("err", err.message); svcBtn.disabled = false; }
  };
  $("#quit-btn").onclick = async () => {
    if (!confirm("Quit the manager and stop all tunnels?")) return;
    try { await api("POST", "/api/shutdown", { stop_tunnels: true }); } catch {}
    modal.close();
    toast("info", "Manager stopped", "Run `tunnel-manager` in a terminal to start it again.", 10000);
  };
}

async function saveSetting(changes) {
  try {
    await api("PUT", "/api/settings", changes);
    toast("ok", "Saved", "Applies to tunnels started from now on.", 1800);
  } catch (e) { toast("err", e.message); }
}

// ------------------------------------------------------------------ theme

const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const b = $("#btn-theme");
  b.innerHTML = icon({ auto: "monitor", light: "sun", dark: "moon" }[t]);
  b.dataset.tip = `Theme: ${t}`;
}

// ------------------------------------------------------------------ drag & drop

let favSortable = null, histSortable = null;

function setupSortable() {
  if (!window.Sortable) return;
  const favList = $("#fav-list"), histList = $("#hist-list");
  const common = {
    handle: ".grip", animation: 160, ghostClass: "sortable-ghost", chosenClass: "sortable-chosen",
    onStart: () => { S.dragging = true; favList.classList.add("drop-target"); },
    onEnd: () => { S.dragging = false; favList.classList.remove("drop-target"); },
  };
  favSortable = Sortable.create(favList, {
    ...common,
    group: { name: "tunnels", pull: false, put: true },
    onEnd: async (evt) => {
      common.onEnd();
      if (evt.from === favList && evt.to === favList && evt.oldIndex !== evt.newIndex) await saveFavOrder();
      render();
    },
    onAdd: async (evt) => {
      const id = evt.item.dataset.id;
      S.dragging = false;
      favList.classList.remove("drop-target");
      try {
        await api("PUT", `/api/tunnels/${id}`, { favorite: true });
        await saveFavOrder();
        toast("ok", "Added to favourites", "", 1800);
      } catch (e) { toast("err", e.message); }
      render();
    },
  });
  histSortable = Sortable.create(histList, {
    ...common,
    group: { name: "tunnels", pull: true, put: false },
    sort: false,
    onEnd: () => { common.onEnd(); render(); },
  });
}

async function saveFavOrder() {
  const ids = $$("#fav-list > .item").map((el) => el.dataset.id);
  try { await api("PUT", "/api/favorites/order", { ids }); } catch (e) { toast("err", e.message); }
}

// ------------------------------------------------------------------ wiring

function init() {
  hydrateIcons();
  applyTheme(store.get("tm-theme", "auto"));
  syncNotifyButton();
  $("#sort").value = S.sort;

  $("#btn-theme").onclick = () => {
    const next = THEMES[(THEMES.indexOf(store.get("tm-theme", "auto")) + 1) % THEMES.length];
    store.set("tm-theme", next);
    applyTheme(next);
  };
  $("#btn-notify").onclick = toggleNotifications;
  $("#btn-settings").onclick = openSettings;
  $("#btn-new").onclick = () => openEditor(null);
  $("#sort").onchange = (e) => {
    S.sort = e.target.value;
    store.set("tm-sort", S.sort);
    S.histOrder = [];
    render();
  };
  $("#search").addEventListener("input", (e) => { S.query = e.target.value; render(); });

  for (const list of [$("#fav-list"), $("#hist-list")]) {
    list.addEventListener("click", (e) => {
      const b = e.target.closest("[data-act]");
      const item = e.target.closest(".item");
      if (!b || !item || b.disabled) return;
      act(b.dataset.act, item.dataset.id, b);
    });
    list.addEventListener("dblclick", (e) => {
      const name = e.target.closest(".name");
      const item = e.target.closest(".item");
      if (name && item) { e.preventDefault(); startRename(item.dataset.id); }
    });
  }

  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea, select, [contenteditable]");
    if (modal.open) return;
    if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); }
    else if (e.key === "n" && !typing && !e.metaKey && !e.ctrlKey) { e.preventDefault(); openEditor(null); }
    else if (e.key === "Escape" && e.target === $("#search")) { $("#search").value = ""; S.query = ""; render(); $("#search").blur(); }
  });

  setupSortable();
  connect();
  loadHosts();
  setInterval(updateTimes, 1000);
}

init();

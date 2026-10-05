"use strict";
// ======================================================================= helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nf = new Intl.NumberFormat();
const fmt = (n) => (n === null || n === undefined || n === "" ? "–" : nf.format(Math.round(n * 10) / 10));
const fmtDur = (s) => {
  if (s === null || s === undefined) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}h ${m}m ${x}s` : m ? `${m}m ${x}s` : `${x}s`;
};
const tstr = (ms) => new Date(ms).toLocaleTimeString([], { hour12: false });
const dtstr = (sec) => (sec ? new Date(sec * 1000).toLocaleString([], { hour12: false }) : "–");
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch { /* not json */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function toast(msg, kind = "") {
  const d = document.createElement("div");
  d.className = kind;
  d.textContent = msg;
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), kind === "error" ? 7000 : 3500);
}

async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, "ok"); return r; } catch (e) { toast(e.message, "error"); }
}

/** Update table rows in place (keyed), only touching cells whose HTML changed. */
function syncRows(tbody, rows) {
  const existing = new Map([...tbody.children].map((tr) => [tr.dataset.key, tr]));
  let prev = null;
  for (const { key, cells, cls, attrs } of rows) {
    let tr = existing.get(String(key));
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.key = key;
      cells.forEach(() => tr.appendChild(document.createElement("td")));
    }
    existing.delete(String(key));
    if (cls !== undefined && tr.className !== cls) tr.className = cls;
    if (attrs) for (const [a, v] of Object.entries(attrs)) tr.setAttribute(a, v);
    while (tr.children.length < cells.length) tr.appendChild(document.createElement("td"));
    cells.forEach((c, i) => {
      const td = tr.children[i];
      const html = typeof c === "object" ? c.html : c;
      const tdc = typeof c === "object" ? c.cls || "" : "";
      if (td._h !== html) { td.innerHTML = html; td._h = html; }
      if (td.className !== tdc) td.className = tdc;
    });
    const want = prev ? prev.nextSibling : tbody.firstChild;
    if (want !== tr) tbody.insertBefore(tr, want);
    prev = tr;
  }
  existing.forEach((tr) => tr.remove());
}

function setHTML(el, html) { if (el && el._h !== html) { el.innerHTML = html; el._h = html; } }

// ======================================================================= chart
function drawChart(canvas, points, seriesDefs) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue("--grid").trim(), muted = css.getPropertyValue("--muted").trim();
  const padL = 48, padR = 10, padT = 8, padB = 22;
  const N = 300;
  const pts = points.slice(-N);
  let max = 1;
  for (const p of pts) for (const s of seriesDefs) max = Math.max(max, p[s.idx] || 0);
  const nice = (v) => { const e = Math.pow(10, Math.floor(Math.log10(v))); const m = v / e; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * e; };
  max = nice(max * 1.1);
  const X = (i) => padL + (w - padL - padR) * (i / (N - 1));
  const Y = (v) => padT + (h - padT - padB) * (1 - v / max);
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = muted; ctx.strokeStyle = grid; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4, y = Math.round(Y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + "k" : String(Math.round(v)), padL - 6, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const sec of [300, 240, 180, 120, 60, 0]) {
    const i = N - 1 - sec;
    ctx.fillText(sec ? `-${sec / 60}m` : "now", X(i), h - padB + 6);
  }
  const off = N - pts.length;
  for (const s of seriesDefs) {
    ctx.strokeStyle = css.getPropertyValue(s.color).trim();
    ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    pts.forEach((p, i) => { const x = X(i + off), y = Y(p[s.idx] || 0); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }
}

// ======================================================================= state
let S = { connections: [], jobs: [], mock: {}, series: {} };
let defaults = {};
let templates = [];
let view = "dashboard";
let selectedJob = null;
let ws = null;
const logs = [], inbound = [], traces = [];
const sub = { job: null, trace: false };

function wsSend() { if (ws && ws.readyState === 1) ws.send(JSON.stringify(sub)); }

function connectWS() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => { $("#ws-dot").className = "dot ok"; $("#ws-text").textContent = "live"; wsSend(); };
  ws.onclose = () => { $("#ws-dot").className = "dot err"; $("#ws-text").textContent = "disconnected – retrying"; setTimeout(connectWS, 2000); };
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    for (const l of d.logs || []) logs.push(l);
    if (logs.length > 2000) logs.splice(0, logs.length - 2000);
    for (const r of d.inbound || []) inbound.push(r);
    if (inbound.length > 3000) inbound.splice(0, inbound.length - 3000);
    if (d.trace && !$("#tr-pause").checked) { for (const t of d.trace) traces.push(t); if (traces.length > 3000) traces.splice(0, traces.length - 3000); }
    S = d;
    render(d.logs || []);
  };
}

// ======================================================================= navigation
function show(v) {
  view = v;
  $$("nav.side button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === "v-" + v));
  if (location.hash !== "#" + v) history.replaceState(null, "", "#" + v);
  sub.trace = v === "trace";
  sub.job = v === "jobs" ? selectedJob : (v === "dashboard" ? null : sub.job);
  wsSend();
  if (v === "history") loadHistory();
  render([]);
}
$$("nav.side button[data-view]").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  store.set("theme", next);
  render([]);
});
if (store.get("theme")) document.documentElement.dataset.theme = store.get("theme");

// ======================================================================= render dispatch
function render(newLogs) {
  const bound = S.connections.reduce((a, c) => a + c.bound, 0), total = S.connections.reduce((a, c) => a + c.total, 0);
  $("#nav-binds").textContent = `${bound}/${total}`;
  $("#nav-jobs").textContent = S.jobs.filter((j) => ["running", "paused", "stopping", "pending"].includes(j.state)).length;
  fillConnSelects();
  if (view === "dashboard") renderDashboard();
  if (view === "connections") renderConnections();
  if (view === "send") { renderSendStatus(); renderSingleResult(); }
  if (view === "jobs") renderJobs();
  if (view === "inbound") renderInbound();
  if (view === "trace") renderTrace();
  if (view === "logs") renderLogs();
  if (view === "mock") renderMock();
  if (view === "dashboard" && newLogs) renderDashLog();
}

function fillConnSelects() {
  const sig = S.connections.map((c) => c.id + c.profile.name).join("|");
  for (const sel of [$("#s-conn"), $("#dash-conn")]) {
    if (sel._sig === sig) continue;
    const cur = sel.value || store.get(sel.id, "");
    const opts = S.connections.map((c) => `<option value="${c.id}">${esc(c.profile.name)} (${esc(c.profile.host)}:${c.profile.port})</option>`);
    sel.innerHTML = (sel.id === "dash-conn" ? `<option value="">All connections</option>` : "") + opts.join("");
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    sel._sig = sig;
  }
}
$("#dash-conn").addEventListener("change", (e) => { store.set("dash-conn", e.target.value); render([]); });

// ======================================================================= dashboard
function aggSeries(conns) {
  const out = new Map();
  for (const c of conns) for (const p of S.series?.[c.id] || []) {
    const k = Math.round(p[0] / 1000);
    const o = out.get(k) || [p[0], 0, 0, 0, 0];
    for (let i = 1; i <= 4; i++) o[i] += p[i] || 0;
    out.set(k, o);
  }
  return [...out.values()].sort((a, b) => a[0] - b[0]);
}

function renderDashboard() {
  const cid = $("#dash-conn").value;
  const conns = S.connections.filter((c) => !cid || c.id === cid);
  const sum = (f) => conns.reduce((a, c) => a + (f(c) || 0), 0);
  const tiles = [
    ["Binds bound", `${sum((c) => c.bound)}<span class="muted" style="font-size:15px"> / ${sum((c) => c.total)}</span>`, `${conns.length} connection(s)`],
    ["Submit TPS", fmt(sum((c) => c.rate.submit)), "submit_sm / s"],
    ["Response TPS", fmt(sum((c) => c.rate.resp)), "submit_sm_resp / s"],
    ["DLR / s", fmt(sum((c) => c.rate.dlr)), `MO / s: ${fmt(sum((c) => c.rate.mo))}`],
    ["In flight", fmt(sum((c) => c.inflight)), "awaiting resp"],
    ["Submitted", fmt(sum((c) => c.totals.submit)), "PDUs total"],
    ["OK", `<span class="ok-t">${fmt(sum((c) => c.counts.ok))}</span>`, "ESME_ROK"],
    ["Errors", `<span class="err-t">${fmt(sum((c) => (c.counts.err || 0) + (c.counts.timeout || 0)))}</span>`, `timeouts: ${fmt(sum((c) => c.counts.timeout))}`],
    ["DLRs", fmt(sum((c) => c.counts.dlr)), `unmatched: ${fmt(S.unmatched_dlr)}`],
  ];
  setHTML($("#dash-tiles"), tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join(""));
  drawChart($("#dash-chart"), aggSeries(conns), [{ idx: 1, color: "--s1" }, { idx: 2, color: "--s2" }, { idx: 3, color: "--s3" }, { idx: 4, color: "--s4" }]);
  const active = S.jobs.filter((j) => ["running", "paused", "stopping", "pending"].includes(j.state) || (j.finished && Date.now() / 1000 - j.finished < 60));
  setHTML($("#dash-jobs"), active.length ? `<div class="tablewrap"><table><tr><th>Job</th><th>State</th><th>Progress</th><th class="num">TPS</th><th class="num">OK</th><th class="num">Err</th></tr>${active.map((j) => {
    const done = j.counts.messages || 0, pct = j.planned ? Math.min(100, (100 * done) / j.planned) : 0;
    return `<tr class="click" data-job="${j.id}"><td>${esc(j.name)}</td><td><span class="state ${j.state}">${j.state}</span></td><td><div class="progress"><div style="width:${pct}%"></div></div><span class="small muted">${fmt(done)}${j.planned ? " / " + fmt(j.planned) : ""}</span></td><td class="num">${fmt(j.tps_target || 0) === "0" ? "∞" : fmt(j.tps_target)}</td><td class="num ok-t">${fmt(j.counts.ok || 0)}</td><td class="num err-t">${fmt((j.counts.err || 0) + (j.counts.timeout || 0) + (j.counts.lost || 0))}</td></tr>`;
  }).join("")}</table></div>` : `<div class="empty">No active jobs. <a href="#send" data-goto="send">Send some messages →</a></div>`);
}
function renderDashLog() {
  const recent = logs.slice(-60).reverse();
  setHTML($("#dash-log"), recent.map((l) => `<div class="${l.level}">${tstr(l.t)} ${esc(l.msg)}</div>`).join("") || `<div class="empty">No events yet.</div>`);
}
$("#dash-jobs").addEventListener("click", (e) => { const tr = e.target.closest("[data-job]"); if (tr) { selectJob(tr.dataset.job); show("jobs"); } });
document.addEventListener("click", (e) => { const a = e.target.closest("[data-goto]"); if (a) { e.preventDefault(); show(a.dataset.goto); } });

// ======================================================================= connections
function renderConnections() {
  const list = $("#conn-list");
  if (!S.connections.length) {
    setHTML(list, `<div class="panel empty">No connections yet. Click <b>+ New connection</b> to add your SMPP account, or start the <a href="#mock" data-goto="mock">Mock SMSC</a> to try things out.</div>`);
    return;
  }
  if (list.querySelector(".empty")) { list.innerHTML = ""; list._h = null; }
  const seen = new Set();
  for (const c of S.connections) {
    seen.add(c.id);
    let card = list.querySelector(`[data-conn="${c.id}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "panel";
      card.dataset.conn = c.id;
      card.innerHTML = `<h2><span class="cname"></span><span class="cstat"></span><span class="grow"></span>
        <label class="c small"><input type="checkbox" data-act="trace"> Trace PDUs</label>
        <button class="btn sm primary" data-act="bind">Bind all</button>
        <button class="btn sm" data-act="unbind">Unbind all</button>
        <button class="btn sm" data-act="rebind">Rebind</button>
        <button class="btn sm" data-act="add_bind">+ Bind</button>
        <button class="btn sm" data-act="edit">Edit</button>
        <button class="btn sm" data-act="reset_stats">Reset stats</button>
        <button class="btn sm danger" data-act="delete">Delete</button></h2>
        <div class="cinfo small muted" style="margin:-6px 0 10px"></div>
        <div class="tablewrap"><table><thead><tr><th>#</th><th>Type</th><th>State</th><th>Uptime</th><th class="num">Window</th><th class="num">Submitted</th><th class="num">OK</th><th class="num">Err</th><th class="num">Timeout</th><th class="num">DLR</th><th class="num">MO</th><th class="num">Enq RTT</th><th class="num">Reconn</th><th>Last error</th><th></th></tr></thead><tbody></tbody></table></div>`;
      list.appendChild(card);
    }
    const p = c.profile;
    setHTML(card.querySelector(".cname"), esc(p.name));
    setHTML(card.querySelector(".cstat"), `<span class="pill"><span class="dot ${c.bound === c.total && c.total ? "ok" : c.bound ? "warn" : ""}"></span>${c.bound}/${c.total} bound</span>`);
    setHTML(card.querySelector(".cinfo"), `${esc(p.host)}:${p.port} · system_id <b>${esc(p.system_id)}</b> · ${p.bind_type} × ${p.binds}${p.rx_binds ? ` + RX × ${p.rx_binds}` : ""} · window ${p.window} · enquire ${p.enquire_interval}s · ${p.tls ? "TLS" : "plain TCP"} · in flight ${fmt(c.inflight)} · ${fmt(c.rate.submit)} submit/s`);
    card.querySelector('[data-act="trace"]').checked = c.trace;
    const rows = c.sessions.map((s) => ({
      key: s.index,
      cells: [
        String(s.index), s.bind_type, `<span class="state ${s.state}">${s.state}</span>`, s.uptime === null ? "–" : fmtDur(s.uptime),
        { html: `${s.inflight}/${p.window}`, cls: "num" }, { html: fmt(s.stats.submit), cls: "num" }, { html: fmt(s.stats.resp_ok), cls: "num" },
        { html: fmt(s.stats.resp_err), cls: "num" }, { html: fmt(s.stats.timeout), cls: "num" }, { html: fmt(s.stats.dlr), cls: "num" },
        { html: fmt(s.stats.mo), cls: "num" }, { html: s.enquire_rtt_ms === null ? "–" : s.enquire_rtt_ms + " ms", cls: "num" },
        { html: String(s.reconnects), cls: "num" }, `<span class="err-t small">${esc(s.last_error)}</span>`,
        ["idle", "error"].includes(s.state)
          ? `<button class="btn sm" data-act="bind_one" data-i="${s.index}">Bind</button> <button class="btn sm danger" data-act="remove_bind" data-i="${s.index}" title="Remove this bind">✕</button>`
          : `<button class="btn sm" data-act="unbind_one" data-i="${s.index}">Unbind</button>`,
      ],
    }));
    const tbody = card.querySelector("tbody");
    if (!rows.length) setHTML(tbody, `<tr><td colspan="15" class="empty">Not bound yet — click <b>Bind all</b>.</td></tr>`);
    else { if (tbody.querySelector(".empty")) { tbody.innerHTML = ""; tbody._h = null; } syncRows(tbody, rows); }
  }
  $$("[data-conn]", list).forEach((el) => { if (!seen.has(el.dataset.conn)) el.remove(); });
}

$("#conn-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b || b.tagName === "INPUT") return;
  const cid = b.closest("[data-conn]").dataset.conn;
  const a = b.dataset.act;
  const conn = S.connections.find((c) => c.id === cid);
  if (a === "edit") return openConnDialog(conn);
  if (a === "delete") {
    if (!confirm(`Delete connection "${conn.profile.name}"? It will be unbound.`)) return;
    return act(() => api("DELETE", `/api/connections/${cid}`), "Connection deleted");
  }
  const body = b.dataset.i ? { index: +b.dataset.i } : undefined;
  b.disabled = true;
  await act(() => api("POST", `/api/connections/${cid}/${a}`, body));
  b.disabled = false;
});
$("#conn-list").addEventListener("change", (e) => {
  if (e.target.dataset.act !== "trace") return;
  const cid = e.target.closest("[data-conn]").dataset.conn;
  act(() => api("POST", `/api/connections/${cid}/trace`, { on: e.target.checked }), e.target.checked ? "Tracing enabled" : "Tracing disabled");
});

let editingConn = null;
function openConnDialog(conn, preset) {
  editingConn = conn ? conn.id : null;
  const f = $("#conn-form");
  const p = Object.assign({}, defaults, conn ? conn.profile : {}, preset || {});
  if (!conn && !preset) { p.name = "smsc-" + (S.connections.length + 1); p.binds = 10; }
  for (const el of f.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") el.checked = !!p[el.name];
    else if (el.name === "password") el.value = preset && preset.password ? preset.password : "";
    else el.value = p[el.name] ?? "";
  }
  $("#conn-dialog-title").textContent = conn ? `Edit connection – ${conn.profile.name}` : "New connection";
  $("#conn-pass-hint").textContent = conn && conn.profile.has_password ? "Leave password empty to keep the stored password. Changes to bind count/type apply on the next Bind all." : "";
  $("#conn-dialog").showModal();
}
$("#conn-new").addEventListener("click", () => openConnDialog(null));
$("#conn-form").addEventListener("submit", async (e) => {
  const action = e.submitter && e.submitter.value;
  if (action === "cancel") return;
  e.preventDefault();
  const f = e.target, data = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    data[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" || el.name === "interface_version" ? Number(el.value) : el.value;
  }
  if (editingConn) data.id = editingConn;
  const conn = await act(() => api("POST", "/api/connections", data), "Connection saved");
  if (!conn) return;
  $("#conn-dialog").close();
  if (action === "savebind") act(() => api("POST", `/api/connections/${conn.id}/bind`), "Binding…");
});

// ======================================================================= send: message info
const GSM_BASIC = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\u001bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
const GSM_EXT = "\f^{}\\[~]|€";
function msgInfo(text, enc) {
  const isGsm = [...text].every((c) => (GSM_BASIC.includes(c) && c !== "\u001b") || GSM_EXT.includes(c));
  let name = enc === "auto" ? (isGsm ? "gsm7" : "ucs2") : enc;
  if (name === "binary") {
    const n = text.replace(/\s/g, "").length / 2;
    return { name, units: n, unit: "octets", segs: n <= 140 ? 1 : Math.ceil(n / 134), single: 140, part: 134, warn: /^[0-9a-f\s]*$/i.test(text) ? "" : "invalid hex" };
  }
  const gsm = name.startsWith("gsm7");
  const w = [...text].map((c) => (gsm ? (GSM_EXT.includes(c) ? 2 : 1) : name === "ucs2" ? (c.codePointAt(0) > 0xffff ? 4 : 2) : 1));
  const total = w.reduce((a, b) => a + b, 0);
  const single = gsm ? 160 : 140, part = gsm ? 153 : 134;
  let segs = 1;
  if (total > single) { segs = 1; let cur = 0; for (const x of w) { if (cur + x > part) { segs++; cur = 0; } cur += x; } }
  let warn = "";
  if (gsm && !isGsm) warn = "contains characters outside GSM 03.38 – they will be sent as '?'";
  if ((name === "latin1" || name === "ascii") && [...text].some((c) => c.charCodeAt(0) > (name === "ascii" ? 127 : 255))) warn = `contains characters not representable in ${name}`;
  return { name, units: total, unit: gsm ? "septets" : "octets", segs, single, part, chars: [...text].length, warn };
}

function updateCounter() {
  const text = $("#s-text").value, enc = $("#s-enc").value;
  const i = msgInfo(text, enc);
  const long = $("#s-long").value;
  const pdus = i.segs > 1 && (long === "payload" || long === "truncate") ? 1 : i.segs;
  const left = i.segs === 1 ? i.single - i.units : i.part * i.segs - i.units;
  $("#s-counter").innerHTML = `<span>Encoding <b>${i.name.toUpperCase()}</b></span><span><b>${fmt(i.chars ?? i.units)}</b> chars</span><span><b>${fmt(i.units)}</b> ${i.unit}</span>
    <span><b>${i.segs}</b> segment${i.segs > 1 ? "s" : ""} (${i.segs > 1 ? i.part + "/segment" : i.single + " max"}) · ${left} left</span>
    ${long === "payload" && i.segs > 1 ? "<span>sent as 1 PDU (message_payload)</span>" : ""}${long === "truncate" && i.segs > 1 ? '<span class="warn-t">will be truncated</span>' : ""}
    ${/\{(number|seq|otp|rand|uuid|time|date|job)/.test(text) ? '<span class="muted">placeholders: count is for the raw template</span>' : ""}
    ${i.warn ? `<span class="warn-t">⚠ ${esc(i.warn)}</span>` : ""}`;
  return { ...i, pdus };
}

const PLACEHOLDERS = [["{number}", "destination"], ["{seq}", "message #"], ["{seq:6}", "zero padded"], ["{otp}", "6-digit random"], ["{rand:4}", "N random digits"], ["{uuid:8}", "random hex"], ["{time}", "HH:MM:SS"], ["{date}", "YYYY-MM-DD"], ["{job}", "job id"]];
$("#s-placeholders").innerHTML = PLACEHOLDERS.map(([p, d]) => `<span class="pill" title="${d}" data-ph="${p}">${p}</span>`).join("");
$("#s-placeholders").addEventListener("click", (e) => {
  const p = e.target.closest("[data-ph]"); if (!p) return;
  const ta = $("#s-text"), s = ta.selectionStart, en = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + p.dataset.ph + ta.value.slice(en);
  ta.focus(); ta.selectionStart = ta.selectionEnd = s + p.dataset.ph.length;
  onSendFormChange();
});

// TON / NPI selects
const TON = [[0, "0 Unknown"], [1, "1 International"], [2, "2 National"], [3, "3 Network specific"], [4, "4 Subscriber"], [5, "5 Alphanumeric"], [6, "6 Abbreviated"]];
const NPI = [[0, "0 Unknown"], [1, "1 ISDN (E.164)"], [3, "3 Data (X.121)"], [4, "4 Telex"], [6, "6 Land mobile"], [8, "8 National"], [9, "9 Private"], [10, "10 ERMES"], [14, "14 Internet (IP)"], [18, "18 WAP"]];
const opts = (arr) => arr.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
$("#s-ston").innerHTML = opts(TON); $("#s-snpi").innerHTML = opts(NPI); $("#s-dton").innerHTML = opts(TON); $("#s-dnpi").innerHTML = opts(NPI);
$("#s-ston").value = 5; $("#s-snpi").value = 0; $("#s-dton").value = 1; $("#s-dnpi").value = 1;
$("#s-src").addEventListener("change", () => {
  const v = $("#s-src").value;
  if (/^\+?\d+$/.test(v)) { $("#s-ston").value = v.length > 8 ? 1 : 3; $("#s-snpi").value = v.length > 8 ? 1 : 0; }
  else { $("#s-ston").value = 5; $("#s-snpi").value = 0; }
});

// custom TLV rows
function addTlvRow(t = { tag: "0x", format: "string", value: "" }) {
  const d = document.createElement("div");
  d.className = "row tlv-row";
  d.style.marginBottom = "6px";
  d.innerHTML = `<input class="t-tag" placeholder="tag e.g. 0x1403" style="width:130px" value="${esc(t.tag)}">
    <select class="t-fmt" style="width:120px">${["string", "cstring", "hex", "int8", "int16", "int32"].map((f) => `<option ${f === t.format ? "selected" : ""}>${f}</option>`).join("")}</select>
    <input class="t-val" placeholder="value" style="flex:1;min-width:120px" value="${esc(t.value)}">
    <button class="btn sm danger t-del">✕</button>`;
  $("#tlv-rows").appendChild(d);
}
$("#tlv-add").addEventListener("click", () => addTlvRow());
$("#tlv-rows").addEventListener("click", (e) => { if (e.target.classList.contains("t-del")) { e.target.closest(".tlv-row").remove(); onSendFormChange(); } });

// recipients tabs
let source = "single";
function setSource(src) {
  source = src;
  $$("#send-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== src));
  onSendFormChange();
}
$("#send-tabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-src]"); if (b) setSource(b.dataset.src); });

function parseList(text, dedupe) {
  const out = []; const seen = new Set(); let invalid = 0, dups = 0;
  for (let raw of text.split(/[\n,;]+/)) {
    raw = raw.trim(); if (!raw) continue;
    let n = raw.replace(/[\s\-()"']/g, "");
    if (n.startsWith("+")) n = n.slice(1); else if (n.startsWith("00")) n = n.slice(2);
    if (!/^\d{1,20}$/.test(n)) { invalid++; continue; }
    if (dedupe && seen.has(n)) { dups++; continue; }
    seen.add(n); out.push(n);
  }
  return { out, invalid, dups };
}

function genSample() {
  const cc = $("#g-cc").value.replace(/\D/g, ""), len = +$("#g-len").value || 10;
  const pre = $("#g-prefix").value.split(/[,\s]+/).filter((p) => /^\d+$/.test(p));
  const prefixes = pre.length ? pre : [""];
  const out = [];
  if ($("#g-mode").value === "sequential") {
    const p = prefixes[0], start = +$("#g-start").value || 0;
    for (let i = 0; i < 3; i++) out.push(cc + p + String(start + i).padStart(len - p.length, "0"));
  } else {
    for (let i = 0; i < 3; i++) {
      const p = prefixes[Math.floor(Math.random() * prefixes.length)];
      let r = ""; for (let k = 0; k < len - p.length; k++) r += Math.floor(Math.random() * 10);
      out.push(cc + p + r);
    }
  }
  return out.join(", ") + ", …";
}

let csvUpload = null;
$("#csv-upload").addEventListener("click", async () => {
  const f = $("#csv-file").files[0];
  if (!f) return toast("Choose a file first", "error");
  const fd = new FormData(); fd.append("file", f);
  $("#csv-info").innerHTML = `<div class="hint">Uploading…</div>`;
  const r = await act(() => api("POST", "/api/upload", fd));
  if (!r) { $("#csv-info").innerHTML = ""; return; }
  csvUpload = r;
  if (r.has_message_col && +$("#csv-msgcol").value === 0) $("#csv-msgcol").value = 2;
  renderCsvInfo();
  onSendFormChange();
});
$("#csv-file").addEventListener("change", () => $("#csv-upload").click());
function renderCsvInfo() {
  const r = csvUpload;
  if (!r) return;
  $("#csv-info").innerHTML = `<div class="result"><div class="kv"><span>File</span><span>${esc(r.filename)} (${fmt(r.bytes / 1024)} KB)</span>
    <span>Valid rows</span><span><b>${fmt(r.rows)}</b> (${fmt(r.unique)} unique, ${fmt(r.skipped)} skipped)</span>
    <span>Preview</span><span class="mono">${r.preview.map(esc).join("<br>")}</span></div></div>`;
}

function recipientCount() {
  if (source === "single") return parseList($("#s-single").value, true).out.length ? 1 : 0;
  if (source === "list") return parseList($("#s-list").value, $("#s-dedupe").checked).out.length;
  if (source === "generate") return +$("#g-count").value || 0;
  if (source === "csv") return csvUpload ? ($("#csv-dedupe").checked ? csvUpload.unique : csvUpload.rows) : 0;
  return 0;
}

function onSendFormChange() {
  const info = updateCounter();
  if (source === "list") {
    const r = parseList($("#s-list").value, $("#s-dedupe").checked);
    $("#s-list-count").innerHTML = `<b>${fmt(r.out.length)}</b> valid numbers${r.dups ? ` · ${fmt(r.dups)} duplicates removed` : ""}${r.invalid ? ` · <span class="err-t">${fmt(r.invalid)} invalid</span>` : ""}`;
  }
  if (source === "generate") $("#g-sample").textContent = genSample();
  const n = recipientCount();
  const repeat = source === "single" ? 1 : Math.max(1, +$("#s-repeat").value || 1);
  const dur = source === "single" ? 0 : +$("#s-duration").value || 0;
  const max = source === "single" ? 0 : +$("#s-max").value || 0;
  let msgs = n * repeat; if (max) msgs = Math.min(msgs, max);
  const tps = +$("#s-tps").value || 0;
  const pdus = msgs * info.pdus;
  let html;
  if (dur) {
    const est = tps ? tps * dur : null;
    html = `Duration mode: cycle through <b>${fmt(n)}</b> recipients for <b>${fmtDur(dur)}</b>${est ? ` ≈ <b>${fmt(max ? Math.min(est, max * info.pdus) : est)}</b> PDUs at ${fmt(tps)} TPS` : " at max speed"}.`;
  } else {
    html = `<b>${fmt(msgs)}</b> message${msgs === 1 ? "" : "s"} × ${info.pdus} PDU = <b>${fmt(pdus)}</b> submit_sm` +
      (tps && pdus ? ` · ETA ≈ <b>${fmtDur(pdus / tps)}</b> at ${fmt(tps)} TPS` : pdus ? " · unlimited TPS (window-bound)" : "");
  }
  $("#s-summary").innerHTML = html;
  $("#s-send").textContent = source === "single" ? "Send SMS" : `Start job (${fmt(dur ? n : msgs)} msgs)`;
  saveSendForm();
}

const SEND_FIELDS = ["s-single", "s-list", "s-dedupe", "g-count", "g-cc", "g-len", "g-prefix", "g-mode", "g-start", "g-unique", "csv-numcol", "csv-msgcol", "csv-dedupe",
  "s-src", "s-text", "s-enc", "s-long", "s-dlr", "s-dlt-pe", "s-dlt-tpl", "s-dlt-tm", "s-dlt-nul", "s-ston", "s-snpi", "s-dton", "s-dnpi", "s-svc", "s-esm", "s-pid",
  "s-prio", "s-dc", "s-valid", "s-sched", "s-tps", "s-ramp", "s-repeat", "s-duration", "s-max", "s-retries", "s-conn"];
function formValues(fields = SEND_FIELDS) {
  const v = {};
  for (const id of fields) { const el = $("#" + id); v[id] = el.type === "checkbox" ? el.checked : el.value; }
  v.tlvs = $$(".tlv-row").map((r) => ({ tag: $(".t-tag", r).value, format: $(".t-fmt", r).value, value: $(".t-val", r).value }));
  v.source = source;
  return v;
}
function applyValues(v) {
  for (const id of SEND_FIELDS) { const el = $("#" + id); if (!el || !(id in v)) continue; if (el.type === "checkbox") el.checked = !!v[id]; else el.value = v[id]; }
  $("#tlv-rows").innerHTML = "";
  (v.tlvs || []).forEach(addTlvRow);
  if (v.source) setSource(v.source);
}
let saveTimer = null;
function saveSendForm() { clearTimeout(saveTimer); saveTimer = setTimeout(() => store.set("sendform", formValues()), 400); }
$("#v-send").addEventListener("input", (e) => { if (!e.target.closest("#csv-info")) onSendFormChange(); });
$("#v-send").addEventListener("change", onSendFormChange);

// templates
function renderTemplates() {
  $("#tpl-select").innerHTML = `<option value="">Load saved template…</option>` + templates.map((t, i) => `<option value="${i}">${esc(t.name)}</option>`).join("");
}
$("#tpl-select").addEventListener("change", (e) => {
  const t = templates[+e.target.value]; if (!t) return;
  const keep = { "s-single": $("#s-single").value, "s-list": $("#s-list").value, "s-conn": $("#s-conn").value };
  applyValues(Object.assign({}, t.values, keep));
  onSendFormChange();
  toast(`Template "${t.name}" loaded`);
});
$("#tpl-save").addEventListener("click", async () => {
  const name = prompt("Template name:", $("#s-name").value || "");
  if (!name) return;
  const values = formValues();
  delete values["s-single"]; delete values["s-list"]; delete values["s-conn"];
  const idx = templates.findIndex((t) => t.name === name);
  if (idx >= 0) templates[idx] = { name, values }; else templates.push({ name, values });
  await act(() => api("POST", "/api/templates", templates), "Template saved");
  renderTemplates();
});
$("#tpl-del").addEventListener("click", async () => {
  const i = $("#tpl-select").value;
  if (i === "") return toast("Select a template first", "error");
  if (!confirm(`Delete template "${templates[+i].name}"?`)) return;
  templates.splice(+i, 1);
  await act(() => api("POST", "/api/templates", templates), "Template deleted");
  renderTemplates();
});

// send
function buildSpec() {
  const v = formValues();
  const spec = {
    connection: v["s-conn"], kind: source === "single" ? "single" : "bulk", name: $("#s-name").value.trim(),
    source: source === "single" ? "list" : source, message: v["s-text"], source_addr: v["s-src"],
    encoding: v["s-enc"], long_mode: v["s-long"], registered_delivery: +v["s-dlr"],
    source_ton: +v["s-ston"], source_npi: +v["s-snpi"], dest_ton: +v["s-dton"], dest_npi: +v["s-dnpi"],
    service_type: v["s-svc"], esm_class: +v["s-esm"] || 0, protocol_id: +v["s-pid"] || 0, priority: +v["s-prio"] || 0,
    data_coding_override: v["s-dc"] === "" ? null : +v["s-dc"], validity_period: v["s-valid"], schedule_delivery_time: v["s-sched"],
    dlt_entity_id: v["s-dlt-pe"], dlt_template_id: v["s-dlt-tpl"], dlt_tm_id: v["s-dlt-tm"], dlt_null_terminate: v["s-dlt-nul"],
    custom_tlvs: v.tlvs.filter((t) => t.tag && t.tag !== "0x"),
    tps: source === "single" ? 0 : +v["s-tps"] || 0, ramp_seconds: +v["s-ramp"] || 0, max_retries: +v["s-retries"] || 0,
  };
  if (source === "single") spec.numbers = v["s-single"];
  if (source === "list") { spec.numbers = v["s-list"]; spec.dedupe = v["s-dedupe"]; }
  if (source === "generate") Object.assign(spec, { gen_count: +v["g-count"], gen_country_code: v["g-cc"], gen_length: +v["g-len"], gen_prefixes: v["g-prefix"], gen_mode: v["g-mode"], gen_start: +v["g-start"] || 0, gen_unique: v["g-unique"] });
  if (source === "csv") Object.assign(spec, { csv_id: csvUpload && csvUpload.id, csv_number_col: +v["csv-numcol"] || 1, csv_message_col: +v["csv-msgcol"] || null, dedupe: v["csv-dedupe"] });
  if (source !== "single") Object.assign(spec, { repeat: +v["s-repeat"] || 1, duration: +v["s-duration"] || 0, max_messages: +v["s-max"] || 0 });
  if (!spec.name) spec.name = source === "single" ? `single → ${parseList(v["s-single"], true).out[0] || ""}` : `${source} ${new Date().toLocaleTimeString([], { hour12: false })}`;
  return spec;
}

let lastSingleJob = null;
$("#s-send").addEventListener("click", async () => {
  if (!S.connections.length) return toast("Create a connection first (Connections page).", "error");
  if (source === "csv" && !csvUpload) return toast("Upload a CSV file first.", "error");
  if (!recipientCount()) return toast("No valid destination numbers.", "error");
  const spec = buildSpec();
  const conn = S.connections.find((c) => c.id === spec.connection);
  if (conn && !conn.sessions.some((s) => s.state === "bound" && s.bind_type !== "RX")) {
    if (!confirm(`"${conn.profile.name}" has no bound TX/TRX sessions. The job will wait until a bind is up. Continue?`)) return;
  }
  const n = recipientCount();
  if (n > 100000 && spec.kind === "bulk" && !confirm(`This will send to ${fmt(n)} recipients. Continue?`)) return;
  $("#s-send").disabled = true;
  const job = await act(() => api("POST", "/api/jobs", spec));
  $("#s-send").disabled = false;
  if (!job) return;
  if (spec.kind === "single") { lastSingleJob = job.id; renderSingleResult(); toast("Message submitted"); }
  else { toast(`Job ${job.id} started`, "ok"); selectJob(job.id); show("jobs"); }
});

function renderSingleResult() {
  const box = $("#s-single-result");
  if (!lastSingleJob) return setHTML(box, "");
  const j = S.jobs.find((x) => x.id === lastSingleJob);
  if (!j) return;
  const r = j.single_result;
  const dlr = Object.entries(j.dlr_stats || {}).map(([k, v]) => `${k}${v > 1 ? " ×" + v : ""}`).join(", ");
  setHTML(box, `<div class="result"><div class="kv">
    <span>Job</span><span><a href="#jobs" data-jobid="${j.id}">${j.id}</a> · <span class="state ${j.state}">${j.state}</span></span>
    <span>submit_sm_resp</span><span>${r ? `<b class="${r.status === "ESME_ROK" ? "ok-t" : "err-t"}">${esc(r.status)}</b>` : "waiting…"}</span>
    <span>message_id</span><span class="mono">${r ? esc(r.message_id || "–") : "–"}</span>
    <span>Latency</span><span>${r && r.latency_ms !== "" ? r.latency_ms + " ms" : "–"}</span>
    <span>Segments</span><span>${fmt(j.counts.submitted || 0)} PDU(s)</span>
    <span>DLR</span><span>${dlr ? `<b>${esc(dlr)}</b>` : (j.counts.ok ? "waiting for delivery receipt…" : "–")}</span></div></div>`);
}
$("#s-single-result").addEventListener("click", (e) => { const a = e.target.closest("[data-jobid]"); if (a) { e.preventDefault(); selectJob(a.dataset.jobid); show("jobs"); } });

function renderSendStatus() {
  const c = S.connections.find((x) => x.id === $("#s-conn").value);
  if (!c) return setHTML($("#s-conn-status"), `<div class="empty">No connection. <a href="#connections" data-goto="connections">Create one →</a></div>`);
  const tx = c.sessions.filter((s) => s.bind_type !== "RX");
  const txb = tx.filter((s) => s.state === "bound").length;
  setHTML($("#s-conn-status"), `<div class="kv"><span>Connection</span><span><b>${esc(c.profile.name)}</b> ${esc(c.profile.host)}:${c.profile.port} (${esc(c.profile.system_id)})</span>
    <span>TX-capable binds</span><span><span class="dot ${txb ? (txb === tx.length ? "ok" : "warn") : "err"}"></span> ${txb} / ${tx.length} bound · window ${c.profile.window} each</span>
    <span>Live</span><span>${fmt(c.rate.submit)} submit/s · ${fmt(c.rate.resp)} resp/s · ${fmt(c.rate.dlr)} DLR/s · in flight ${fmt(c.inflight)}</span></div>
    ${!txb ? `<div class="row" style="margin-top:10px"><button class="btn sm primary" data-bindconn="${c.id}">Bind now</button></div>` : ""}`);
}
$("#s-conn-status").addEventListener("click", (e) => { const b = e.target.closest("[data-bindconn]"); if (b) act(() => api("POST", `/api/connections/${b.dataset.bindconn}/bind`), "Binding…"); });
$("#s-conn").addEventListener("change", (e) => { store.set("s-conn", e.target.value); render([]); });

// ======================================================================= jobs
function selectJob(id) {
  selectedJob = id;
  sub.job = id;
  wsSend();
  $("#job-detail").innerHTML = "";
  $("#job-detail")._job = null;
  renderJobs();
}

function renderJobs() {
  const t = $("#jobs-table");
  if (!S.jobs.length) return setHTML(t, `<tr><td class="empty">No jobs yet.</td></tr>`);
  if (!t.tHead) t.innerHTML = `<thead><tr><th>Job</th><th>Name</th><th>Connection</th><th>State</th><th>Progress</th><th class="num">Target TPS</th><th class="num">Avg TPS</th><th class="num">PDUs</th><th class="num">OK</th><th class="num">Err</th><th class="num">DLR</th><th>Elapsed</th></tr></thead><tbody></tbody>`;
  syncRows(t.tBodies[0], S.jobs.map((j) => {
    const done = j.counts.messages || 0, pct = j.planned ? Math.min(100, (100 * done) / j.planned) : (j.state === "completed" ? 100 : 0);
    return {
      key: j.id, cls: "click" + (j.id === selectedJob ? " sel" : ""), attrs: { "data-job": j.id },
      cells: [`<span class="mono small">${j.id}</span>${j.id === selectedJob ? " ◀" : ""}`, esc(j.name), esc(j.connection_name), `<span class="state ${j.state}">${j.state}</span>`,
        `<div class="progress"><div style="width:${pct}%"></div></div><span class="small muted">${fmt(done)}${j.planned ? " / " + fmt(j.planned) : ""}</span>`,
        { html: j.tps_target ? fmt(j.tps_target) : "∞", cls: "num" }, { html: fmt(j.avg_tps), cls: "num" }, { html: fmt(j.counts.submitted || 0), cls: "num" },
        { html: fmt(j.counts.ok || 0), cls: "num ok-t" }, { html: fmt((j.counts.err || 0) + (j.counts.timeout || 0) + (j.counts.lost || 0)), cls: "num err-t" },
        { html: fmt(j.counts.dlr || 0), cls: "num" }, fmtDur(j.elapsed)],
    };
  }));
  if (!selectedJob && S.jobs.length) selectJob(S.jobs[0].id);
  renderJobDetail();
}
$("#jobs-table").addEventListener("click", (e) => { const tr = e.target.closest("[data-job]"); if (tr) selectJob(tr.dataset.job); });

function renderJobDetail() {
  const box = $("#job-detail");
  const d = S.job_detail && S.job_detail.id === selectedJob ? S.job_detail : null;
  if (!d) return;
  if (box._job !== d.id) {
    box._job = d.id;
    box.innerHTML = `<div class="panel"><h2><span id="jd-title"></span><span class="grow"></span>
        <label class="row small">TPS <input id="jd-tps" type="number" min="0" step="any" style="width:100px"></label><button class="btn sm" id="jd-settps">Set</button>
        <button class="btn sm" id="jd-pause">Pause</button><button class="btn sm" id="jd-resume">Resume</button><button class="btn sm danger" id="jd-stop">Stop</button>
        <a class="btn sm" href="/api/jobs/${d.id}/submits.csv">⬇ submits.csv</a><a class="btn sm" href="/api/jobs/${d.id}/dlrs.csv">⬇ dlrs.csv</a></h2>
        <div id="jd-progress"></div><div class="tiles" id="jd-tiles" style="margin-top:12px"></div>
        <h3>Per-second throughput <span class="legend" style="display:inline-flex;margin-left:12px;text-transform:none"><span><i style="background:var(--s1)"></i>submitted</span><span><i style="background:var(--s2)"></i>responses</span><span><i style="background:var(--s3)"></i>DLRs</span></span></h3>
        <canvas class="chart" id="jd-chart"></canvas>
        <div class="grid g3" style="margin-top:12px"><div><h3>submit_sm_resp latency (ms)</h3><div id="jd-lat"></div></div><div><h3>Errors</h3><div id="jd-err"></div></div><div><h3>Delivery receipts</h3><div id="jd-dlr"></div></div></div>
        <h3>Latest messages</h3><div class="tablewrap scroll"><table id="jd-recent"><thead><tr><th>Time</th><th>#</th><th>Destination</th><th>Part</th><th>Bind</th><th>Status</th><th>message_id</th><th class="num">Latency</th><th>DLR</th></tr></thead><tbody></tbody></table></div>
        <details class="adv"><summary>Job parameters</summary><pre class="mono small" id="jd-spec" style="white-space:pre-wrap"></pre></details></div>`;
    $("#jd-tps").value = d.tps_target;
    $("#jd-settps").onclick = () => act(() => api("POST", `/api/jobs/${d.id}/tps`, { tps: +$("#jd-tps").value }), "TPS updated");
    $("#jd-pause").onclick = () => act(() => api("POST", `/api/jobs/${d.id}/pause`));
    $("#jd-resume").onclick = () => act(() => api("POST", `/api/jobs/${d.id}/resume`));
    $("#jd-stop").onclick = () => confirm("Stop this job?") && act(() => api("POST", `/api/jobs/${d.id}/stop`));
    $("#jd-spec").textContent = JSON.stringify(d.spec, null, 2);
  }
  const live = ["running", "paused", "pending"].includes(d.state);
  $("#jd-pause").disabled = d.state !== "running"; $("#jd-resume").disabled = d.state !== "paused"; $("#jd-stop").disabled = !live;
  $("#jd-settps").disabled = !live;
  setHTML($("#jd-title"), `${esc(d.name)} <span class="state ${d.state}">${d.state}</span> <span class="muted small">${d.id} · ${esc(d.connection_name)}</span>${d.error ? ` <span class="err-t small">${esc(d.error)}</span>` : ""}`);
  const c = d.counts, done = c.messages || 0, pct = d.planned ? Math.min(100, (100 * done) / d.planned) : (d.state === "completed" ? 100 : 0);
  const last = d.series.length ? d.series[d.series.length - 1] : [0, 0, 0, 0];
  const remain = d.planned && d.avg_tps ? (d.planned - done) / Math.max(1, d.avg_tps) : null;
  setHTML($("#jd-progress"), `<div class="progress" style="height:12px"><div style="width:${pct}%"></div></div>
    <div class="counter"><span><b>${fmt(done)}</b>${d.planned ? " / " + fmt(d.planned) : ""} messages (${pct.toFixed(1)}%)</span><span>elapsed <b>${fmtDur(d.elapsed)}</b></span>${live && remain !== null ? `<span>ETA <b>${fmtDur(remain)}</b></span>` : ""}
    <span>started ${dtstr(d.started)}</span>${d.finished ? `<span>finished ${dtstr(d.finished)}</span>` : ""}${d.tps_effective && d.tps_effective < d.tps_target ? `<span class="warn-t">ramping: ${fmt(d.tps_effective)} TPS</span>` : ""}</div>`);
  const tiles = [["Submit TPS (now)", fmt(last[1]), `target ${d.tps_target ? fmt(d.tps_target) : "∞"} · avg ${fmt(d.avg_tps)}`], ["PDUs submitted", fmt(c.submitted || 0), `${fmt(c.retried || 0)} retried`],
    ["OK", `<span class="ok-t">${fmt(c.ok || 0)}</span>`, c.submitted ? ((100 * (c.ok || 0)) / c.submitted).toFixed(2) + "%" : ""], ["Errors", `<span class="err-t">${fmt(c.err || 0)}</span>`, "error command_status"],
    ["Timeouts / lost", fmt((c.timeout || 0) + (c.lost || 0)), `${fmt(c.timeout || 0)} timeout · ${fmt(c.lost || 0)} lost`],
    ["DLRs", fmt(c.dlr || 0), c.ok ? ((100 * (c.dlr || 0)) / c.ok).toFixed(1) + "% of OK" : ""],
    ["Resp latency p95", d.latency ? fmt(d.latency.p95) + " ms" : "–", d.latency ? `avg ${fmt(d.latency.avg)} ms` : ""],
    ["DLR latency p50", d.dlr_latency ? fmtDur(d.dlr_latency.p50 / 1000) : "–", d.dlr_latency ? `p95 ${fmtDur(d.dlr_latency.p95 / 1000)}` : ""]];
  setHTML($("#jd-tiles"), tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join(""));
  drawChart($("#jd-chart"), d.series, [{ idx: 1, color: "--s1" }, { idx: 2, color: "--s2" }, { idx: 3, color: "--s3" }]);
  const L = d.latency;
  setHTML($("#jd-lat"), L ? `<table>${["min", "avg", "p50", "p95", "p99", "max"].map((k) => `<tr><td>${k}</td><td class="num">${fmt(L[k])}</td></tr>`).join("")}<tr><td class="muted">samples</td><td class="num muted">${fmt(L.n)}</td></tr></table>` : `<div class="muted">–</div>`);
  const errs = Object.entries(d.errors || {}).sort((a, b) => b[1] - a[1]);
  setHTML($("#jd-err"), errs.length ? `<table>${errs.map(([k, v]) => `<tr><td class="err-t">${esc(k)}</td><td class="num">${fmt(v)}</td></tr>`).join("")}</table>` : `<div class="muted">None 🎉</div>`);
  const dl = Object.entries(d.dlr_stats || {}).sort((a, b) => b[1] - a[1]);
  const tot = dl.reduce((a, [, v]) => a + v, 0);
  const col = (k) => (k === "DELIVRD" ? "var(--ok)" : ["UNDELIV", "REJECTD", "EXPIRED", "DELETED"].includes(k) ? "var(--err)" : "var(--warn)");
  setHTML($("#jd-dlr"), dl.length ? `<div class="bar">${dl.map(([k, v]) => `<span style="width:${(100 * v) / tot}%;background:${col(k)}" title="${k}"></span>`).join("")}</div>
    <table style="margin-top:8px">${dl.map(([k, v]) => `<tr><td><span class="dot" style="background:${col(k)}"></span> ${esc(k)}</td><td class="num">${fmt(v)}</td><td class="num muted">${((100 * v) / tot).toFixed(1)}%</td></tr>`).join("")}</table>` : `<div class="muted">${d.spec && d.spec.registered_delivery === 0 ? "DLR not requested" : "None yet"}</div>`);
  syncRows($("#jd-recent").tBodies[0], d.recent.slice().reverse().map((r, i) => ({
    key: `${r.t}-${r.n}-${r.part}-${i}`,
    cells: [tstr(r.t), String(r.n), `<span class="mono">${esc(r.dest)}</span>`, r.part, String(r.bind), `<span class="${r.ok ? "ok-t" : "err-t"}">${esc(r.status)}</span>`,
      `<span class="mono small">${esc(r.msg_id)}</span>`, { html: r.lat === "" ? "–" : r.lat + " ms", cls: "num" }, r.dlr ? `<span style="color:${col(r.dlr)}">${esc(r.dlr)}</span>` : "–"],
  })));
}

// ======================================================================= inbound
function renderInbound() {
  const kind = $("#in-kind").value, q = $("#in-search").value.trim().toLowerCase();
  const stats = {};
  let dlrs = 0, mos = 0;
  for (const r of inbound) { if (r.kind === "DLR") { dlrs++; stats[r.stat || "?"] = (stats[r.stat || "?"] || 0) + 1; } else mos++; }
  setHTML($("#in-tiles"), [["DLRs (buffer)", fmt(dlrs)], ["MO (buffer)", fmt(mos)], ...Object.entries(stats).sort((a, b) => b[1] - a[1]).slice(0, 6)]
    .map(([k, v]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${typeof v === "number" ? fmt(v) : v}</div></div>`).join(""));
  const rows = inbound.filter((r) => (!kind || r.kind === kind) && (!q || JSON.stringify(r).toLowerCase().includes(q))).slice(-500).reverse();
  const t = $("#in-table");
  if (!t.tHead) t.innerHTML = `<thead><tr><th>Time</th><th>Type</th><th>Conn / bind</th><th>From</th><th>To</th><th>message_id</th><th>Stat</th><th>Err</th><th>Job</th><th>Text</th></tr></thead><tbody></tbody>`;
  syncRows(t.tBodies[0], rows.map((r) => ({
    key: r.id,
    cells: [tstr(r.t), r.kind, `${esc(r.conn)} #${r.bind}`, `<span class="mono">${esc(r.src)}</span>`, `<span class="mono">${esc(r.dst)}</span>`, `<span class="mono small">${esc(r.msg_id || "")}</span>`,
      r.stat ? `<b class="${r.stat === "DELIVRD" ? "ok-t" : "err-t"}">${esc(r.stat)}</b>` : "", esc(r.err || ""), r.job ? `<span class="mono small">${r.job}</span>` : "", { html: esc(r.text), cls: "wrap small" }],
  })));
}
$("#in-kind").addEventListener("change", renderInbound);
$("#in-search").addEventListener("input", renderInbound);
$("#in-clear").addEventListener("click", async () => { inbound.length = 0; await act(() => api("POST", "/api/inbound/clear")); renderInbound(); });

// ======================================================================= trace
function renderTrace() {
  setHTML($("#tr-conns"), S.connections.map((c) => `<span class="pill"><span class="dot ${c.trace ? "ok" : ""}"></span>${esc(c.profile.name)}: ${c.trace ? "tracing" : "off"}</span>`).join(" ") || '<span class="muted">No connections</span>');
  const q = $("#tr-filter").value.trim().toLowerCase(), hideEl = $("#tr-hide-el").checked;
  const rows = traces.filter((t) => (!hideEl || !t.cmd.startsWith("enquire_link")) && (!q || `${t.cmd} ${t.seq} ${t.status} ${t.conn}`.toLowerCase().includes(q))).slice(-400).reverse();
  const t = $("#tr-table");
  if (!t.tHead) t.innerHTML = `<thead><tr><th>Time</th><th>Conn</th><th>Bind</th><th>Dir</th><th>Command</th><th>Status</th><th class="num">Seq</th><th class="num">Len</th><th>Hex</th></tr></thead><tbody></tbody>`;
  syncRows(t.tBodies[0], rows.map((r, i) => ({
    key: `${r.t}-${r.bind}-${r.dir}-${r.seq}-${r.cmd}`,
    cells: [tstr(r.t) + "." + String(r.t % 1000).padStart(3, "0"), esc(r.conn), String(r.bind), r.dir === "out" ? "→ out" : "← in", `<b>${esc(r.cmd)}</b>`,
      `<span class="${r.status && r.status !== "ESME_ROK" ? "err-t" : ""}">${esc(r.status)}</span>`, { html: String(r.seq), cls: "num" }, { html: String(r.len), cls: "num" },
      { html: `<span class="mono">${esc(r.hex)}</span>`, cls: "wrap small" }],
  })));
}
["#tr-filter", "#tr-hide-el"].forEach((s) => $(s).addEventListener("input", renderTrace));
$("#tr-clear").addEventListener("click", async () => { traces.length = 0; await act(() => api("POST", "/api/trace/clear")); renderTrace(); });

// ======================================================================= logs
function renderLogs() {
  const lvl = $("#log-level").value;
  const ok = (l) => !lvl || (lvl === "warn" ? l.level !== "info" : l.level === "error");
  setHTML($("#log-box"), logs.filter(ok).slice(-1000).reverse().map((l) => `<div class="${l.level}">${new Date(l.t).toLocaleString([], { hour12: false })} [${l.level}] ${esc(l.msg)}</div>`).join("") || `<div class="empty">No events.</div>`);
}
$("#log-level").addEventListener("change", renderLogs);

// ======================================================================= history
async function loadHistory() {
  const rows = await act(() => api("GET", "/api/history"));
  if (!rows) return;
  $("#hist-table").innerHTML = `<thead><tr><th>Job</th><th>Name</th><th>Connection</th><th>State</th><th>Started</th><th>Duration</th><th class="num">Msgs</th><th class="num">PDUs</th><th class="num">OK</th><th class="num">Err</th><th class="num">DLR</th><th class="num">Avg TPS</th><th class="num">p95 ms</th><th>Results</th></tr></thead><tbody>` +
    (rows.map((j) => { const c = j.counts || {}; return `<tr><td class="mono small">${esc(j.id)}</td><td>${esc(j.name)}</td><td>${esc(j.connection_name)}</td><td><span class="state ${j.state}">${esc(j.state)}</span></td><td>${dtstr(j.started)}</td><td>${fmtDur(j.elapsed)}</td>
      <td class="num">${fmt(c.messages || 0)}</td><td class="num">${fmt(c.submitted || 0)}</td><td class="num ok-t">${fmt(c.ok || 0)}</td><td class="num err-t">${fmt((c.err || 0) + (c.timeout || 0) + (c.lost || 0))}</td><td class="num">${fmt(c.dlr || 0)}</td>
      <td class="num">${fmt(j.avg_tps)}</td><td class="num">${j.latency ? fmt(j.latency.p95) : "–"}</td><td><a href="/api/jobs/${j.id}/submits.csv">submits</a> · <a href="/api/jobs/${j.id}/dlrs.csv">dlrs</a></td></tr>`; }).join("") ||
      `<tr><td colspan="14" class="empty">No finished jobs yet.</td></tr>`) + "</tbody>";
}
$("#hist-refresh").addEventListener("click", loadHistory);

// ======================================================================= mock
const MOCK_FIELDS = ["port", "max_binds", "system_id", "password", "tps_per_bind", "reject_rate", "resp_delay_ms", "dlr_delay_ms", "dlr_undeliv_rate", "send_dlr"];
function renderMock() {
  const m = S.mock || {};
  setHTML($("#mock-state"), `<span class="pill"><span class="dot ${m.running ? "ok" : ""}"></span>${m.running ? "running on port " + m.config.port : "stopped"}</span>`);
  const c = m.counts || {};
  setHTML($("#mock-status"), `<div class="kv"><span>State</span><span>${m.running ? "running" : "stopped"}</span><span>TCP connections</span><span>${fmt(m.connections || 0)}</span>
    <span>Binds per system_id</span><span>${Object.entries(m.binds || {}).map(([k, v]) => `${esc(k || "(empty)")}: ${v}`).join(", ") || "–"}</span>
    <span>Binds accepted / rejected</span><span>${fmt(c.binds || 0)} / ${fmt(c.bind_rejected || 0)}</span><span>submit_sm received</span><span>${fmt(c.submit || 0)}</span>
    <span>Throttled / rejected</span><span>${fmt(c.throttled || 0)} / ${fmt(c.rejected || 0)}</span><span>DLRs sent</span><span>${fmt(c.dlr || 0)}</span></div>`);
}
$("#mock-start").addEventListener("click", () => {
  const cfg = {};
  for (const k of MOCK_FIELDS) { const el = $("#m-" + k); cfg[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value; }
  store.set("mockcfg", cfg);
  act(() => api("POST", "/api/mock/start", cfg), "Mock SMSC started");
});
$("#mock-stop").addEventListener("click", () => act(() => api("POST", "/api/mock/stop"), "Mock SMSC stopped"));
$("#mock-profile").addEventListener("click", () => {
  show("connections");
  openConnDialog(null, { name: "mock-smsc", host: "127.0.0.1", port: +$("#m-port").value, system_id: $("#m-system_id").value || "test", password: $("#m-password").value || "test", binds: Math.min(10, +$("#m-max_binds").value || 10) });
});

// ======================================================================= boot
(async function boot() {
  const st = await act(() => api("GET", "/api/state"));
  if (st) { S = Object.assign(S, st); defaults = st.defaults; templates = st.templates || []; }
  renderTemplates();
  const mc = store.get("mockcfg");
  if (mc) for (const k of MOCK_FIELDS) { const el = $("#m-" + k); if (el && k in mc) { if (el.type === "checkbox") el.checked = mc[k]; else el.value = mc[k]; } }
  fillConnSelects();
  const saved = store.get("sendform");
  if (saved) applyValues(saved); else setSource("single");
  if (store.get("s-conn")) $("#s-conn").value = store.get("s-conn");
  if (store.get("dash-conn")) $("#dash-conn").value = store.get("dash-conn");
  onSendFormChange();
  connectWS();
  const v = location.hash.slice(1);
  show($("#v-" + v) ? v : "dashboard");
  window.addEventListener("resize", () => render([]));
})();

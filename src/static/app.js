"use strict";

// ================================================================ 共通

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function fmtBytes(n, digits) {
  if (n == null || Number.isNaN(n)) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(digits ?? (n < 10 ? 2 : 1))} ${u[i]}`;
}
const fmtRate = (n) => `${fmtBytes(n)}/秒`;
const fmtBits = (n) => {
  if (n == null) return "-";
  const b = n * 8;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} Mbps`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(0)} Kbps`;
  return `${Math.round(b)} bps`;
};
const pct = (n, d = 0) => (n == null ? "-" : `${Number(n).toFixed(d)}%`);
function fmtUptime(sec) {
  sec = Math.floor(sec || 0);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${d}:${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
const ipKey = (ip) => (ip || "").split(".").map((x) => x.padStart(3, "0")).join(".");

// ================================================================ 表示設定（ブラウザごとに保存）

const PREF_DEFAULTS = { wall: "black", theme: "auto", clear: 50, refract: true, accent: "", range: 90, size: "normal", start: "home", nav: "open" };
const ACCENTS = [["", "既定"], ["#0a64d6", "ブルー"], ["#7c3aed", "パープル"], ["#0f9d58", "グリーン"], ["#e8710a", "オレンジ"], ["#db2777", "ピンク"], ["#0891b2", "ティール"]];
const WALLS = window.HSWall.WALLS;
const isDark = () => state.prefs.theme === "dark" || (state.prefs.theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
const RANGES = [[30, "1 分"], [90, "3 分"], [180, "6 分"]];

// 以前の「ガラスの濃さ」（3 段階）を透明度（%）に読み替える
const GLASS_TO_CLEAR = { strong: 75, normal: 50, subtle: 20 };
function migratePrefs(p) {
  if (p.clear == null && p.glass in GLASS_TO_CLEAR) p.clear = GLASS_TO_CLEAR[p.glass];
  delete p.glass;
  p.clear = Math.max(0, Math.min(100, Math.round(Number(p.clear ?? 50))));
  return p;
}
function loadPrefs() {
  try { return migratePrefs({ ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem("hs-prefs") || "{}") }); } catch { return { ...PREF_DEFAULTS }; }
}
// 透明度 0〜100% → ガラスの色の濃さ・ぼかし・彩度。
// 透明にするほど色もぼかしも弱くして、100% では壁紙がほぼそのまま見えるようにする
function applyClear() {
  const c = state.prefs.clear / 100, root = document.documentElement.style;
  // OS で「透明度を下げる」がオンならほぼ不透明にする
  const reduce = matchMedia("(prefers-reduced-transparency: reduce)").matches;
  const a = reduce ? .9 : (isDark() ? .62 : .78) * (1 - c) + .02;
  root.setProperty("--glass-a", a.toFixed(3));
  root.setProperty("--blur", `${reduce ? 30 : Math.round(32 * (1 - c) ** 1.2)}px`);
  root.setProperty("--sat", `${Math.round(100 + 85 * (1 - c))}%`);
}
// 表示設定はアカウントごとにサーバーへ保存し、どの端末でも同じにする。
// ブラウザにも控えを置き、次に開いたときの最初の表示に使う。メニューの開閉（nav）だけは端末ごと。
let prefsTimer = 0;
function savePrefs({ sync = true } = {}) {
  try { localStorage.setItem("hs-prefs", JSON.stringify(state.prefs)); } catch { /* 保存できなくても表示は続ける */ }
  if (!sync) return;
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => {
    const { nav, ...shared } = state.prefs;
    post("/api/prefs", { prefs: shared }).catch((err) => toast(`表示設定を保存できませんでした: ${err.message}`, true));
  }, 400);
}
async function loadAccountPrefs() {
  try {
    const res = await fetch("/api/prefs", { cache: "no-store" });
    if (!res.ok) return;
    const { prefs } = await res.json();
    if (!prefs || !Object.keys(prefs).length) { savePrefs(); return; } // 初回はこの端末の設定をアカウントに登録
    const before = JSON.stringify(state.prefs);
    state.prefs = migratePrefs({ ...state.prefs, ...prefs });
    if (JSON.stringify(state.prefs) === before) return;
    savePrefs({ sync: false });
    applyPrefs();
    if (state.data) buildPage();
  } catch { /* 取れなければこの端末の控えのまま */ }
}
function applyPrefs() {
  const p = state.prefs, root = document.documentElement;
  if (p.theme === "auto") delete root.dataset.theme; else root.dataset.theme = p.theme;
  applyClear();
  window.HSWall.apply(p.wall, isDark());
  root.dataset.size = p.size;
  root.classList.toggle("refract", p.refract && !!window.chrome && !matchMedia("(prefers-reduced-transparency: reduce)").matches);
  if (p.accent) root.style.setProperty("--accent", p.accent); else root.style.removeProperty("--accent");
  $("app").classList.toggle("collapsed", p.nav === "closed");
}

// ================================================================ 状態

const state = {
  data: null,
  page: null,
  prefs: loadPrefs(),
  who: { can_edit: false, who: "", credential: "" },
  sort: {},
  selected: {},
  search: "",
  perfRes: "cpu",
  cpuView: "total",
  busy: false,
  histRange: "24h",
  hist: null,
  site: null,
  alertHistory: [],
  auditRows: [],
};
const sec = (k) => state.data?.sections?.[k] || {};

// ================================================================ 通信

async function post(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "home-status" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { location.replace("/login"); throw new Error(data.error || "ログインが必要です"); }
  if (res.status === 409) { location.reload(); throw new Error(data.error || "状態が変わりました"); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Pi の再起動: 実行後は戻ってくるまで画面に「再起動中」と出す
async function rebootPi() {
  const res = await act("pi.reboot", {}, { confirm: { title: "Pi を再起動しますか？", message: "1〜2 分ほど、このサイト・Nextcloud などすべてが止まります。\n再起動が終わると自動で再接続します。", ok: "再起動", danger: true } });
  if (!res) return;
  setLive("off", "Pi を再起動中…");
  const started = Date.now();
  // 一度落ちてから戻ってきたらページを読み直す
  let wentDown = false;
  const timer = setInterval(async () => {
    try {
      const r = await fetch("/api/whoami", { cache: "no-store" });
      // ログイン状態は再起動で消えるので、戻ったらパスキーでログインし直す
      if (r.status === 401 && wentDown) { clearInterval(timer); location.replace("/login"); return; }
      if (!r.ok) throw new Error();
      if (wentDown || Date.now() - started > 240000) { clearInterval(timer); location.reload(); }
    } catch { wentDown = true; }
  }, 4000);
}

async function act(action, params = {}, { confirm: c, quiet = false } = {}) {
  if (c && !(await confirmDialog(c))) return null;
  state.busy = true;
  renderCommands();
  try {
    const res = await post("/api/action", { action, params });
    if (!quiet) toast(res.message || "完了しました");
    return res;
  } catch (err) {
    toast(err.message, true);
    return null;
  } finally {
    state.busy = false;
    renderCommands();
  }
}

function toast(message, isError = false) {
  const el = document.createElement("div");
  el.className = `toast${isError ? " error" : ""}`;
  el.textContent = message;
  $("toasts").append(el);
  setTimeout(() => el.remove(), isError ? 8000 : 4200);
}

// ================================================================ ダイアログ

let dialogResolve = null;
function openDialog({ title, body, buttons, wide = false }) {
  $("dialog").classList.toggle("wide", wide);
  $("dialog-title").textContent = title;
  const b = $("dialog-body");
  b.innerHTML = "";
  if (typeof body === "string") b.innerHTML = body; else b.append(body);
  const bar = $("dialog-buttons");
  bar.innerHTML = "";
  for (const btn of buttons) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `btn ${btn.cls || ""}`;
    el.textContent = btn.label;
    el.addEventListener("click", btn.onClick);
    bar.append(el);
  }
  $("dialog-backdrop").hidden = false;
  (bar.querySelector(".primary") || bar.lastElementChild)?.focus();
}
function closeDialog(result = false) {
  $("dialog-backdrop").hidden = true;
  const r = dialogResolve; dialogResolve = null;
  r?.(result);
}
function confirmDialog({ title, message, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    dialogResolve = resolve;
    openDialog({
      title,
      body: `<p class="msg">${esc(message).replace(/\n/g, "<br>")}</p>`,
      buttons: [
        { label: "キャンセル", onClick: () => closeDialog(false) },
        { label: ok, cls: danger ? "danger" : "primary", onClick: () => closeDialog(true) },
      ],
    });
  });
}
async function showLogs(source, label) {
  const res = await act("logs.view", { source, lines: 200 }, { quiet: true });
  if (!res) return;
  openDialog({
    title: res.message || `${label} のログ`,
    wide: true,
    body: `<pre class="log" id="log-pre">${esc(res.data?.text || "")}</pre>`,
    buttons: [
      { label: "最新に更新", onClick: () => { closeDialog(); showLogs(source, label); } },
      { label: "閉じる", cls: "primary", onClick: () => closeDialog() },
    ],
  });
  const pre = $("log-pre");
  pre.scrollTop = pre.scrollHeight;
}

// ================================================================ グラフ

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return null;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function hexA(color, a) {
  const c = color.trim();
  if (c.startsWith("#") && c.length === 7) {
    const n = parseInt(c.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  return c;
}

function niceMax(m) {
  if (m <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(m)));
  for (const k of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * 1.08 <= k * p) return k * p;
  return 10 * p;
}

// series: [{values, color, fill?}]; stacked で積み上げ。描画に使った上限値を返す
function drawGraph(canvas, series, { max = null, stacked = false, points = state.prefs.range, grid = true } = {}) {
  if (!canvas) return 0;
  const g = setupCanvas(canvas);
  if (!g) return 0;
  const { ctx, w, h } = g;
  if (grid) {
    ctx.strokeStyle = css("--graph-grid"); ctx.lineWidth = 1;
    for (let i = 1; i < 10; i++) { const x = Math.round((w * i) / 10) + .5; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
    for (let i = 1; i < 5; i++) { const y = Math.round((h * i) / 5) + .5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  }
  const vals = series.map((s) => s.values.slice(-points));
  let top = max;
  if (top == null) {
    let m = 0;
    const len = Math.max(0, ...vals.map((v) => v.length));
    for (let i = 0; i < len; i++) {
      if (stacked) m = Math.max(m, vals.reduce((a, v) => a + (v[i] || 0), 0));
      else for (const v of vals) if (v[i] != null) m = Math.max(m, v[i]);
    }
    top = niceMax(m);
  }
  const step = w / Math.max(points - 1, 1);
  const base = new Array(points).fill(0);
  const sy = (n) => h - (Math.min(n, top) / top) * (h - 3) - 1.5;
  series.forEach((s, si) => {
    const v = vals[si];
    const off = points - v.length;
    const pts = v.map((val, i) => {
      const idx = off + i;
      const b = stacked ? base[idx] : 0;
      const y = val == null ? null : b + val;
      if (stacked && val != null) base[idx] = y;
      return { x: idx * step, y: y == null ? null : sy(y), b: sy(b) };
    });
    const fillColor = s.fill ?? hexA(s.color, .18);
    let seg = [];
    const flush = () => {
      if (seg.length > 1 && fillColor) {
        ctx.beginPath(); ctx.moveTo(seg[0].x, seg[0].b);
        for (const p of seg) ctx.lineTo(p.x, p.y);
        for (let i = seg.length - 1; i >= 0; i--) ctx.lineTo(seg[i].x, seg[i].b);
        ctx.closePath(); ctx.fillStyle = fillColor; ctx.fill();
      }
      seg = [];
    };
    for (const p of pts) { if (p.y == null) flush(); else seg.push(p); }
    flush();
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.6; ctx.lineJoin = "round";
    ctx.beginPath();
    let pen = false;
    for (const p of pts) {
      if (p.y == null) { pen = false; continue; }
      if (pen) ctx.lineTo(p.x, p.y); else { ctx.moveTo(p.x, p.y); pen = true; }
    }
    ctx.stroke();
  });
  return top;
}

const rangeLabel = (points, seconds = 2) => {
  const s = points * seconds;
  return s >= 60 ? `${Math.round(s / 60)} 分` : `${s} 秒`;
};

function graphBlock(id, { left, right, small = false, points = state.prefs.range, seconds = 2 } = {}) {
  return `<div class="graph-wrap">
    <div class="gl"><span>${esc(left || "")}</span><span id="${id}-max">${esc(right || "")}</span></div>
    <canvas class="graph${small ? " small" : ""}" id="${id}"></canvas>
    <div class="gb"><span>${rangeLabel(points, seconds)}</span><span>0</span></div>
  </div>`;
}

// ================================================================ 表（並べ替え・検索・選択・ヒート表示）

function tableHTML(id) {
  return `<div class="card table-card"><div class="table-scroll"><table class="grid" id="${id}"><thead></thead><tbody></tbody></table></div>
    <p class="empty" id="${id}-empty" hidden>該当する項目がありません</p></div>`;
}

function renderTable(id, cfg, rows) {
  const table = $(id);
  if (!table) return;
  const sort = state.sort[id] || cfg.sort;
  const col = cfg.columns.find((c) => c.key === sort.key) || cfg.columns[0];
  const q = state.search.trim().toLowerCase();
  let list = rows.slice();
  if (q) list = list.filter((r) => cfg.columns.some((c) => String(c.text ? c.text(r) : c.get(r) ?? "").toLowerCase().includes(q)));
  list.sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    const cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""), "ja", { numeric: true });
    return sort.desc ? -cmp : cmp;
  });
  const head = `<tr>${cfg.columns.map((c) => `<th data-sort="${c.key}" class="${c.num ? "num " : ""}${c.opt ? "opt " : ""}${c.key === sort.key ? `sorted${sort.desc ? "" : " asc"}` : ""}">${
    c.total ? `<span class="total">${esc(c.total(rows))}</span>` : ""}${esc(c.label)}</th>`).join("")}</tr>`;
  if (table.tHead.innerHTML !== head) table.tHead.innerHTML = head;
  const selected = state.selected[id];
  table.tBodies[0].innerHTML = list.map((r) => {
    const key = String(cfg.key(r));
    return `<tr data-key="${esc(key)}" class="${key === selected ? "selected" : ""}">${cfg.columns.map((c) => {
      const content = c.html ? c.html(r) : esc(c.text ? c.text(r) : c.get(r));
      const heat = c.heat ? c.heat(r) : 0;
      const style = heat > 0.005 ? ` style="background:rgba(var(--heat),${Math.min(.55, .06 + heat * .5).toFixed(3)})"` : "";
      return `<td class="${c.num ? "num " : ""}${c.opt ? "opt " : ""}${c.cls || ""}"${style}>${content}</td>`;
    }).join("")}</tr>`;
  }).join("");
  $(`${id}-empty`).hidden = list.length > 0;
  if (!table.dataset.bound) {
    table.dataset.bound = "1";
    table.tHead.addEventListener("click", (e) => {
      const th = e.target.closest("[data-sort]");
      if (!th) return;
      const cur = state.sort[id] || cfg.sort;
      const c = cfg.columns.find((x) => x.key === th.dataset.sort);
      state.sort[id] = cur.key === c.key ? { key: c.key, desc: !cur.desc } : { key: c.key, desc: !!c.num };
      update();
    });
    table.tBodies[0].addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-key]");
      if (!tr) return;
      state.selected[id] = state.selected[id] === tr.dataset.key ? null : tr.dataset.key;
      update();
    });
    table.tBodies[0].addEventListener("dblclick", (e) => {
      const tr = e.target.closest("tr[data-key]");
      if (tr && cfg.onOpen) { state.selected[id] = tr.dataset.key; cfg.onOpen(); }
    });
  }
}

function selectedRow(id, rows, keyFn) {
  const k = state.selected[id];
  return k == null ? null : rows.find((r) => String(keyFn(r)) === k) || null;
}

// ================================================================ 部品

const kv = (items) => `<dl class="kv">${(items || []).map((i) => `<dt>${esc(i.label)}</dt><dd class="${i.level && i.level !== "ok" ? `lv-${i.level}` : ""}">${esc(i.value)}</dd>`).join("")}</dl>`;
const legend = (entries) => `<div class="legend">${entries.map(([c, t]) => `<span><i style="background:${c}"></i>${esc(t)}</span>`).join("")}</div>`;
const tile = (k, v, s = "", level = "") => `<div class="glass tile"><div class="k">${esc(k)}</div><div class="v ${level && level !== "ok" ? `lv-${level}` : ""}">${esc(v)}</div>${s ? `<div class="s">${esc(s)}</div>` : ""}</div>`;
const fill = (id, html) => { const el = $(id); if (el && el.innerHTML !== html) el.innerHTML = html; };
const text = (id, t) => { const el = $(id); if (el && el.textContent !== String(t)) el.textContent = t; };
const canEdit = () => state.who.can_edit && !state.busy;
const big = (items) => items.map(([k, v]) => `<div><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");

// ================================================================ パフォーマンス

function resources() {
  const pi = sec("pi"), net = sec("network");
  const h = pi.history || {}, nh = net.history || {};
  const cpu = pi.cpu || {}, mem = pi.memory || {};
  const util = (cpu.user ?? 0) + (cpu.system ?? 0);
  const list = [
    { id: "cpu", name: "CPU", value: `${pct(util)}  ${cpu.freq ? (cpu.freq / 1000).toFixed(2) + " GHz" : ""}`,
      spark: () => [{ values: (h.cpu_user || []).map((u, i) => u + ((h.cpu_system || [])[i] || 0)), color: css("--c-cpu") }], max: 100 },
    { id: "mem", name: "メモリ", value: `${fmtBytes(mem.used, 1)} / ${fmtBytes(mem.total, 1)} (${pct(mem.percent)})`,
      spark: () => [{ values: h.mem_used || [], color: css("--c-mem") }], max: mem.total },
  ];
  for (const d of pi.disks || []) {
    if (!d.dev) continue;
    list.push({ id: `disk:${d.dev}`, name: `ディスク ${d.label}`, value: `${d.model || d.path} · ${pct(d.busy)}`,
      spark: () => [{ values: h[`disk_${d.dev}_busy`] || [], color: css("--c-disk") }], max: 100 });
  }
  list.push(
    { id: "net", name: "イーサネット", value: `送信: ${fmtBits(net.net?.tx_rate)} 受信: ${fmtBits(net.net?.rx_rate)}`,
      spark: () => [{ values: nh.net_rx || [], color: css("--c-net") }, { values: nh.net_tx || [], color: css("--c-sys"), fill: "" }] },
    { id: "temp", name: "温度", value: `${(h.temp || []).at(-1)?.toFixed?.(1) ?? "-"} ℃`,
      spark: () => [{ values: h.temp || [], color: css("--c-temp") }], max: 90 },
    { id: "lat", name: "インターネット", value: `${(nh.internet_ms || []).filter((x) => x != null).at(-1)?.toFixed?.(0) ?? "-"} ms`,
      spark: () => [{ values: nh.internet_ms || [], color: css("--c-lat") }], slow: true },
  );
  return list;
}

const perfDetail = {
  cpu() {
    const cpu = sec("pi").cpu || {};
    return `<div class="detail-head"><h2>CPU</h2><span class="model">${esc(cpu.model || "")}</span></div>
      <div class="form-actions" style="justify-content:flex-start;margin-bottom:10px">
        <button class="choice ${state.cpuView === "total" ? "on" : ""}" type="button" data-cpuview="total">全体</button>
        <button class="choice ${state.cpuView === "cores" ? "on" : ""}" type="button" data-cpuview="cores">論理プロセッサ</button>
      </div>
      ${state.cpuView === "total"
        ? graphBlock("g-cpu", { left: "% 使用率", right: "100%" }) + legend([[css("--c-cpu"), "使用率"], [css("--c-sys"), "カーネル時間"]])
        : `<div class="cores">${(cpu.per_core || []).map((_, i) => `<div><div class="gl" style="font-size:.74rem;color:var(--text-2);padding-bottom:4px">CPU ${i}</div><canvas class="graph" id="g-core-${i}"></canvas></div>`).join("")}</div>`}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="cpu-big"></div><div id="cpu-kv"></div></div>`;
  },
  mem() {
    return `<div class="detail-head"><h2>メモリ</h2><span class="model" id="mem-total"></span></div>
      ${graphBlock("g-mem", { left: "メモリ使用量", right: "" })}
      <div style="font-size:.78rem;color:var(--text-2)">メモリの構成</div>
      <div class="composition" id="mem-comp"></div>
      ${legend([[css("--c-mem"), "使用中"], [hexA(css("--c-mem"), .45), "キャッシュ"], ["rgba(127,127,127,.25)", "空き"]])}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="mem-big"></div><div id="mem-kv"></div></div>`;
  },
  disk(d) {
    return `<div class="detail-head"><h2>ディスク ${esc(d.label)}</h2><span class="model">${esc(d.model || "")}</span></div>
      ${graphBlock("g-disk-busy", { left: "アクティブな時間", right: "100%" })}
      ${graphBlock("g-disk-io", { left: "ディスク転送速度", right: "", small: true })}
      ${legend([[css("--c-disk"), "読み取り"], [css("--c-sys"), "書き込み"]])}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="disk-big"></div><div id="disk-kv"></div></div>`;
  },
  net() {
    return `<div class="detail-head"><h2>ネットワーク</h2><span class="model" id="net-iface"></span></div>
      ${graphBlock("g-net", { left: "スループット", right: "" })}
      ${legend([[css("--c-net"), "受信"], [css("--c-sys"), "送信"]])}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="net-big"></div><div id="net-kv"></div></div>`;
  },
  temp() {
    return `<div class="detail-head"><h2>温度</h2><span class="model">SoC</span></div>
      ${graphBlock("g-temp", { left: "℃", right: "90 ℃" })}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="temp-big"></div><div id="temp-kv"></div></div>`;
  },
  lat() {
    return `<div class="detail-head"><h2>インターネット</h2><span class="model">応答時間</span></div>
      ${graphBlock("g-lat", { left: "ms", right: "", points: 180, seconds: 10 })}
      ${legend([[css("--c-lat"), "インターネット"], [css("--c-disk"), "ルーター"]])}
      <div class="bigstats" style="margin-top:14px"><div class="big" id="lat-big"></div><div id="lat-kv"></div></div>`;
  },
};

function updatePerfDetail() {
  const pi = sec("pi"), net = sec("network"), h = pi.history || {}, nh = net.history || {};
  const id = state.perfRes;
  if (id === "cpu") {
    const cpu = pi.cpu || {};
    if (state.cpuView === "total") {
      drawGraph($("g-cpu"), [
        { values: (h.cpu_user || []).map((u, i) => u + ((h.cpu_system || [])[i] || 0)), color: css("--c-cpu") },
        { values: h.cpu_system || [], color: css("--c-sys"), fill: hexA(css("--c-sys"), .15) },
      ], { max: 100 });
    } else {
      (cpu.per_core || []).forEach((_, i) => drawGraph($(`g-core-${i}`), [{ values: (h.cores || []).map((c) => c?.[i]), color: css("--c-cpu") }], { max: 100, grid: false }));
    }
    fill("cpu-big", big([["使用率", pct((cpu.user ?? 0) + (cpu.system ?? 0))], ["速度", cpu.freq ? `${(cpu.freq / 1000).toFixed(2)} GHz` : "-"],
      ["プロセス", cpu.processes ?? "-"], ["スレッド", cpu.threads ?? "-"], ["稼働時間", fmtUptime(cpu.uptime)]]));
    const temp = (h.temp || []).at(-1);
    fill("cpu-kv", kv([
      { label: "最大速度", value: cpu.max_mhz ? `${(cpu.max_mhz / 1000).toFixed(2)} GHz` : "-" },
      { label: "最小速度", value: cpu.min_mhz ? `${(cpu.min_mhz / 1000).toFixed(2)} GHz` : "-" },
      { label: "論理プロセッサ数", value: cpu.cores },
      { label: "ユーザー / カーネル", value: `${pct(cpu.user, 1)} / ${pct(cpu.system, 1)}` },
      { label: "温度", value: temp != null ? `${temp.toFixed(1)} ℃` : "-", level: temp > 80 ? "error" : temp > 70 ? "warn" : "" },
      ...(pi.items || []).filter((i) => ["負荷平均", "電圧低下"].includes(i.label)),
    ]));
  } else if (id === "mem") {
    const m = pi.memory || {};
    drawGraph($("g-mem"), [{ values: h.mem_used || [], color: css("--c-mem") }], { max: m.total });
    text("g-mem-max", fmtBytes(m.total, 1));
    text("mem-total", fmtBytes(m.total, 1));
    const used = m.used || 0, cache = Math.min(m.cached || 0, (m.total || 0) - used), free = Math.max(0, (m.total || 0) - used - cache);
    fill("mem-comp", [[used, css("--c-mem")], [cache, hexA(css("--c-mem"), .45)], [free, "rgba(127,127,127,.18)"]]
      .map(([v, c]) => `<span style="width:${(100 * v / (m.total || 1)).toFixed(2)}%;background:${c}"></span>`).join(""));
    fill("mem-big", big([["使用中", fmtBytes(m.used, 1)], ["利用可能", fmtBytes(m.available, 1)], ["キャッシュ済み", fmtBytes(m.cached, 1)], ["使用率", pct(m.percent)]]));
    fill("mem-kv", kv([{ label: "空き", value: fmtBytes(m.free, 1) }, { label: "ダーティ", value: fmtBytes(m.dirty, 1) },
      { label: "スワップ", value: m.swap_total ? `${fmtBytes(m.swap_used, 1)} / ${fmtBytes(m.swap_total, 1)}` : "なし" }]));
  } else if (id.startsWith("disk:")) {
    const d = (pi.disks || []).find((x) => `disk:${x.dev}` === id);
    if (!d) return;
    drawGraph($("g-disk-busy"), [{ values: h[`disk_${d.dev}_busy`] || [], color: css("--c-disk") }], { max: 100 });
    const top = drawGraph($("g-disk-io"), [{ values: h[`disk_${d.dev}_read`] || [], color: css("--c-disk") }, { values: h[`disk_${d.dev}_write`] || [], color: css("--c-sys"), fill: "" }]);
    text("g-disk-io-max", fmtRate(top));
    fill("disk-big", big([["アクティブな時間", pct(d.busy)], ["読み取り速度", fmtRate(d.read)], ["書き込み速度", fmtRate(d.write)]]));
    fill("disk-kv", kv([{ label: "容量", value: d.total ? fmtBytes(d.total, 1) : "-" }, { label: "使用済み", value: d.text, level: d.level },
      { label: "マウント", value: d.path }, { label: "デバイス", value: d.dev || "-" }, { label: "モデル", value: d.model || "-" }]));
  } else if (id === "net") {
    const n = net.net || {};
    const top = drawGraph($("g-net"), [{ values: nh.net_rx || [], color: css("--c-net") }, { values: nh.net_tx || [], color: css("--c-sys"), fill: "" }]);
    text("g-net-max", fmtBits(top));
    text("net-iface", net.iface || "");
    fill("net-big", big([["送信", fmtBits(n.tx_rate)], ["受信", fmtBits(n.rx_rate)]]));
    fill("net-kv", kv([{ label: "送信合計", value: fmtBytes(n.tx_total, 1) }, { label: "受信合計", value: fmtBytes(n.rx_total, 1) }, ...(net.items || [])]));
  } else if (id === "temp") {
    const t = (h.temp || []).filter((x) => x != null);
    drawGraph($("g-temp"), [{ values: h.temp || [], color: css("--c-temp") }], { max: 90 });
    fill("temp-big", big([["現在", t.length ? `${t.at(-1).toFixed(1)} ℃` : "-"], ["最高", t.length ? `${Math.max(...t).toFixed(1)} ℃` : "-"], ["最低", t.length ? `${Math.min(...t).toFixed(1)} ℃` : "-"]]));
    fill("temp-kv", kv((pi.items || []).filter((i) => ["CPU 温度", "電圧低下"].includes(i.label)).concat([{ label: "注意の目安", value: "70 ℃ / 80 ℃" }])));
  } else if (id === "lat") {
    const top = drawGraph($("g-lat"), [{ values: nh.internet_ms || [], color: css("--c-lat") }, { values: nh.router_ms || [], color: css("--c-disk"), fill: "" }], { points: 180 });
    text("g-lat-max", `${Math.round(top)} ms`);
    const v = (nh.internet_ms || []).filter((x) => x != null), r = (nh.router_ms || []).filter((x) => x != null);
    const avg = (a) => (a.length ? (a.reduce((x, y) => x + y, 0) / a.length).toFixed(1) : "-");
    fill("lat-big", big([["インターネット", v.length ? `${v.at(-1).toFixed(1)} ms` : "-"], ["ルーター", r.length ? `${r.at(-1).toFixed(1)} ms` : "-"], ["平均（30 分）", `${avg(v)} ms`]]));
    fill("lat-kv", kv(net.items || []));
  }
}

// ================================================================ ページ定義

const PAGES = [
  {
    key: "home", label: "ホーム", icon: "dashboard", level: () => homeLevel(),
    sub: () => `${sec("pi").items?.find((i) => i.label === "ホスト名")?.value || "Raspberry Pi"} · 起動から ${fmtUptime(sec("pi").cpu?.uptime)}`,
    build: () => homeBuild(),
    afterBuild: () => homeAfterBuild(),
    update: () => homeUpdate(),
  },
  {
    key: "processes", label: "プロセス", icon: "processes", section: "pi",
    sub: () => `${sec("pi").cpu?.processes ?? "-"} 個のプロセス（CPU の高い順に 80 件）`,
    build: () => tableHTML("t-proc"),
    table: {
      id: "t-proc", rows: () => sec("pi").processes || [], key: (r) => r.pid, sort: { key: "cpu", desc: true },
      columns: [
        { key: "name", label: "名前", get: (r) => r.name },
        { key: "cpu", label: "CPU", num: true, get: (r) => r.cpu, text: (r) => `${r.cpu.toFixed(1)}%`, heat: (r) => r.cpu / 100,
          total: () => pct((sec("pi").cpu?.user ?? 0) + (sec("pi").cpu?.system ?? 0)) },
        { key: "rss", label: "メモリ", num: true, get: (r) => r.rss, text: (r) => fmtBytes(r.rss, 1), heat: (r) => (r.rss / (sec("pi").memory?.total || 1)) * 4,
          total: () => pct(sec("pi").memory?.percent) },
        { key: "threads", label: "スレッド", num: true, opt: true, get: (r) => r.threads },
        { key: "pid", label: "PID", num: true, cls: "muted", get: (r) => r.pid },
        { key: "user", label: "ユーザー", cls: "muted", opt: true, get: (r) => r.user },
      ],
    },
  },
  {
    key: "performance", label: "パフォーマンス", icon: "performance", section: "pi",
    sub: () => `${sec("pi").cpu?.model || ""} · 稼働時間 ${fmtUptime(sec("pi").cpu?.uptime)}`,
    build: () => `<div class="perf"><div class="res-list" id="res-list"></div><div class="card" id="perf-detail"></div></div>`,
    update() {
      const list = resources();
      if (!list.some((r) => r.id === state.perfRes)) state.perfRes = "cpu";
      const box = $("res-list");
      const sig = list.map((r) => r.id).join();
      if (box.dataset.sig !== sig) {
        box.dataset.sig = sig;
        box.innerHTML = list.map((r) => {
          const k = r.id.replace(":", "-");
          return `<button class="res" type="button" data-res="${r.id}"><canvas id="sp-${k}"></canvas><div><div class="rn">${esc(r.name)}</div><div class="rv" id="rv-${k}"></div></div></button>`;
        }).join("");
        box.onclick = (e) => { const b = e.target.closest("[data-res]"); if (b) { state.perfRes = b.dataset.res; buildPerfDetail(); update(); } };
      }
      for (const r of list) {
        const k = r.id.replace(":", "-");
        box.querySelector(`[data-res="${r.id}"]`).classList.toggle("active", r.id === state.perfRes);
        text(`rv-${k}`, r.value);
        drawGraph($(`sp-${k}`), r.spark(), { max: r.max ?? null, points: r.slow ? 180 : state.prefs.range, grid: false });
      }
      if ($("perf-detail").dataset.res !== state.perfRes) buildPerfDetail();
      updatePerfDetail();
    },
  },
  {
    key: "history", label: "履歴", icon: "clock",
    sub: () => "CPU・温度・通信量・応答時間などの長期の推移",
    build: () => historyBuild(),
    afterBuild: () => historyAfterBuild(),
    update: () => historyUpdate(),
  },
  {
    key: "network", label: "ネットワーク", icon: "network", section: "network",
    sub: () => `LAN の端末 ${sec("network").devices?.length ?? 0} 台 · ${sec("network").summary || ""}`,
    commands() {
      const row = selectedRow("t-net", sec("network").devices || [], (r) => r.ip);
      return [
        { label: "LAN を再スキャン", icon: "scan", disabled: !canEdit(), run: () => act("network.scan") },
        ...lanCommands(row),
      ];
    },
    build: () => `<div class="tiles" id="net-tiles"></div>
      <div class="grid-2"><div class="stack">${tableHTML("t-net")}</div>
      <div class="stack"><div class="card"><h2>${icon("network")}通信量</h2>${graphBlock("g-net2", { left: "スループット", small: true })}
        ${legend([[css("--c-net"), "受信"], [css("--c-sys"), "送信"]])}</div>
        <div class="card"><h2>${icon("performance")}応答時間（直近 30 分）</h2>${graphBlock("g-lat2", { left: "ms", small: true, points: 180, seconds: 10 })}
        ${legend([[css("--c-lat"), "インターネット"], [css("--c-disk"), "ルーター"]])}</div>
        </div></div>`,
    update() {
      const s = sec("network"), nh = s.history || {}, n = s.net || {};
      const it = Object.fromEntries((s.items || []).map((i) => [i.label.replace(/ \(.*\)$/, ""), i]));
      fill("net-tiles", [
        tile("ルーター", it["ルーター"]?.value || "-", "", it["ルーター"]?.level),
        tile("インターネット", it["インターネット"]?.value || "-", s.internet_host || "", it["インターネット"]?.level),
        tile("DNS", it["DNS"]?.value || "-", "", it["DNS"]?.level),
        tile(`インターフェース (${s.iface || "-"})`, it["インターフェース"]?.value || "-", `受信 ${fmtBits(n.rx_rate)} · 送信 ${fmtBits(n.tx_rate)}`),
      ].join(""));
      text("g-net2-max", fmtBits(drawGraph($("g-net2"), [{ values: nh.net_rx || [], color: css("--c-net") }, { values: nh.net_tx || [], color: css("--c-sys"), fill: "" }])));
      text("g-lat2-max", `${Math.round(drawGraph($("g-lat2"), [{ values: nh.internet_ms || [], color: css("--c-lat") }, { values: nh.router_ms || [], color: css("--c-disk"), fill: "" }], { points: 180 }))} ms`);
    },
    table: {
      id: "t-net", rows: () => sec("network").devices || [], key: (r) => r.ip, sort: { key: "ip", desc: false },
      columns: [
        { key: "name", label: "名前", get: (r) => lanName(r), html: (r) => `<span class="cell-name"><span class="dot ${r.state === "REACHABLE" ? "" : "off"}"></span>${esc(lanName(r))}</span>` },
        { key: "ip", label: "IP アドレス", get: (r) => ipKey(r.ip), text: (r) => r.ip },
        { key: "mac", label: "MAC アドレス", cls: "mono", opt: true, get: (r) => r.mac },
        { key: "state", label: "状態", get: (r) => r.state, text: (r) => ({ REACHABLE: "応答あり", STALE: "最近通信", DELAY: "確認中", PROBE: "確認中" }[r.state] || r.state) },
        { key: "note", label: "備考", get: (r) => noteFor(r), html: (r) => noteFor(r) },
      ],
    },
  },
  {
    key: "tailscale", label: "Tailscale", icon: "tailscale", section: "tailscale",
    sub: () => `${sec("tailscale").items?.find((i) => i.label === "オンライン")?.value || "-"} がオンライン`,
    commands: () => [
      { label: "Serve を追加", icon: "plus", disabled: !canEdit(), run: () => serveAddDialog() },
      { label: "ログ", icon: "log", disabled: !canEdit(), run: () => showLogs("tailscaled", "tailscaled") },
    ],
    build: () => `
      <div class="glass hero"><div class="state"><div class="orb on" id="ts-orb">${icon("tailscale")}</div>
        <div><div class="big-label" id="ts-state">-</div><div class="small" id="ts-sub"></div></div></div>
        <label class="switch" style="margin-left:auto" title="Pi の Tailscale 接続"><input type="checkbox" id="ts-switch"><span></span></label></div>
      <div class="grid-2"><div class="stack">${tableHTML("t-ts")}</div>
        <div class="stack"><div class="card"><h2>${icon("lock")}HTTPS 公開（Serve）</h2><div id="ts-serve"></div>
          <p class="note">Tailnet 内（自分の端末）からだけ見られます。インターネットには公開されません。</p></div>
          <div class="card"><h2>${icon("tailscale")}このマシン</h2><div id="ts-kv"></div></div></div></div>`,
    afterBuild() {
      $("ts-switch").addEventListener("change", async (e) => {
        const running = e.target.checked;
        e.target.checked = !running;
        await act("tailscale.set_running", { running }, {
          confirm: running
            ? { title: "Tailscale をオンにしますか？", message: "Pi を Tailscale に接続します。", ok: "オンにする" }
            : { title: "Tailscale をオフにしますか？", message: "Pi が Tailscale から切断され、Nextcloud など Tailscale 経由のアクセスが切れます。\nこの画面はオン/オフだけの表示になり、この URL からいつでもオンに戻せます。", ok: "オフにする", danger: true },
        });
      });
      $("ts-serve").addEventListener("click", (e) => {
        const b = e.target.closest("[data-serve]");
        if (!b) return;
        const port = Number(b.dataset.serve);
        act("tailscale.serve.remove", { port }, { confirm: { title: `https ${port} の公開をやめますか？`, message: "その URL からアクセスできなくなります。", ok: "削除", danger: true } });
      });
    },
    update() {
      const s = sec("tailscale");
      const it = Object.fromEntries((s.items || []).map((i) => [i.label, i.value]));
      text("ts-state", s.running ? "接続中" : "停止中");
      $("ts-orb").className = `orb ${s.running ? "on" : "off"}`;
      text("ts-sub", `${it["MagicDNS 名"] || ""} · ${it.IP || ""}`);
      const sw = $("ts-switch");
      if (document.activeElement !== sw) sw.checked = !!s.running;
      sw.disabled = !canEdit();
      fill("ts-serve", (s.serve || []).length ? `<div class="list-rows">${s.serve.map((m) => `<div class="list-row"><div class="grow"><div>https :${esc(m.port)}${esc(m.path)}</div><div class="sub">→ ${esc(m.target)}</div></div>
        ${m.target.includes(":8080") ? '<span class="tag">Nextcloud</span>' : m.target.includes(":8092") ? '<span class="tag">このサイト（転送）</span>' : ""}
        <button class="btn small danger" type="button" data-serve="${m.port}" ${canEdit() ? "" : "disabled"}>削除</button></div>`).join("")}</div>` : `<p class="note">公開中のものはありません</p>`);
      fill("ts-kv", kv(s.items));
    },
    table: {
      id: "t-ts", rows: () => sec("tailscale").peers || [], key: (r) => r.ip, sort: { key: "online", desc: true },
      columns: [
        { key: "name", label: "名前", get: (r) => r.name, html: (r) => `<span class="cell-name"><span class="dot ${r.online ? "" : "off"}"></span>${esc(r.name)}</span>` },
        { key: "ip", label: "IP", cls: "mono", opt: true, get: (r) => r.ip },
        { key: "os", label: "OS", get: (r) => r.os },
        { key: "online", label: "状態", get: (r) => (r.online ? 1 : 0), text: (r) => (r.online ? "オンライン" : r.last_seen || "オフライン") },
        { key: "direct", label: "経路", opt: true, get: (r) => (r.online ? (r.direct ? "直接" : `中継 ${r.relay || ""}`) : "-") },
        { key: "rx", label: "受信", num: true, opt: true, get: (r) => r.rx, text: (r) => fmtBytes(r.rx, 1) },
        { key: "tx", label: "送信", num: true, opt: true, get: (r) => r.tx, text: (r) => fmtBytes(r.tx, 1) },
      ],
    },
  },
  {
    key: "nextcloud", label: "Nextcloud", icon: "nextcloud", section: "nextcloud",
    sub: () => sec("nextcloud").summary || "",
    commands() {
      // 表で選んだコンテナ。選んでいなければ本体（app）を対象にする
      const cs = sec("nextcloud").containers || [];
      const row = selectedRow("t-nc", cs, (r) => r.name) || cs.find((r) => /-app-\d+$/.test(r.name)) || cs[0];
      const short = row ? row.name.replace(/^nextcloud-/, "").replace(/-\d+$/, "") : "";
      const maint = sec("nextcloud").maintenance;
      return [
        { label: row ? `ログ（${short}）` : "ログ", icon: "log", disabled: !canEdit() || !row, title: "表でコンテナを選ぶと切り替わります", run: () => showLogs(row.name, row.name) },
        { label: row ? `再起動（${short}）` : "再起動", icon: "restart", cls: "danger", disabled: !canEdit() || !row, title: "表でコンテナを選ぶと切り替わります",
          run: () => act("nextcloud.restart", { container: row.name }, { confirm: { title: `${row.name} を再起動しますか？`, message: "再起動中は Nextcloud が一時的に使えなくなります。", ok: "再起動", danger: true } }) },
        { label: maint ? "メンテナンスを解除" : "メンテナンス開始", icon: "wrench", disabled: !canEdit() || maint == null,
          run: () => act("nextcloud.maintenance", { on: !maint }, { confirm: maint
            ? { title: "メンテナンスモードを解除しますか？", message: "Nextcloud を通常どおり使えるようにします。", ok: "解除" }
            : { title: "メンテナンスモードにしますか？", message: "その間はユーザーが Nextcloud を使えなくなります。", ok: "開始", danger: true } }) },
        { label: "全体を再起動", icon: "power", cls: "danger", disabled: !canEdit(),
          run: () => act("service.restart", { id: "nextcloud" }, { confirm: { title: "Nextcloud の全コンテナを再起動しますか？", message: "1 分ほど Nextcloud が使えなくなります。", ok: "再起動", danger: true } }) },
      ];
    },
    build: () => `<div class="tiles" id="nc-tiles"></div>
      <div class="grid-2"><div class="stack">${tableHTML("t-nc")}</div>
        <div class="stack"><div class="card"><h2>${icon("performance")}応答時間（直近 30 分）</h2>${graphBlock("g-nc", { left: "ms", small: true, points: 180, seconds: 10 })}</div>
          </div></div>`,
    update() {
      const s = sec("nextcloud");
      const it = Object.fromEntries((s.items || []).map((i) => [i.label, i]));
      const cs = s.containers || [];
      fill("nc-tiles", [
        tile("Web 応答", it["Web 応答"]?.value || "-", it["応答時間"]?.value || "", it["Web 応答"]?.level),
        tile("メンテナンス", s.maintenance ? "ON" : "OFF", s.maintenance ? "ユーザーは利用できません" : "通常運転", s.maintenance ? "warn" : ""),
        tile("コンテナ", `${cs.filter((c) => c.state === "running").length} / ${cs.length}`, "稼働中", cs.some((c) => c.level === "error") ? "error" : ""),
        ...(s.items || []).filter((i) => i.label.startsWith("データ")).map((i) => tile(i.label, i.value)),
      ].join(""));
      text("g-nc-max", `${Math.round(drawGraph($("g-nc"), [{ values: s.history?.nextcloud_ms || [], color: css("--c-lat") }], { points: 180 }))} ms`);
    },
    table: {
      id: "t-nc", rows: () => sec("nextcloud").containers || [], key: (r) => r.name, sort: { key: "name", desc: false },
      columns: [
        { key: "name", label: "コンテナ", get: (r) => r.name, html: (r) => `<span class="cell-name"><span class="dot ${r.level}"></span>${esc(r.name)}</span>` },
        { key: "status", label: "状態", opt: true, get: (r) => r.status },
        { key: "cpu", label: "CPU", num: true, get: (r) => r.cpu ?? -1, text: (r) => pct(r.cpu, 1), heat: (r) => (r.cpu || 0) / 100 },
        { key: "mem", label: "メモリ", num: true, get: (r) => r.mem ?? -1, text: (r) => fmtBytes(r.mem, 1), heat: (r) => ((r.mem || 0) / (sec("pi").memory?.total || 1)) * 4 },
        { key: "net_in", label: "受信", num: true, opt: true, get: (r) => r.net_in ?? -1, text: (r) => fmtBytes(r.net_in, 1) },
        { key: "net_out", label: "送信", num: true, opt: true, get: (r) => r.net_out ?? -1, text: (r) => fmtBytes(r.net_out, 1) },
        { key: "pids", label: "PID", num: true, opt: true, get: (r) => r.pids ?? -1, text: (r) => r.pids ?? "-" },
        { key: "image", label: "イメージ", cls: "mono", opt: true, get: (r) => r.image },
      ],
    },
  },
  {
    key: "services", label: "サービス", icon: "services", section: "pi",
    sub: () => `${(sec("pi").services || []).filter((s) => s.state === "running").length} 個が実行中`,
    commands() {
      const row = selectedRow("t-svc", sec("pi").services || [], (r) => r.id);
      const logSrc = row && (row.id === "nextcloud" ? "nextcloud"
        : ["home-status", "cloudflared", "tailscaled", "docker"].includes(row.id) ? row.id : null);
      return [
        { label: "再起動", icon: "restart", cls: "danger", disabled: !canEdit() || !row?.restartable, title: "再起動できるサービスを選んでから",
          run: () => act("service.restart", { id: row.id }, { confirm: { title: `${row.name} を再起動しますか？`, message: row.id === "home-status" ? "このサイトが数秒止まり、自動で再接続します。" : "再起動中は一時的に使えなくなります。", ok: "再起動", danger: true } }) },
        { label: "ログ", icon: "log", disabled: !canEdit() || !logSrc, title: "サービスを選んでから", run: () => showLogs(logSrc, row.name) },
      ];
    },
    build: () => `<div class="card pi-card"><div><h2>${icon("power")}Raspberry Pi 本体</h2>
        <p class="note" id="pi-card-sub">再起動すると 1〜2 分ですべてのサービスが自動で戻ります。</p></div>
        <button class="btn danger" type="button" id="pi-reboot">${icon("power")}Pi を再起動</button></div>
      ${tableHTML("t-svc")}<p class="note">tailscaled・Docker などシステムのサービス単体の再起動とシャットダウンは、ここからはできません。</p>`,
    afterBuild: () => { $("pi-reboot").addEventListener("click", rebootPi); },
    table: {
      id: "t-svc", rows: () => sec("pi").services || [], key: (r) => r.id, sort: { key: "name", desc: false },
      onOpen: () => {},
      columns: [
        { key: "name", label: "名前", get: (r) => r.name, html: (r) => `<span class="cell-name"><span class="dot ${r.state === "running" ? "" : r.state === "absent" ? "off" : r.state === "degraded" ? "warn" : "error"}"></span>${esc(r.name)}</span>` },
        { key: "desc", label: "説明", opt: true, get: (r) => r.desc },
        { key: "state", label: "状態", get: (r) => r.state, text: (r) => ({ running: "実行中", stopped: "停止", degraded: "一部停止", absent: "未インストール", failed: "失敗", inactive: "停止" }[r.state] || r.state) },
        { key: "detail", label: "詳細", cls: "muted", opt: true, get: (r) => r.detail },
        { key: "pid", label: "PID", num: true, cls: "muted", get: (r) => r.pid ?? -1, text: (r) => r.pid ?? "-" },
        { key: "memory", label: "メモリ", num: true, get: (r) => r.memory ?? -1, text: (r) => (r.memory != null ? fmtBytes(r.memory, 1) : "-") },
        { key: "restartable", label: "操作", get: (r) => (r.restartable ? 1 : 0), html: (r) => (r.restartable ? '<span class="tag ok">再起動可</span>' : '<span class="tag">表示のみ</span>') },
      ],
    },
  },
  {
    key: "alerts", label: "アラート", icon: "bell",
    level: () => worstLevel((state.data?.alerts || []).map((a) => a.level)),
    sub: () => ((state.data?.alerts || []).length ? `${state.data.alerts.length} 件が発生中` : "発生中のアラートはありません") + " · 異常をスマホに通知します",
    build: () => alertsBuild(),
    afterBuild: () => alertsAfterBuild(),
    update: () => alertsUpdate(),
    table: {
      id: "t-alerts", rows: () => state.alertHistory || [], key: (r) => r.id, sort: { key: "started_at", desc: true },
      columns: [
        { key: "started_at", label: "発生", cls: "muted", get: (r) => r.started_at, text: (r) => fmtTs(r.started_at) },
        { key: "level", label: "種類", get: (r) => r.level, html: (r) => levelTag(r.level) },
        { key: "title", label: "内容", cls: "wrap", get: (r) => `${r.title} ${r.message}`, html: (r) => `${esc(r.title)}<div class="muted" style="font-size:.78rem">${esc(r.message)}</div>` },
        { key: "ended_at", label: "復旧", get: (r) => r.ended_at || Infinity, html: (r) => (r.ended_at ? (r.ended_at === r.started_at ? '<span class="tag">通知のみ</span>' : `${fmtTs(r.ended_at)}<div class="muted" style="font-size:.78rem">${fmtDur(r.ended_at - r.started_at)}</div>`) : '<span class="tag error">継続中</span>') },
      ],
    },
  },
  {
    key: "audit", label: "操作の記録", icon: "audit",
    sub: () => "いつ・どの端末から・何をしたか",
    build: () => auditBuild(),
    afterBuild: () => auditAfterBuild(),
    update: () => auditUpdate(),
    table: {
      id: "t-audit", rows: () => state.auditRows || [], key: (r) => r.id, sort: { key: "ts", desc: true },
      columns: [
        { key: "ts", label: "日時", cls: "muted", get: (r) => r.ts, text: (r) => fmtTs(r.ts, true) },
        { key: "action", label: "操作", get: (r) => actionLabel(r.action), html: (r) => `${r.ok ? "" : '<span class="tag error">失敗</span> '}${esc(actionLabel(r.action))}` },
        { key: "message", label: "結果", cls: "wrap", get: (r) => `${r.message} ${auditDetail(r)}`, html: (r) => `${esc(r.message)}${auditDetail(r) ? `<div class="muted mono" style="font-size:.74rem">${esc(auditDetail(r))}</div>` : ""}` },
        { key: "credential", label: "端末", get: (r) => r.credential || "-", html: (r) => esc(r.credential || "-") },
        { key: "ip", label: "IP", cls: "mono", opt: true, get: (r) => r.ip || "-" },
      ],
    },
  },
  {
    key: "settings", label: "設定", icon: "settings", bottom: true,
    sub: () => "表示の設定はアカウントに保存され、どの端末でも共通です",
    build: () => settingsHTML(),
    afterBuild: () => bindSettings(),
  },
  {
    key: "system", label: "システム情報", icon: "info", bottom: true,
    sub: () => `${sec("pi").items?.find((i) => i.label === "機種")?.value || "Raspberry Pi"} · ${sec("pi").items?.find((i) => i.label === "ホスト名")?.value || ""}`,
    build: () => `<div class="grid-2"><div class="stack">
        <div class="card"><h2>${icon("cores")}デバイス</h2><div id="sys-device"></div></div>
        ${systemExtraHTML()}
        <div class="card"><h2>${icon("check")}このサイトについて</h2><div id="about-kv"></div></div>
      </div><div class="stack">${systemBootsHTML()}<div class="stack" id="sys-versions"></div></div></div>`,
    afterBuild: () => systemExtraAfterBuild(),
    update() {
      fill("sys-device", kv(deviceItems()));
      fill("about-kv", kv(aboutItems()));
      fill("sys-versions", versionGroupsHTML());
      systemExtraUpdate();
    },
  },
];
const PAGE = Object.fromEntries(PAGES.map((p) => [p.key, p]));

function noteFor(r) {
  const notes = [];
  const fresh = lanNoteTags(r);
  if (fresh) notes.push(fresh);
  if (r.random_mac) notes.push('<span class="tag">ランダム MAC</span>');
  if (r.ip && r.ip === sec("network").gateway) notes.push('<span class="tag">ルーター</span>');
  return notes.join(" ");
}

function serveAddDialog() {
  openDialog({
    title: "Tailscale Serve を追加",
    body: `<form class="form" onsubmit="return false"><div class="form-group">
      <div class="field"><span class="lbl">公開ポート（https）</span><input type="number" id="sv-port" min="1" max="65535" placeholder="例: 10000"></div>
      <div class="field"><span class="lbl">転送先（127.0.0.1 のポート）</span><input type="number" id="sv-target" min="1" max="65535" placeholder="例: 3000"></div>
      <p class="note">https://&lt;この Pi の MagicDNS 名&gt;:公開ポート で、この Pi の 127.0.0.1:転送先 に届くようになります。Tailnet 内からだけ見られます。</p></div></form>`,
    buttons: [
      { label: "キャンセル", onClick: () => closeDialog() },
      { label: "追加", cls: "primary", onClick: async () => {
        const port = Number($("sv-port").value), target = Number($("sv-target").value);
        if (!port || !target) { toast("ポートを入力してください", true); return; }
        closeDialog();
        await act("tailscale.serve.add", { port, target });
      } },
    ],
  });
}

// ================================================================ 設定ページ

function choiceRow(label, key, options, hint = "") {
  return `<div class="field"><span class="lbl">${esc(label)}</span><div class="choices" data-pref="${key}">${options.map(([v, l]) => `<button type="button" class="choice ${String(state.prefs[key]) === String(v) ? "on" : ""}" data-value="${esc(v)}">${esc(l)}</button>`).join("")}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
}
function switchRow(label, key, hint = "") {
  return `<div class="field"><span class="lbl">${esc(label)}</span><label class="switch"><input type="checkbox" data-prefswitch="${key}" ${state.prefs[key] ? "checked" : ""}><span></span></label>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
}

function settingsHTML() {
  const pages = PAGES.filter((p) => !p.bottom).map((p) => [p.key, p.label]);
  return `<div class="grid-2"><div class="stack">
    <div class="card"><h2>${icon("settings")}表示</h2><div class="form">
      <div class="field"><span class="lbl">背景</span><div class="choices walls" data-pref="wall">${Object.entries(WALLS).map(([k, w]) => `<button type="button" class="wall-pick ${(WALLS[state.prefs.wall] ? state.prefs.wall : window.HSWall.DEFAULT) === k ? "on" : ""}" data-value="${k}"><i style="background-image:url(${window.HSWall.thumb(k, isDark())})"></i><span>${esc(w.label)}</span></button>`).join("")}</div></div>
      ${choiceRow("テーマ", "theme", [["auto", "システムに合わせる"], ["light", "ライト"], ["dark", "ダーク"]])}
      <div class="field"><span class="lbl">背景の透明度</span><div class="range-row"><input type="range" id="pref-clear" min="0" max="100" step="5" value="${state.prefs.clear}" aria-label="背景の透明度"><output id="pref-clear-v">${state.prefs.clear}%</output></div><div class="hint">上げるほどパネルが透けて、後ろの壁紙がよく見えます</div></div>
      ${switchRow("屈折エフェクト", "refract", window.chrome ? "ガラス越しの背景がゆがんで見える効果（Chrome / Edge）" : "このブラウザでは使えません（Chrome / Edge のみ）")}
      <div class="field"><span class="lbl">アクセントカラー</span><div class="choices" data-pref="accent">${ACCENTS.map(([v, l]) => `<button type="button" class="swatch ${state.prefs.accent === v ? "on" : ""}" data-value="${v}" title="${l}" style="background:${v || "linear-gradient(135deg,#0a64d6,#4ca2ff)"}"></button>`).join("")}</div></div>
      ${choiceRow("文字サイズ", "size", [["small", "小"], ["normal", "標準"], ["large", "大"]])}
      ${choiceRow("グラフの表示範囲", "range", RANGES)}
      <div class="field"><span class="lbl">開いたときのページ</span><select id="pref-start">${pages.map(([k, l]) => `<option value="${k}" ${state.prefs.start === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      <div class="form-actions"><button class="btn small" type="button" id="pref-reset">表示の設定を元に戻す</button></div>
    </div></div>
    <div class="card"><h2>${icon("lock")}アカウントとパスキー</h2><div id="account-box"><p class="note">読み込み中…</p></div></div>
  </div><div class="stack">
    <div class="card"><h2>${icon("info")}バージョンと機器の情報</h2><p class="note">「システム情報」ページにまとめています。</p>
      <div class="form-actions"><button class="btn small" type="button" onclick="go('system')">${icon("info")}システム情報を開く</button></div></div>
  </div></div>`;
}

function aboutItems() {
  return [
    { label: "ログイン中", value: state.who.who || "-" },
    { label: "パスキー", value: state.who.credential || "-" },
    { label: "Pi の Tailscale", value: state.who.tailscale_running ? "接続中" : "停止中" },
    { label: "リアルタイム更新", value: $("live").classList.contains("on") ? "接続中（約 2 秒ごと）" : "再接続中" },
    { label: "最終更新", value: state.data?.generated_at || "-" },
  ];
}

function deviceItems() {
  const pi = sec("pi"), cpu = pi.cpu || {}, it = Object.fromEntries((pi.items || []).map((i) => [i.label, i]));
  const ver = Object.fromEntries((pi.versions || []).map((v) => [v.name, v.version]));
  return [
    { label: "機種", value: it["機種"]?.value || "-" },
    { label: "ホスト名", value: it["ホスト名"]?.value || "-" },
    { label: "CPU", value: `${cpu.vendor || ""} ${cpu.model || ""} × ${cpu.cores || "?"}`.trim() },
    { label: "クロック", value: cpu.max_mhz ? `${Math.round(cpu.min_mhz)}〜${Math.round(cpu.max_mhz)} MHz` : "-" },
    { label: "メモリ", value: fmtBytes(pi.memory?.total, 1) },
    { label: "OS", value: ver["OS"] || "-" },
    { label: "カーネル", value: ver["カーネル"] || "-" },
    { label: "稼働時間", value: fmtUptime(cpu.uptime) },
  ];
}

function versionGroupsHTML() {
  const groups = [["pi", "Raspberry Pi"], ["network", "ネットワーク"], ["tailscale", "Tailscale"], ["nextcloud", "Nextcloud"]];
  return groups.map(([k, t]) => [k, t, sec(k).versions || []]).filter(([, , v]) => v.length)
    .map(([k, t, v]) => `<div class="card"><h2>${icon(k === "pi" ? "cores" : k)}${esc(t)}</h2>
      <dl class="kv">${v.map((x) => `<dt title="${esc(x.name)}">${esc(x.name)}</dt><dd class="mono">${esc(x.version)}</dd>`).join("")}</dl></div>`).join("");
}

function bindSettings() {
  const page = $("page");
  page.querySelectorAll("[data-pref]").forEach((box) => box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-value]");
    if (!b) return;
    const key = box.dataset.pref;
    state.prefs[key] = key === "range" ? Number(b.dataset.value) : b.dataset.value;
    box.querySelectorAll("[data-value]").forEach((x) => x.classList.toggle("on", x === b));
    savePrefs(); applyPrefs(); update();
    if (key === "theme") buildPage();
  }));
  page.querySelectorAll("[data-prefswitch]").forEach((el) => el.addEventListener("change", () => {
    state.prefs[el.dataset.prefswitch] = el.checked;
    savePrefs(); applyPrefs();
  }));
  $("pref-clear").addEventListener("input", (e) => {
    state.prefs.clear = Number(e.target.value);
    $("pref-clear-v").textContent = `${state.prefs.clear}%`;
    applyClear();
    savePrefs();
  });
  $("pref-start").addEventListener("change", (e) => { state.prefs.start = e.target.value; savePrefs(); });
  $("pref-reset").addEventListener("click", () => { state.prefs = { ...PREF_DEFAULTS }; savePrefs(); applyPrefs(); buildPage(); });
  loadAccount();
}

const b64 = {
  toBuf: (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)).buffer,
  fromBuf: (b) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
};
const deviceName = () => { const ua = navigator.userAgent; return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "この端末"; };

async function loadAccount() {
  const box = $("account-box");
  if (!box) return;
  let list = [];
  try { list = (await (await fetch("/api/passkeys", { cache: "no-store" })).json()).passkeys || []; } catch { /* 一覧が出せなくても他は使える */ }
  box.innerHTML = `<div class="list-rows">${list.map((c) => `<div class="list-row"><div class="grow"><div>${esc(c.name)}${c.common ? ' <span class="tag">共通</span>' : ""}${c.name === state.who.credential ? ' <span class="tag ok">使用中</span>' : ""}</div>
      <div class="sub">登録 ${esc(c.created_at)} · 最終使用 ${esc(c.last_used_at || "-")}</div></div>
      ${c.common ? '<div class="sub">全サービス共通のパスキーです</div>' : ""}</div>
      <button class="btn small danger" type="button" data-pk="${esc(c.id)}" data-common="${c.common ? 1 : ""}">削除</button></div>`).join("")}</div>
    <div class="form" style="margin-top:14px"><div class="field"><span class="lbl">この端末の名前</span><input type="text" id="pk-name" maxlength="40" value="${esc(deviceName())}"></div>
    <div class="form-actions"><button class="btn small danger" type="button" id="pk-logout">ログアウト</button><button class="btn small primary" type="button" id="pk-add">${icon("plus")}この端末のパスキーを追加</button></div>
    <p class="note">パスキーを同期していない端末を追加するときは、その端末でログイン画面を開き、「パスキーでログイン」→ 別の端末（パスキーがある端末）で QR コードを読み取ってログインしてから、ここで追加します。${state.who.common ? "ここで追加するパスキーは全サービス共通のパスキーになります。" : "すべて削除すると、次に開いたときに最初の登録からやり直しになります。"}</p></div>`;
  box.querySelectorAll("[data-pk]").forEach((b) => b.addEventListener("click", async () => {
    const message = b.dataset.common
      ? "全サービス共通のパスキーです。共通アカウントからも削除され、すべてのサービスでこのパスキーを使えなくなります。"
      : "削除した端末ではログインできなくなります。";
    if (!(await confirmDialog({ title: "このパスキーを削除しますか？", message, ok: "削除", danger: true }))) return;
    try { toast((await post("/api/passkey/remove", { id: b.dataset.pk })).message); } catch (err) { toast(err.message, true); }
    loadAccount();
  }));
  $("pk-logout").addEventListener("click", async () => { await post("/api/passkey/logout"); location.replace("/login"); });
  $("pk-add").addEventListener("click", async () => {
    const name = $("pk-name").value.trim() || "パスキー";
    try {
      const o = await post("/api/passkey/register/options");
      const cred = await navigator.credentials.create({ publicKey: {
        challenge: b64.toBuf(o.challenge), rp: o.rp,
        user: { id: b64.toBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
        pubKeyCredParams: o.pubKeyCredParams, authenticatorSelection: o.authenticatorSelection,
        excludeCredentials: o.excludeCredentials.map((c) => ({ type: c.type, id: b64.toBuf(c.id) })),
        attestation: o.attestation, timeout: o.timeout,
      } });
      await post("/api/passkey/register", { id: cred.id, name, response: {
        clientDataJSON: b64.fromBuf(cred.response.clientDataJSON), attestationObject: b64.fromBuf(cred.response.attestationObject),
        transports: typeof cred.response.getTransports === "function" ? cred.response.getTransports() : [] } });
      toast(`パスキー「${name}」を追加しました`);
      loadAccount();
    } catch (err) {
      toast(err.name === "InvalidStateError" ? "この端末のパスキーは登録済みです" : err.name === "NotAllowedError" ? "キャンセルされました" : err.message, true);
    }
  });
}

// ================================================================ 描画

function renderNav() {
  const html = PAGES.map((p) => {
    const lv = p.level ? p.level() : p.section ? sec(p.section).level : null;
    return `${p.bottom && p.key === PAGES.find((x) => x.bottom).key ? '<div class="spacer"></div>' : ""}<button class="nav-item ${p.key === state.page ? "active" : ""}" type="button" data-page="${p.key}" title="${esc(p.label)}">
      ${icon(p.icon)}<span>${esc(p.label)}</span><i class="ndot ${lv === "warn" || lv === "error" ? lv : ""}"></i></button>`;
  }).join("");
  fill("nav", html);
}

function renderCommands() {
  const p = PAGE[state.page];
  const cmds = p?.commands && state.data ? p.commands() : [];
  const box = $("commands");
  const html = cmds.map((c, i) => `<button class="btn ${c.cls || ""}" type="button" data-cmd="${i}" ${c.disabled ? "disabled" : ""} title="${esc(c.title || c.label)}">${icon(c.icon)}${esc(c.label)}</button>`).join("");
  if (box.innerHTML !== html) box.innerHTML = html;
  box.onclick = (e) => { const b = e.target.closest("[data-cmd]"); if (b && !b.disabled) cmds[Number(b.dataset.cmd)].run(); };
}

function buildPerfDetail() {
  const box = $("perf-detail");
  if (!box) return;
  const id = state.perfRes;
  const disk = (sec("pi").disks || []).find((d) => `disk:${d.dev}` === id);
  box.dataset.res = id;
  box.innerHTML = disk ? perfDetail.disk(disk) : (perfDetail[id] || perfDetail.cpu)();
  box.querySelectorAll("[data-cpuview]").forEach((b) => b.addEventListener("click", () => { state.cpuView = b.dataset.cpuview; buildPerfDetail(); update(); }));
}

function buildPage() {
  const p = PAGE[state.page];
  $("page").innerHTML = p.build();
  p.afterBuild?.();
  if (state.page === "performance") buildPerfDetail();
  update();
}

function update() {
  if (!state.data) return;
  const p = PAGE[state.page];
  renderNav();
  text("page-title", p.label);
  text("page-sub", p.sub ? p.sub() : "");
  if (p.table) renderTable(p.table.id, p.table, p.table.rows());
  p.update?.();
  renderCommands();
}

function go(page) {
  if (!PAGE[page]) page = "home";
  state.page = page;
  history.replaceState(null, "", `#${page}`);
  $("content").scrollTop = 0;
  window.scrollTo(0, 0);
  buildPage();
}

// ================================================================ イベント

// スマホ: スクロールしている間はフローティングバーを隠す
{
  const mobile = window.matchMedia("(max-width: 760px)");
  let timer = 0;
  document.addEventListener("scroll", (e) => {
    if (!mobile.matches || (e.target instanceof Element && e.target.closest("#nav"))) return;
    document.body.classList.add("scrolling");
    clearTimeout(timer);
    timer = setTimeout(() => document.body.classList.remove("scrolling"), 350);
  }, { capture: true, passive: true });
}
// 閉じる: ログアウトしてタブを閉じる（閉じられないブラウザではログイン画面へ）
$("close-btn").addEventListener("click", async () => {
  if (!await confirmDialog({ title: "閉じますか？", message: "ログアウトしてこの画面を閉じます。次に開くときはパスキーでのログインが必要です。", ok: "閉じる", danger: true })) return;
  try { await post("/api/passkey/logout"); } catch { /* ログアウトに失敗しても閉じる */ }
  window.close();
  setTimeout(() => location.replace("/login"), 300);
});
$("nav").addEventListener("click", (e) => { const b = e.target.closest("[data-page]"); if (b) go(b.dataset.page); });
$("nav-toggle").addEventListener("click", () => { state.prefs.nav = state.prefs.nav === "closed" ? "open" : "closed"; savePrefs({ sync: false }); applyPrefs(); setTimeout(update, 220); });
$("who").addEventListener("click", () => go("settings"));
$("search").addEventListener("input", (e) => { state.search = e.target.value; update(); });
$("dialog-backdrop").addEventListener("click", (e) => { if (e.target === $("dialog-backdrop")) closeDialog(false); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("dialog-backdrop").hidden) closeDialog(false);
  if ((e.metaKey || e.ctrlKey) && e.key === "f") { e.preventDefault(); $("search").focus(); }
});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(update, 120); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyPrefs(); if (state.data) buildPage(); });

// ================================================================ 接続（リアルタイム）

function setLive(status, label) {
  $("live").className = `live ${status}`;
  text("live-text", label);
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setLive("on", "リアルタイム");
  es.onmessage = (e) => {
    const first = !state.data;
    state.data = JSON.parse(e.data);
    setLive("on", `リアルタイム · ${state.data.generated_at.slice(11)}`);
    if (first) buildPage(); else update();
  };
  es.addEventListener("tailscale-off", () => { es.close(); location.reload(); });
  es.onerror = async () => {
    setLive("off", "再接続中…");
    try {
      const res = await fetch("/api/whoami", { cache: "no-store" });
      if (res.status === 401) { es.close(); location.replace("/login"); return; }
      const who = await res.json();
      if (!who.tailscale_running) { es.close(); location.reload(); }
    } catch { /* ネットワーク断。EventSource が自動で再接続する */ }
  };
}

// SSE が止まっていても、Tailscale が切れたら切り替え画面へ移るよう定期的にも確認する
setInterval(async () => {
  if (!state.data) return;
  try {
    const res = await fetch("/api/whoami", { cache: "no-store" });
    if (res.status === 401) { location.replace("/login"); return; }
    if (res.ok && !(await res.json()).tailscale_running) location.reload();
  } catch { /* 通信できないときは次の確認まで待つ */ }
}, 10000);

async function loadWho() {
  try {
    const res = await fetch("/api/whoami", { cache: "no-store" });
    if (res.status === 401) { location.replace("/login"); return; }
    state.who = await res.json();
  } catch {
    state.who = { can_edit: false, who: "" };
  }
  text("who-text", state.who.who || "未ログイン");
  if (state.data) update();
}

applyPrefs();
state.page = PAGE[location.hash.slice(1)] ? location.hash.slice(1) : (PAGE[state.prefs.start] ? state.prefs.start : "home");
text("page-title", PAGE[state.page].label);
loadWho();
loadAccountPrefs();
connect();

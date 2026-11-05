"use strict";
// 追加機能の画面: ホーム・履歴・アラート・操作の記録と、既存ページに足すカード。
// app.js の共通部品（$, esc, icon, fill, kv, tile, drawGraph, act, post, toast など）を使う。
// このファイルは app.js より先に読み込まれる。中の関数は、画面を組み立てるときに初めて呼ばれる。

// ================================================================ 共通

const fmtTs = (s, withYear = false) => (s ? new Date(s * 1000).toLocaleString("ja-JP", {
  ...(withYear ? { year: "numeric" } : {}), month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "-");
function fmtAgo(s) {
  if (!s) return "-";
  const d = Date.now() / 1000 - s;
  if (d < 60) return "たった今";
  if (d < 3600) return `${Math.floor(d / 60)} 分前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 時間前`;
  return `${Math.floor(d / 86400)} 日前`;
}
function fmtDur(sec) {
  if (sec == null) return "-";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 時間 ${Math.round((sec % 3600) / 60)} 分`;
  return `${Math.floor(sec / 86400)} 日 ${Math.floor((sec % 86400) / 3600)} 時間`;
}
const ext = () => state.data?.extra || {};
const LEVEL_TEXT = { error: "異常", warn: "注意", info: "情報", ok: "正常" };
const levelTag = (lv) => `<span class="tag ${lv === "error" ? "error" : lv === "warn" ? "warn" : "ok"}">${LEVEL_TEXT[lv] || esc(lv)}</span>`;

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { location.replace("/login"); throw new Error("ログインが必要です"); }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// 一定間隔で読み直すデータ（ページを開いている間だけ）
const loaders = {};
function loadEvery(key, ms, fn) {
  const l = loaders[key] || (loaders[key] = { at: 0, busy: false });
  if (l.busy || Date.now() - l.at < ms) return;
  l.busy = true;
  fn().catch((err) => toast(err.message, true)).finally(() => { l.busy = false; l.at = Date.now(); });
}
const reloadSoon = (key) => { if (loaders[key]) loaders[key].at = 0; };

// サイト全体の設定（通知・アラート）
async function loadSite() {
  state.site = await getJSON("/api/settings");
  return state.site;
}
async function saveSite(changes, okMessage) {
  const res = await post("/api/settings", { changes });
  state.site = res;
  toast(okMessage || res.message || "保存しました");
  return res;
}

// ================================================================ ホーム（概要）

const SECTION_PAGES = [["pi", "Raspberry Pi", "performance", "cores"], ["network", "ネットワーク", "network", "network"],
  ["tailscale", "Tailscale", "tailscale", "tailscale"], ["nextcloud", "Nextcloud", "nextcloud", "nextcloud"]];

function worstLevel(levels) {
  return levels.includes("error") ? "error" : levels.includes("warn") ? "warn" : "ok";
}
function homeLevel() {
  return worstLevel([...(state.data?.alerts || []).map((a) => a.level), ...SECTION_PAGES.map(([k]) => sec(k).level || "ok")]);
}

function homeBuild() {
  return `<div class="glass hero"><div class="state"><div class="orb" id="home-orb">${icon("check")}</div>
      <div><div class="big-label" id="home-state">-</div><div class="small" id="home-sub"></div></div></div></div>
    <div class="tiles" id="home-tiles"></div>
    <div class="grid-2"><div class="stack">
        <div class="card"><h2>${icon("bell")}いま起きていること</h2><div id="home-alerts"></div></div>
        <div class="card"><h2>${icon("info")}各部の状態</h2><div id="home-sections"></div></div>
      </div><div class="stack">
        <div class="card"><h2>${icon("audit")}最近の操作</h2><div id="home-audit"><p class="note">読み込み中…</p></div>
          <div class="form-actions"><button class="btn small" type="button" data-go="audit">${icon("audit")}すべて見る</button></div></div>
        <div class="card"><h2>${icon("lock")}記録の保存先（Cloudflare D1）</h2><div id="home-cloud"></div></div>
      </div></div>`;
}

function homeAfterBuild() {
  $("page").addEventListener("click", (e) => { const b = e.target.closest("[data-go]"); if (b) go(b.dataset.go); });
}

function alertRows(list) {
  if (!list.length) return `<p class="note">${icon("check")} 問題は起きていません</p>`;
  return `<div class="list-rows">${list.map((a) => `<div class="list-row"><div class="grow"><div>${levelTag(a.level)} ${esc(a.title)}</div>
    <div class="sub">${esc(a.message)} · ${fmtAgo(a.started_at)}から</div></div></div>`).join("")}</div>`;
}

function homeUpdate() {
  const lv = homeLevel(), active = state.data?.alerts || [];
  $("home-orb").className = `orb ${lv === "ok" ? "on" : lv === "warn" ? "disarmed" : "off"}`;
  text("home-state", { ok: "すべて正常です", warn: "注意が必要な項目があります", error: "異常があります" }[lv]);
  text("home-sub", active.length ? `${active.length} 件のアラートが発生中` : `最終更新 ${state.data?.generated_at?.slice(11) || "-"}`);

  const pi = sec("pi"), cpu = pi.cpu || {}, mem = pi.memory || {}, net = sec("network");
  const temp = (pi.history?.temp || []).filter((x) => x != null).at(-1);
  const disk = (pi.disks || []).filter((d) => d.percent != null).sort((a, b) => b.percent - a.percent)[0];
  const inet = (net.items || []).find((i) => i.label.startsWith("インターネット"));
  const ts = sec("tailscale");
  const tileGo = (page, k, v, s, level) => tile(k, v, s, level).replace('class="glass tile"', `class="glass tile" data-go="${page}" style="cursor:pointer"`);
  fill("home-tiles", [
    tileGo("performance", "CPU", pct((cpu.user ?? 0) + (cpu.system ?? 0)), `${cpu.cores || "-"} コア`),
    tileGo("performance", "メモリ", pct(mem.percent), `${fmtBytes(mem.used, 1)} / ${fmtBytes(mem.total, 1)}`, mem.level),
    tileGo("performance", "温度", temp != null ? `${temp.toFixed(1)} ℃` : "-", "SoC", temp >= 80 ? "error" : temp >= 70 ? "warn" : ""),
    tileGo("performance", "ディスク", disk ? pct(disk.percent) : "-", disk ? `${disk.label} が一番多い` : "", disk?.level),
    tileGo("network", "インターネット", inet?.value || "-", net.internet_host || "", inet?.level),
    tileGo("tailscale", "Tailscale", ts.summary || "-", (ts.items || []).find((i) => i.label === "オンライン")?.value || "", ts.level === "ok" ? "" : ts.level),
  ].join(""));

  fill("home-alerts", alertRows(active));
  fill("home-sections", `<div class="list-rows">${SECTION_PAGES.map(([k, label, page, ic]) => {
    const s = sec(k);
    return `<div class="list-row" data-go="${page}" style="cursor:pointer"><span class="dot ${s.level === "ok" ? "" : s.level || "off"}"></span>
      <div class="grow"><div>${icon(ic)} ${esc(label)}</div><div class="sub">${esc(s.summary || "-")}</div></div></div>`;
  }).join("")}</div>`);

  const c = ext().cloud || {};
  fill("home-cloud", kv([
    { label: "状態", value: !c.configured ? "未設定" : c.last_error ? "送信できていません" : "接続中", level: !c.configured || c.last_error ? "warn" : "" },
    { label: "最後に送信", value: c.last_ok ? fmtAgo(c.last_ok) : "-" },
    { label: "送信待ち", value: `${c.pending ?? 0} 件`, level: (c.pending || 0) > 100 ? "warn" : "" },
    ...(c.last_error ? [{ label: "エラー", value: c.last_error, level: "warn" }] : []),
  ]) + `<p class="note">Pi の SD カードが壊れても、履歴・操作の記録・設定・パスキーはここに残ります。</p>`);

  loadEvery("home-audit", 30000, async () => {
    const { rows } = await getJSON("/api/audit?limit=6");
    fill("home-audit", rows.length ? `<div class="list-rows">${rows.map((r) => `<div class="list-row"><div class="grow">
      <div>${r.ok ? "" : '<span class="tag error">失敗</span> '}${esc(actionLabel(r.action))}</div>
      <div class="sub">${fmtTs(r.ts)} · ${esc(r.credential || r.who || "-")} · ${esc(r.message)}</div></div></div>`).join("")}</div>`
      : `<p class="note">まだ記録はありません</p>`);
  });
}

// ================================================================ 履歴（長期のグラフ）

const HIST_RANGES = [["1h", "1 時間"], ["6h", "6 時間"], ["24h", "24 時間"], ["7d", "7 日"], ["30d", "30 日"], ["1y", "1 年"]];

function histGraph(id, title, legendEntries) {
  return `<div class="card"><h2>${esc(title)}</h2><div class="graph-wrap">
    <div class="gl"><span id="${id}-stat"></span><span id="${id}-max"></span></div>
    <canvas class="graph small" id="${id}"></canvas>
    <div class="gb"><span id="${id}-from"></span><span id="${id}-to"></span></div></div>
    ${legendEntries ? legend(legendEntries) : ""}</div>`;
}

function historyBuild() {
  return `<div class="form-actions" style="justify-content:flex-start;margin-bottom:12px" id="hist-ranges">
      ${HIST_RANGES.map(([k, l]) => `<button class="choice ${state.histRange === k ? "on" : ""}" type="button" data-range="${k}">${l}</button>`).join("")}
      <span class="note" id="hist-note" style="margin:0 0 0 8px"></span></div>
    <div class="grid-2"><div class="stack">
      ${histGraph("h-cpu", "CPU 使用率", [[css("--c-cpu"), "平均"], [css("--c-sys"), "最大"]])}
      ${histGraph("h-temp", "温度", [[css("--c-temp"), "平均"], [css("--c-sys"), "最大"]])}
    </div><div class="stack">
      ${histGraph("h-mem", "メモリ使用量", [[css("--c-mem"), "使用中"]])}
      ${histGraph("h-net", "通信量", [[css("--c-net"), "受信"], [css("--c-sys"), "送信"]])}
      ${histGraph("h-lat", "応答時間", [[css("--c-lat"), "インターネット"], [css("--c-disk"), "ルーター"], [css("--c-mem"), "Nextcloud"]])}
    </div></div>
    <p class="note">1 分ごとの平均を Cloudflare D1 に保存しています（1 分単位は 31 日、1 時間単位はずっと）。線が途切れているところは Pi が止まっていた時間です。</p>`;
}

function historyAfterBuild() {
  $("hist-ranges").addEventListener("click", (e) => {
    const b = e.target.closest("[data-range]");
    if (!b) return;
    state.histRange = b.dataset.range;
    document.querySelectorAll("#hist-ranges [data-range]").forEach((x) => x.classList.toggle("on", x === b));
    state.hist = null;
    reloadSoon("history");
    historyUpdate();
  });
}

const statText = (vals, f) => {
  const v = vals.filter((x) => x != null);
  if (!v.length) return "データなし";
  return `平均 ${f(v.reduce((a, b) => a + b, 0) / v.length)} · 最大 ${f(Math.max(...v))}`;
};

function historyUpdate() {
  loadEvery("history", 60000, async () => {
    const want = state.histRange;
    const d = await getJSON(`/api/history?range=${want}`);
    if (want === state.histRange) { state.hist = d; drawHistory(); }
  });
  drawHistory();
}

function drawHistory() {
  const h = state.hist;
  if (!h || !$("h-cpu")) { text("hist-note", "読み込み中…"); return; }
  text("hist-note", h.source === "d1" ? "" : "D1 に届かないため、手元の 24 時間分だけを表示しています");
  const n = h.t.length, from = fmtTs(h.t[0]), to = fmtTs(h.t.at(-1));
  for (const id of ["h-cpu", "h-temp", "h-mem", "h-net", "h-lat"]) { text(`${id}-from`, from); text(`${id}-to`, to); }
  const opt = { points: n };
  drawGraph($("h-cpu"), [{ values: h.cpu, color: css("--c-cpu") }, { values: h.cpu_max, color: css("--c-sys"), fill: "" }], { ...opt, max: 100 });
  text("h-cpu-stat", statText(h.cpu, (x) => `${x.toFixed(0)}%`)); text("h-cpu-max", "100%");
  drawGraph($("h-temp"), [{ values: h.temp, color: css("--c-temp") }, { values: h.temp_max, color: css("--c-sys"), fill: "" }], { ...opt, max: 90 });
  text("h-temp-stat", statText(h.temp_max.length ? h.temp_max : h.temp, (x) => `${x.toFixed(1)} ℃`)); text("h-temp-max", "90 ℃");
  const memTop = drawGraph($("h-mem"), [{ values: h.mem_used, color: css("--c-mem") }], { ...opt, max: h.mem_total || null });
  text("h-mem-stat", statText(h.mem_used, (x) => fmtBytes(x, 1))); text("h-mem-max", fmtBytes(memTop, 1));
  const netTop = drawGraph($("h-net"), [{ values: h.net_rx, color: css("--c-net") }, { values: h.net_tx, color: css("--c-sys"), fill: "" }], opt);
  text("h-net-stat", `受信 ${statText(h.net_rx, fmtBits)}`); text("h-net-max", fmtBits(netTop));
  const latTop = drawGraph($("h-lat"), [{ values: h.internet_ms, color: css("--c-lat") }, { values: h.router_ms || [], color: css("--c-disk"), fill: "" },
    { values: h.nextcloud_ms, color: css("--c-mem"), fill: "" }], opt);
  text("h-lat-stat", `インターネット ${statText(h.internet_ms, (x) => `${x.toFixed(0)} ms`)}`); text("h-lat-max", `${Math.round(latTop)} ms`);

}

// ================================================================ アラートと通知

function alertsBuild() {
  return `<div class="grid-2"><div class="stack">
      <div class="card"><h2>${icon("bell")}発生中</h2><div id="al-active"></div></div>
      ${tableHTML("t-alerts")}
    </div><div class="stack">
      <div class="card"><h2>${icon("bell")}通知（ntfy）</h2><div id="al-notify"><p class="note">読み込み中…</p></div></div>
      <div class="card"><h2>${icon("target")}監視する項目</h2><div id="al-rules"><p class="note">読み込み中…</p></div></div>
    </div></div>`;
}

function alertsAfterBuild() {
  loadSite().then(renderAlertSettings).catch((err) => toast(err.message, true));
}

function alertsUpdate() {
  fill("al-active", alertRows(state.data?.alerts || []));
  loadEvery("alerts", 30000, async () => {
    const d = await getJSON("/api/alerts?limit=200");
    state.alertHistory = d.history;
    if (state.page === "alerts") renderTable("t-alerts", PAGE.alerts.table, state.alertHistory);
  });
}

function renderAlertSettings() {
  const s = state.site, n = s.settings.notify;
  const wd = s.watchdog;
  $("al-notify").innerHTML = `<div class="form">
    <div class="field"><span class="lbl">通知する</span><label class="switch"><input type="checkbox" id="nt-enabled" ${n.enabled ? "checked" : ""}><span></span></label></div>
    <div class="field"><span class="lbl">購読する URL</span><div class="range-row"><input type="text" class="input mono" id="nt-url" readonly value="${esc(s.subscribe_url)}"><button class="btn small" type="button" id="nt-copy">コピー</button></div>
      <div class="hint">スマホに ntfy アプリを入れ、「＋」→ トピック名に <b class="mono">${esc(n.topic)}</b> を入れて購読します（サーバーは ${esc(n.server)}）。トピック名を知っている人は誰でも通知を読めるので、他人に教えないでください。</div></div>
    <div class="field"><span class="lbl">復旧も通知する</span><label class="switch"><input type="checkbox" id="nt-recovery" ${n.recovery ? "checked" : ""}><span></span></label></div>
    <div class="field"><span class="lbl">静かにする時間帯</span><input type="text" id="nt-quiet" value="${esc(n.quiet)}" placeholder="例: 23:00-07:00（空 = なし）" spellcheck="false">
      <div class="hint">この時間帯は「注意」の通知を送りません。「異常」は常に送ります。</div></div>
    <div class="field"><span class="lbl">ntfy のアクセストークン</span><input type="password" id="nt-token" value="${esc(n.token)}" placeholder="tk_…（空 = 使わない）" autocomplete="off" spellcheck="false">
      <div class="hint">Pi が止まったときの通知（Cloudflare から送る）に必要です。ntfy.sh は送信元の IP ごとに 1 日の上限があり、Cloudflare の IP は大勢で共有しているためすぐ上限に当たります。ntfy.sh で無料アカウントを作り、「Account」→「Access tokens」で発行したものを入れてください。</div></div>
    <details><summary class="note">詳細（サーバー・トピック）</summary>
      <div class="field"><span class="lbl">サーバー</span><input type="text" id="nt-server" value="${esc(n.server)}" spellcheck="false"></div>
      <div class="field"><span class="lbl">トピック名</span><div class="range-row"><input type="text" id="nt-topic" value="${esc(n.topic)}" spellcheck="false"><button class="btn small" type="button" id="nt-regen">作り直す</button></div></div>
    </details>
    <div class="form-actions"><button class="btn small" type="button" id="nt-test">${icon("bell")}テスト通知</button><button class="btn small primary" type="button" id="nt-save">${icon("check")}保存</button></div>
    <p class="note">Pi そのものが止まったとき（電源断・故障・回線断）は、Cloudflare 上の見張りが 10 分応答のないことを検知して同じトピックに通知します。${
      wd?._checked_at ? `見張りの最終確認: ${fmtAgo(wd._checked_at)}` : "見張りはまだ動いていません"}</p>
    ${wd?._error ? `<p class="note lv-warn">見張りからの通知に失敗しています: ${esc(wd._error.slice(25, 220))}${n.token ? "" : "（アクセストークンを設定してください）"}</p>` : ""}</div>`;
  $("nt-copy").addEventListener("click", () => navigator.clipboard?.writeText(s.subscribe_url).then(() => toast("コピーしました")));
  $("nt-regen").addEventListener("click", () => {
    const a = new Uint8Array(18); crypto.getRandomValues(a);
    $("nt-topic").value = "hs-" + Array.from(a, (x) => "abcdefghijkmnopqrstuvwxyz23456789"[x % 33]).join("");
    toast("新しいトピック名を入れました。保存するとアプリ側で購読し直す必要があります");
  });
  $("nt-test").addEventListener("click", async () => { try { toast((await post("/api/notify/test")).message); } catch (err) { toast(err.message, true); } });
  $("nt-save").addEventListener("click", async () => {
    try {
      await saveSite({ notify: { enabled: $("nt-enabled").checked, recovery: $("nt-recovery").checked, quiet: $("nt-quiet").value.trim(),
        server: $("nt-server").value.trim(), topic: $("nt-topic").value.trim(), token: $("nt-token").value.trim() } }, "通知の設定を保存しました");
      renderAlertSettings();
    } catch (err) { toast(err.message, true); }
  });

  const rules = s.settings.rules;
  $("al-rules").innerHTML = `<div class="form">${s.rules.map((r) => `<div class="field"><span class="lbl">${esc(r.label)}</span>
      <div class="range-row"><label class="switch"><input type="checkbox" data-rule="${r.id}" ${rules[r.id]?.enabled ? "checked" : ""}><span></span></label>
      ${r.unit ? `<div class="unit"><input type="number" data-th="${r.id}" value="${esc(rules[r.id]?.threshold)}" min="1" max="1000" step="1"><span>${esc(r.unit)}</span></div>` : ""}</div>
      ${r.hold ? `<div class="hint">${r.hold >= 60 ? `${r.hold / 60} 分` : `${r.hold} 秒`}続いたら通知</div>` : ""}</div>`).join("")}
    <div class="form-actions"><button class="btn small primary" type="button" id="rules-save">${icon("check")}保存</button></div></div>`;
  $("rules-save").addEventListener("click", async () => {
    const changes = {};
    for (const r of s.rules) {
      changes[r.id] = { enabled: document.querySelector(`[data-rule="${r.id}"]`).checked };
      const th = document.querySelector(`[data-th="${r.id}"]`);
      if (th) changes[r.id].threshold = Number(th.value);
    }
    try { await saveSite({ rules: changes }, "監視する項目を保存しました"); } catch (err) { toast(err.message, true); }
  });
}

// ================================================================ 操作の記録

const ACTION_LABELS = {
  "service.restart": "サービスの再起動", "pi.reboot": "Pi の再起動", "nextcloud.maintenance": "Nextcloud のメンテナンス",
  "nextcloud.restart": "コンテナの再起動", "tailscale.serve.add": "Serve の追加", "tailscale.serve.remove": "Serve の削除",
  "tailscale.set_running": "Tailscale のオン/オフ", "network.scan": "LAN の再スキャン",
  "lan.device": "LAN の端末の設定", "lan.known_all": "端末をすべて既知に", "health.refresh": "アップデートの確認",
  "passkey.login": "ログイン", "passkey.logout": "ログアウト", "passkey.register": "パスキーの登録",
  "passkey.remove": "パスキーの削除", "settings.update": "設定の変更",
};
const actionLabel = (a) => ACTION_LABELS[a] || a;

function auditBuild() {
  return `${tableHTML("t-audit")}
    <div class="form-actions"><button class="btn small" type="button" id="audit-more">さらに古い記録を読み込む</button></div>
    <p class="note" id="audit-note">画面からの操作・ログインを記録しています（ログの閲覧は記録しません）。記録は Cloudflare D1 に保存され、Pi が壊れても残ります。</p>`;
}

function auditAfterBuild() {
  state.auditRows = state.auditRows || [];
  $("audit-more").addEventListener("click", async () => {
    const last = state.auditRows.at(-1);
    try {
      const { rows } = await getJSON(`/api/audit?limit=200${last ? `&before=${last.ts}` : ""}`);
      const ids = new Set(state.auditRows.map((r) => r.id));
      state.auditRows.push(...rows.filter((r) => !ids.has(r.id)));
      if (!rows.length) toast("これより古い記録はありません");
      update();
    } catch (err) { toast(err.message, true); }
  });
}

function auditUpdate() {
  loadEvery("audit", 20000, async () => {
    const { rows, source } = await getJSON("/api/audit?limit=200");
    const ids = new Set(rows.map((r) => r.id));
    state.auditRows = [...rows, ...(state.auditRows || []).filter((r) => !ids.has(r.id) && r.ts < (rows.at(-1)?.ts ?? Infinity))];
    text("audit-note", source === "d1" ? $("audit-note").textContent : "D1 に届かないため、この起動以降の記録だけを表示しています");
    if (state.page === "audit") renderTable("t-audit", PAGE.audit.table, state.auditRows);
  });
}

function auditDetail(r) {
  let params = "";
  try {
    const p = JSON.parse(r.params || "{}");
    params = Object.entries(p).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
  } catch { params = r.params; }
  return params;
}

// ================================================================ システム情報に足すカード

function systemExtraHTML() {
  return `<div class="card"><h2>${icon("download")}アップデート</h2><div id="sys-updates"></div>
      <div class="form-actions"><button class="btn small" type="button" id="sys-upd-refresh">${icon("restart")}今すぐ確認</button></div></div>
    <div class="card"><h2>${icon("disk")}ディスクの健康状態</h2><div id="sys-disks"></div></div>
    <div class="card"><h2>${icon("lock")}バックアップ</h2><div id="sys-backup"></div></div>`;
}

function systemBootsHTML() {
  return `<div class="card"><h2>${icon("power")}起動と停止の履歴</h2><div id="sys-boots"><p class="note">読み込み中…</p></div></div>`;
}

function systemExtraAfterBuild() {
  $("sys-upd-refresh")?.addEventListener("click", () => act("health.refresh"));
}

function systemExtraUpdate() {
  const h = ext().health || {};
  const up = h.updates;
  if (!up) fill("sys-updates", `<p class="note">確認中…</p>`);
  else {
    const apt = up.apt || {};
    fill("sys-updates", kv([
      { label: "OS のパッケージ", value: apt.count ? `${apt.count} 個（うちセキュリティ ${apt.security} 個）` : "最新です", level: apt.security ? "warn" : "" },
      { label: "パッケージ一覧の更新", value: apt.checked_at ? fmtAgo(apt.checked_at) : "不明" },
      { label: "再起動が必要", value: up.reboot_required ? "はい（更新を反映するため）" : "いいえ", level: up.reboot_required ? "warn" : "" },
      { label: "Nextcloud", value: (up.nextcloud || []).join(" / ") || "最新です", level: (up.nextcloud || []).length ? "warn" : "" },
      { label: "最終確認", value: fmtAgo(up.checked_at) },
    ]) + (apt.count ? `<p class="note mono">${apt.packages.slice(0, 30).map((p) => esc(`${p.name} ${p.current} → ${p.version}`)).join("<br>")}${apt.count > 30 ? "<br>…" : ""}</p>` : "")
      + `<p class="note">OS の更新は Pi で <span class="mono">sudo apt upgrade</span>、Nextcloud はイメージを作り直して更新します（ここからは実行しません）。</p>`);
  }
  const d = h.disks;
  if (!d) fill("sys-disks", `<p class="note">確認中…</p>`);
  else {
    const items = [];
    for (const [dev, s] of Object.entries(d.smart || {})) {
      items.push(s.available
        ? { label: `${dev}（${s.model || "SSD"}）`, value: `${s.passed === false ? "異常" : "正常"}${s.life_left != null ? ` · 残り寿命 ${s.life_left}%` : ""}${s.temp != null ? ` · ${s.temp} ℃` : ""}${s.power_on_hours != null ? ` · 通電 ${s.power_on_hours} 時間` : ""}${s.reallocated ? ` · 代替処理 ${s.reallocated}` : ""}`, level: s.level }
        : { label: dev, value: `SMART: ${s.reason}` });
    }
    for (const [fs, n] of Object.entries(d.fs_errors || {})) items.push({ label: `${fs} のエラー`, value: n ? `${n} 件` : "なし", level: n ? "warn" : "" });
    items.push({ label: "読み書きのエラー", value: (d.kernel_errors || []).length ? `${d.kernel_errors.length} 件（この起動以降）` : "なし（この起動以降）", level: (d.kernel_errors || []).length ? "warn" : "" });
    if (d.sd_written != null) items.push({ label: "SD カードへの書き込み", value: `${fmtBytes(d.sd_written, 1)}（この起動以降）` });
    fill("sys-disks", kv(items) + ((d.kernel_errors || []).length ? `<pre class="log" style="max-height:140px">${esc(d.kernel_errors.join("\n"))}</pre>` : "")
      + `<p class="note">SD カードには SMART（自己診断）がありません。書き込み量が多いと寿命が縮みます。</p>`);
  }
  const b = h.backup;
  if (b) {
    fill("sys-backup", kv([
      { label: b.d1.label, value: !b.d1.configured ? "未設定" : b.d1.error ? `送信できていません（${b.d1.pending} 件待ち）` : b.d1.last ? `同期済み · ${fmtAgo(b.d1.last)}` : "同期待ち", level: !b.d1.configured || b.d1.error ? "warn" : "" },
      { label: b.nextcloud.label, value: b.nextcloud.configured ? (b.nextcloud.last ? `最新 ${fmtTs(b.nextcloud.last)}（${fmtAgo(b.nextcloud.last)}）` : "中身がありません") : b.nextcloud.note, level: b.nextcloud.configured && b.nextcloud.last ? "" : "warn" },
    ]) + `<p class="note">${esc(b.d1.note)}</p>`);
  }
  loadEvery("boots", 60000, async () => {
    const { rows } = await getJSON("/api/boots");
    fill("sys-boots", rows.length ? `<div class="list-rows">${rows.map((r) => {
      const lv = r.reason === "稼働中" ? "" : r.reason === "予期しない停止" ? "error" : r.detail ? "warn" : "off";
      return `<div class="list-row"><span class="dot ${lv}"></span><div class="grow">
        <div>${esc(r.reason)}${r.reason === "稼働中" ? ` · ${fmtDur(Date.now() / 1000 - r.booted_at)}` : ""}</div>
        <div class="sub">起動 ${fmtTs(r.booted_at, true)}${r.reason === "稼働中" ? "" : ` → 停止 ${fmtTs(r.last_seen)}（${fmtDur(r.last_seen - r.booted_at)}稼働）`}${
          r.down_seconds != null ? ` · ${fmtDur(r.down_seconds)}止まっていた` : ""}${r.detail ? ` · ${esc(r.detail)}` : ""}</div></div></div>`;
    }).join("")}</div>` : `<p class="note">記録がありません</p>`);
  });
}

// ================================================================ ネットワーク: 知らない端末

const lanInfo = (r) => ext().lan?.[(r.mac || "").toLowerCase()];

function lanNoteTags(r) {
  const info = lanInfo(r);
  if (!info) return "";
  return info.known ? "" : '<span class="tag warn">新しい端末</span>';
}

function lanName(r) {
  return lanInfo(r)?.label || r.name || "(名前なし)";
}

function lanCommands(row) {
  const info = row && lanInfo(row);
  const unknown = Object.values(ext().lan || {}).filter((d) => !d.known).length;
  return [
    { label: info && !info.known ? "既知にする" : "未確認に戻す", icon: "check", disabled: !canEdit() || !info, title: "表で端末を選んでから",
      run: () => act("lan.device", { mac: row.mac, known: !info.known }) },
    { label: "名前を付ける", icon: "edit", disabled: !canEdit() || !info, title: "表で端末を選んでから", run: () => labelDialog(row) },
    ...(unknown ? [{ label: `新しい端末 ${unknown} 台をすべて既知に`, icon: "check", disabled: !canEdit(),
      run: () => act("lan.known_all", {}, { confirm: { title: "すべて既知にしますか？", message: "今「新しい端末」と表示されている端末を、すべて知っている端末として扱います。", ok: "既知にする" } }) }] : []),
  ];
}

function labelDialog(row) {
  const info = lanInfo(row) || {};
  openDialog({
    title: "端末に名前を付ける",
    body: `<form class="form" onsubmit="return false"><div class="form-group"><div class="field"><span class="lbl">名前</span>
      <input type="text" id="lan-label" maxlength="40" value="${esc(info.label || "")}" placeholder="${esc(row.name || row.ip)}"></div>
      <p class="note">${esc(row.ip)} · <span class="mono">${esc(row.mac)}</span>。名前を付けると既知の端末にもなります。</p></div></form>`,
    buttons: [
      { label: "キャンセル", onClick: () => closeDialog() },
      { label: "保存", cls: "primary", onClick: async () => { const label = $("lan-label").value; closeDialog(); await act("lan.device", { mac: row.mac, label, known: true }); } },
    ],
  });
}

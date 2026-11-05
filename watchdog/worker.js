// Pi の見張り（Cloudflare Workers で 5 分ごとに動く）。
//
// Pi は 1 分ごとに D1 の heartbeat を更新している。それが DOWN_AFTER 秒以上止まったら、
// Pi が止まったか自宅の回線が切れたとみなして ntfy に通知する。戻ったら復旧を通知する。
// 通知先（ntfy のサーバーとトピック）は Pi と同じく D1 の kv「settings」から読む。
// 通知したかどうかは kv「watchdog」に残す（同じ停止で何度も通知しない）。

const DOWN_AFTER = 10 * 60;

const fmt = (sec, tz) => new Date(sec * 1000).toLocaleString("ja-JP", { timeZone: tz || "Asia/Tokyo", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
const dur = (sec) => (sec >= 3600 ? `${Math.floor(sec / 3600)} 時間 ${Math.round((sec % 3600) / 60)} 分` : `${Math.max(1, Math.round(sec / 60))} 分`);

async function kvGet(db, key) {
  const row = await db.prepare("SELECT value FROM kv WHERE key = ?").bind(key).first();
  return row ? JSON.parse(row.value) : null;
}

async function kvPut(db, key, value) {
  await db.prepare("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at")
    .bind(key, JSON.stringify(value), Date.now()).run();
}

async function send(env, notify, title, message, priority, tags) {
  const res = await fetch(notify.server.replace(/\/$/, ""), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(notify.token ? { Authorization: `Bearer ${notify.token}` } : {}) },
    body: JSON.stringify({ topic: notify.topic, title, message, priority, tags, ...(env.PUBLIC_URL ? { click: env.PUBLIC_URL } : {}) }),
  });
  if (!res.ok) throw new Error(`ntfy ${res.status}: ${(await res.text()).slice(0, 200)}`);
}

async function check(env) {
  const db = env.DB;
  const settings = await kvGet(db, "settings");
  const notify = settings?.notify;
  const canSend = !!(notify?.enabled && notify.topic && notify.server);
  const { results: beats } = await db.prepare("SELECT host, ts FROM heartbeat").all();
  const state = (await kvGet(db, "watchdog")) || {};
  const now = Math.floor(Date.now() / 1000);
  state._error = null;
  // 送信に失敗しても状態は保存し、次の実行で送り直す
  const trySend = async (...args) => {
    if (!canSend) return false;
    try { await send(env, notify, ...args); return true; } catch (e) { state._error = `${new Date().toISOString()} ${e}`; return false; }
  };
  for (const { host, ts } of beats) {
    const st = state[host] || { down: false };
    const age = now - ts;
    if (age > DOWN_AFTER) {
      if (!st.down) Object.assign(st, { down: true, since: ts, notified: false });
      if (!st.notified) {
        st.notified = await trySend(`${host} から応答がありません`,
          `最後の応答: ${fmt(ts, env.TIME_ZONE)}（${dur(age)}前）\nPi が止まったか、自宅のインターネットが切れています。電源とルーターを確認してください。`,
          5, ["skull"]);
      }
    } else if (st.down) {
      if (st.notified && notify?.recovery !== false) {
        await trySend(`${host} が復旧しました`, `約 ${dur(ts - (st.since || ts))} 応答がありませんでした。`, 3, ["white_check_mark"]);
      }
      Object.assign(st, { down: false, since: null, notified: false, recovered_at: now });
    }
    state[host] = st;
  }
  state._checked_at = now;
  await kvPut(db, "watchdog", state);
  return state;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(check(env));
  },
  async fetch() {
    return new Response("not found", { status: 404 });
  },
};

// 壁紙: 粒子でできた薄い布が折り重なる模様を canvas に描いて背景に敷く（全ページ共通）
"use strict";
(() => {
  // tint: 粒子の色
  const WALLS = {
    black: { label: "ブラック", tint: [208, 214, 224] },
    purple: { label: "パープル", tint: [200, 172, 240] },
    blue: { label: "ブルー", tint: [150, 188, 245] },
    green: { label: "グリーン", tint: [160, 218, 192] },
    gold: { label: "ゴールド", tint: [238, 206, 150] },
  };
  const DEFAULT = "black";

  // 毎回同じ模様になるよう固定の種で乱数を作る
  function rng(seed) {
    return () => { seed |= 0; seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  function bezier(p, u) {
    const a = 1 - u;
    return [
      a * a * a * p[0][0] + 3 * a * a * u * p[1][0] + 3 * a * u * u * p[2][0] + u * u * u * p[3][0],
      a * a * a * p[0][1] + 3 * a * a * u * p[1][1] + 3 * a * u * u * p[2][1] + u * u * u * p[3][1],
    ];
  }

  // canvas に描く。dark: ダークテーマ（黒地に明るい粒子）/ ライト（明るい地に暗い粒子）
  function render(canvas, key, dark, seed = 7) {
    const wall = WALLS[key] || WALLS[DEFAULT], [tr, tg, tb] = wall.tint;
    const ctx = canvas.getContext("2d"), W = canvas.width, H = canvas.height, M = Math.max(W, H);
    const r = rng(seed), dot = Math.max(1, Math.round(Math.min(W, H) / 700));

    // 下地
    const bg = ctx.createRadialGradient(W * .3, H * .35, 0, W * .5, H * .5, M * .8);
    if (dark) {
      bg.addColorStop(0, `rgb(${tr * .16 | 0},${tg * .16 | 0},${tb * .17 | 0})`);
      bg.addColorStop(1, "rgb(3,3,4)");
    } else {
      bg.addColorStop(0, `rgb(${246 - (255 - tr) * .08 | 0},${246 - (255 - tg) * .08 | 0},${248 - (255 - tb) * .08 | 0})`);
      bg.addColorStop(1, `rgb(${222 - (255 - tr) * .12 | 0},${224 - (255 - tg) * .12 | 0},${228 - (255 - tb) * .12 | 0})`);
    }
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // 粒子の色（明るさごとにまとめて描くと速い）
    const LEVELS = 14, buckets = Array.from({ length: LEVELS }, () => []);
    const ink = dark ? [tr, tg, tb] : [tr * .22 | 0, tg * .22 | 0, tb * .24 | 0];
    const beads = [];

    // 布: 画面を横切る曲線に沿った帯を、ねじりながら粒子で埋める
    const sheets = 6, budget = Math.min(1.6e6, W * H * .55);
    for (let s = 0; s < sheets; s++) {
      const side = r() < .5;
      const p0 = side ? [-.15 * W, r() * H] : [r() * W, -.15 * H];
      const p3 = side ? [1.15 * W, r() * H] : [r() * W, 1.15 * H];
      const path = [p0, [(r() * 1.2 - .1) * W, (r() * 1.2 - .1) * H], [(r() * 1.2 - .1) * W, (r() * 1.2 - .1) * H], p3];
      const width = (.2 + r() * .18) * M, depth = .45 + r() * .55;
      const twA = r() * Math.PI * 2, twB = (.6 + r() * 1.8) * Math.PI, wob = r() * 6;
      const n = Math.round(budget / sheets * (.7 + r() * .6));
      for (let i = 0; i < n; i++) {
        const u = r(), v = r() * 2 - 1;
        const c = bezier(path, u), c2 = bezier(path, Math.min(1, u + .002));
        let tx = c2[0] - c[0], ty = c2[1] - c[1];
        const tl = Math.hypot(tx, ty) || 1; tx /= tl; ty /= tl;
        const wu = width * (.3 + .7 * Math.sin(Math.PI * u));
        const th = twA + twB * u + .7 * Math.sin(5 * u + wob);
        const off = v * wu, cs = Math.cos(th), sn = Math.sin(th);
        const x = c[0] - ty * off * cs + tx * off * .35 * sn;
        const y = c[1] + tx * off * cs + ty * off * .35 * sn;
        if (x < -2 || y < -2 || x > W + 2 || y > H + 2) continue;
        // 光の当たり具合: ねじれで面がこちらを向くほど明るく、布のふちは光る
        const light = depth * (.2 + .8 * Math.abs(Math.sin(th + v * .9)) ** 1.5);
        const edge = Math.abs(v) > .965 ? .55 : 0;
        const level = Math.min(LEVELS - 1, Math.floor((light * .85 + edge) * LEVELS));
        buckets[level].push(x, y);
        if (r() < .00035) beads.push(x, y, light);
      }
    }

    ctx.globalCompositeOperation = dark ? "lighter" : "source-over";
    for (let l = 0; l < LEVELS; l++) {
      const a = dark ? .035 + l * .03 : .025 + l * .02;
      ctx.fillStyle = `rgba(${ink[0]},${ink[1]},${ink[2]},${a.toFixed(3)})`;
      const b = buckets[l];
      for (let i = 0; i < b.length; i += 2) ctx.fillRect(b[i], b[i + 1], dot, dot);
    }

    // ところどころに小さな玉（光沢のある粒）
    ctx.globalCompositeOperation = "source-over";
    for (let i = 0; i < beads.length; i += 3) {
      const x = beads[i], y = beads[i + 1], rad = dot * (.8 + r() * 2.2);
      const g = ctx.createRadialGradient(x - rad * .35, y - rad * .35, 0, x, y, rad);
      const hi = dark ? `rgba(${Math.min(255, tr + 30)},${Math.min(255, tg + 30)},${Math.min(255, tb + 30)},${(.45 + beads[i + 2] * .45).toFixed(2)})` : "rgba(255,255,255,.9)";
      g.addColorStop(0, hi);
      g.addColorStop(1, dark ? "rgba(0,0,0,.6)" : `rgba(${ink[0]},${ink[1]},${ink[2]},.55)`);
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
    }

    // 四隅を少し落とす
    const vg = ctx.createRadialGradient(W / 2, H / 2, M * .25, W / 2, H / 2, M * .75);
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, dark ? "rgba(0,0,0,.55)" : "rgba(0,0,0,.08)");
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, W, H);
    return canvas;
  }

  // ---------------------------------------------------------------- ブラウザで使う部分
  let current = null, lastUrl = null, lastSize = [0, 0];

  function draw() {
    if (!current || typeof document === "undefined") return;
    // 粒子は細かいので、長い辺 1800px までで描けば十分（重くならないように）
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const scale = Math.min(1, 1800 / Math.max(innerWidth * dpr, innerHeight * dpr));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(innerWidth * dpr * scale);
    canvas.height = Math.round(innerHeight * dpr * scale);
    lastSize = [innerWidth, innerHeight];
    const { key, dark } = current;
    render(canvas, key, dark);
    canvas.toBlob((blob) => {
      if (!blob || current.key !== key || current.dark !== dark) return;
      const url = URL.createObjectURL(blob);
      document.documentElement.style.setProperty("--wall-image", `url("${url}")`);
      if (lastUrl) setTimeout((u) => URL.revokeObjectURL(u), 1000, lastUrl);
      lastUrl = url;
    }, "image/png");
  }

  function apply(key, dark) {
    const k = WALLS[key] ? key : DEFAULT;
    if (current && current.key === k && current.dark === dark) return;
    current = { key: k, dark };
    const bg = dark ? "#050506" : "#e9eaee";
    document.documentElement.style.setProperty("--wall-bg", bg);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", bg);
    setTimeout(draw, 30);
  }

  // 画面の大きさや向きが大きく変わったときだけ描き直す
  let resizeTimer = 0;
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const [w, h] = lastSize;
        const turned = (w > h) !== (innerWidth > innerHeight);
        const grew = innerWidth > w * 1.2 || innerHeight > h * 1.2;
        if (turned || grew) draw();
      }, 400);
    });
  }

  // 設定画面の小さな見本
  const thumbs = {};
  function thumb(key, dark) {
    const id = `${key}-${dark}`;
    if (!thumbs[id]) {
      const c = document.createElement("canvas");
      c.width = 144; c.height = 92;
      thumbs[id] = render(c, key, dark).toDataURL("image/png");
    }
    return thumbs[id];
  }

  // ログイン画面などは保存済みの表示設定を読んで同じ壁紙にする
  function applySaved() {
    let prefs = {};
    try { prefs = JSON.parse(localStorage.getItem("hs-prefs") || "{}"); } catch { /* 既定のまま */ }
    const theme = prefs.theme || "auto";
    const dark = theme === "dark" || (theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    apply(prefs.wall, dark);
  }

  window.HSWall = { WALLS, DEFAULT, render, apply, applySaved, thumb };
})();

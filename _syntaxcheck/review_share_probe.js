/**
 * 前端探针 ④：协作审阅分享卡片契约
 * ==================================
 * 1. escapeHtml 真跑防注入：喂 <script> 必须被转义（分享页把批注/标题
 *    展示给导师，这里失守 = 存储型 XSS 通道）。
 * 2. 分享卡片源码必须包含「下载离线快照」提示（v2.22 契约：桌面版的
 *    分享链接只在本机服务存活时可访问，离线快照是唯一可靠交付方式）。
 * 3. 分享卡片必须对 URL/标题走 escapeHtml（抽查源码形态）。
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");

function extractFn(src, signature) {
  const i = src.indexOf(signature);
  if (i < 0) throw new Error(`找不到 ${signature}`);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") { depth++; started = true; }
    if (src[j] === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error(`${signature} 括号不配对`);
}

// ── ① escapeHtml 防注入（真跑）──
const escapeHtml = new Function(`return (${extractFn(html, "function escapeHtml(s)")});`)();
const evil = '<script>alert("x")</script>';
const got = escapeHtml(evil);
if (got.includes("<script>") || /[<>"]/.test(got.replace(/&(amp|lt|gt|quot|#39);/g, ""))) {
  console.error(`[探针] escapeHtml 未正确转义：${got}`);
  process.exit(1);
}

// ── ② 离线快照提示（v2.22 契约）──
if (!html.includes("下载离线快照")) {
  console.error("[探针] 分享卡片缺少「下载离线快照」提示（桌面版链接只在本机服务存活时可访问）");
  process.exit(1);
}

// ── ③ 卡片对动态值走 escapeHtml ──
const cardFn = extractFn(html, "function card(info)");
if (!cardFn.includes("escapeHtml(")) {
  console.error("[探针] 分享卡片对动态值（URL/日期）未走 escapeHtml");
  process.exit(1);
}

console.log("[PASS] review_share_probe：escapeHtml 转义 / 离线快照提示 / 动态值转义 三契约通过");

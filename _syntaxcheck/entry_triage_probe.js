/**
 * 前端探针 ⑩：v2.54 首页入口分流（EntryTriage）契约
 * =====================================================================
 * 背景：AI 模拟体验（S01–S13，2026-10-24）发现「想学统计」的用户会误入
 * 规范体检——首屏范围声明只校准预期，入口分流把用户一步引到正确的路：
 *   lint → 「论文排查」Tab 的规范体检卡；data / analyze → 上传区（数据体检）
 *
 * 本探针从 templates/index.html 抽出 EntryTriage 真实源码，配 DOM mock 真跑：
 *   lint 用例   → 点 check Tab + 滚到 paperLintCard + 高亮 + toast 说明范围
 *   data 用例   → 点 analyze Tab + 滚到 drop + toast 引导上传
 *   analyze 用例 → 同 data（同一上传入口，体检与分析在同一主线）
 *   未知目标   → 安全 no-op（不点 Tab、不滚动、不崩溃）
 * 另外锁住页面契约：三张分流卡（lint/data/analyze）与 #paperLintCard 锚点存在，
 * 高亮类名与 CSS 里的 .triage-flash 一致。
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");

function extractObj(src, signature) {
  const i = src.indexOf(signature);
  if (i < 0) throw new Error(`找不到 ${signature}`);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") { depth++; started = true; }
    if (src[j] === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error(`${signature} 括号不配对`);
}
function fail(msg) { console.error(`[探针] ${msg}`); process.exit(1); }

// ── 页面契约：分流卡与锚点必须存在 ──
if (!html.includes('id="triageBox"')) fail("页面缺少 #triageBox 分流区");
for (const t of ["lint", "data", "analyze"]) {
  if (!html.includes(`data-triage="${t}"`)) fail(`页面缺少 data-triage="${t}" 的分流卡`);
}
if (!html.includes('id="paperLintCard"')) fail("页面缺少 #paperLintCard（lint 分流的目标锚点）");
if (!/\.triage-flash\s*\{/.test(html)) fail("样式表缺少 .triage-flash 高亮类");

const src = extractObj(html, "const EntryTriage = {");
if (!src.includes("'check'") || !src.includes("'analyze'")) fail("EntryTriage 未分流到 check/analyze 两个 Tab");

// ── DOM mock ──
function makeEl(id) {
  return {
    id,
    classes: new Set(),
    classList: { add(c) { this._el.classes.add(c); }, remove(c) { this._el.classes.delete(c); },
                 contains(c) { return this._el.classes.has(c); }, _el: null },
    scrolled: 0,
    scrollIntoView() { this.scrolled++; },
  };
}
const els = { paperLintCard: makeEl("paperLintCard"), drop: makeEl("drop") };
for (const k of Object.keys(els)) els[k].classList._el = els[k];
let clickedTabs = [];
let toasts = [];
const tabs = { check: { click() { clickedTabs.push("check"); } },
               analyze: { click() { clickedTabs.push("analyze"); } } };
global.document = {
  querySelector(sel) {
    const m = sel.match(/#tabbar \.tab\[data-tab="(\w+)"\]/);
    return m ? tabs[m[1]] : null;
  },
  getElementById(id) { return els[id] || null; },
  querySelectorAll() { return []; },
};
const ranTimers = [];
const setTimeoutNow = (fn) => { ranTimers.push(fn); return 0; };   // 挂起不执行，退场时机由下面的用例驱动

const EntryTriage = new Function("document", "toast", "setTimeout",
  `"use strict";${src}\nreturn EntryTriage;`)(global.document, (m) => toasts.push(m), setTimeoutNow);

// ── lint 用例：切论文排查 Tab，滚到规范体检卡并高亮 ──
EntryTriage.go("lint");
if (clickedTabs.join() !== "check") fail(`lint 用例：应点击 check Tab，实际 [${clickedTabs}]`);
if (els.paperLintCard.scrolled !== 1) fail("lint 用例：没有滚动到 paperLintCard");
if (!els.paperLintCard.classes.has("triage-flash")) fail("lint 用例：没有加 triage-flash 高亮");
if (!toasts.length || !toasts[0].includes("不核对数据")) fail("lint 用例：toast 应说明「不核对数据内容」的范围边界");
if (els.drop.scrolled !== 0) fail("lint 用例：不应滚动上传区");

// ── data 用例：切数据分析 Tab，滚到上传区 ──
EntryTriage.go("data");
if (clickedTabs.join() !== "check,analyze") fail(`data 用例：应点击 analyze Tab，实际 [${clickedTabs}]`);
if (els.drop.scrolled !== 1) fail("data 用例：没有滚动到上传区");
if (!els.drop.classes.has("triage-flash")) fail("data 用例：没有加上传区高亮");
if (!toasts[1] || !toasts[1].includes("12 类数据体检")) fail("data 用例：toast 应引导先跑数据体检");

// ── 高亮会自己退场：定时回调登记后执行，类名应被移除 ──
if (!ranTimers.length) fail("高亮从未安排退场（setTimeout 没被调用）");
ranTimers.splice(0).forEach((fn) => fn());
if (els.paperLintCard.classes.has("triage-flash")) fail("paperLintCard 高亮没有按定时器退场");

// ── analyze 用例：与 data 同一入口 ──
EntryTriage.go("analyze");
if (clickedTabs[clickedTabs.length - 1] !== "analyze") fail("analyze 用例：应点击 analyze Tab");
if (els.drop.scrolled !== 2) fail("analyze 用例：没有滚动到上传区");

// ── 未知目标：安全 no-op ──
const before = { tabs: clickedTabs.length, scrolls: els.drop.scrolled + els.paperLintCard.scrolled, toasts: toasts.length };
EntryTriage.go("bogus");
if (clickedTabs.length !== before.tabs || toasts.length !== before.toasts) fail("未知目标不是 no-op（误点了 Tab 或弹了 toast）");

console.log("[探针] entry_triage 全绿：三入口分流 / 高亮退场 / 未知目标 no-op");

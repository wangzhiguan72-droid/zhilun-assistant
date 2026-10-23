/**
 * 前端探针 ⑨：提交前规范体检（runPaperLint）渲染 / 转义 / 降级契约（v2.34）
 * =====================================================================
 * 从 templates/index.html 抽出 runPaperLint 的真实源码，配上 DOM mock 与
 * fetch mock 真跑三遍：
 *   成功用例 → 渲染结论、分级计数与每条问题；论文摘录里的 <script> 必须被转义
 *              （体检的就是用户原文，什么字符都可能有）；「导出体检清单」按钮出现
 *   失败用例 → 后端 error 文案透出到状态行，结果区不写旧报告，导出按钮隐藏
 *   干净用例 → 0 条问题时给出明确的通过提示，而不是空白
 * 另外锁住：/api/paper_lint 端点名、导出走 /api/export 且 title=论文规范体检。
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

function fail(msg) { console.error(`[探针] ${msg}`); process.exit(1); }

const escapeHtmlSrc = extractFn(html, "function escapeHtml(s)");
const lintSrc = extractFn(html, "async function runPaperLint()");
const exportSrc = extractFn(html, "async function exportPaperLint()");
for (const id of ["paperLintBtn", "paperLintInput", "paperLintStatus", "paperLintResult", "paperLintExportBtn"]) {
  if (!html.includes(`id="${id}"`)) fail(`页面缺少 #${id}`);
}
if (!lintSrc.includes("'/api/paper_lint'")) fail("runPaperLint 没有调用 /api/paper_lint");
if (!exportSrc.includes("'/api/export'") || !exportSrc.includes("论文规范体检")) fail("导出没有走 /api/export + title=论文规范体检");

function makeEl() {
  return { value: "x", textContent: "", innerHTML: "", style: {}, disabled: false,
           classList: { add() {}, remove() {}, contains() { return false; } },
           files: [{ name: "paper.docx" }], addEventListener() {} };
}
const els = {};
function mockDom() {
  for (const k of ["status", "result", "input", "exportBtn"]) els[k] = makeEl();
  els.exportBtn.style.display = "none";
  const map = { paperLintStatus: "status", paperLintResult: "result", paperLintInput: "input",
                paperLintExportBtn: "exportBtn" };
  global.document = {
    getElementById(id) { return map[id] ? els[map[id]] : makeEl(); },
    createElement() { return makeEl(); },
    addEventListener() {},
    querySelectorAll() { return []; },
    body: makeEl(),
  };
  global.window = global;
  global._lastPaperLintMarkdown = "上一次的旧报告";
}

function makeRunner(fakeResponse) {
  mockDom();
  const escapeHtml = new Function(`return (${escapeHtmlSrc});`)();
  return new Function("$", "fetch", "toast", "escapeHtml", "paperLintInput",
                      `"use strict";${lintSrc}\nreturn runPaperLint;`)(
    (id) => document.getElementById(id), async () => fakeResponse, () => {}, escapeHtml, els.input);
}

(async () => {
  const XSS = "<script>alert(1)</script>";
  const REPORT = {
    ok: true, text_length: 12345, headline: "发现 2 条待核对：必改 1 条、建议改 1 条、可选 0 条。",
    counts: { P0: 1, P1: 1, P2: 0, total: 2 },
    sections: { references: 6, ref_mode_label: "按编号解析", citations: 4, cite_style_label: "顺序编码制",
                figures: 3, tables: 2 },
    stats: { summary: { checked: 5 }, disclaimer: "不构成任何学术不端判定" },
    findings: [
      { priority: "P0", category: "统计复算", issue: "t(58)=2.35 重算 p≈.022，但写作 p=.30", action: "请核对", context: "…" + XSS + "…" },
      { priority: "P1", category: "引用核对", issue: "文献表中 [6] 在正文里没有被引用", action: "补引用或删去", context: "" },
    ],
    notes: [], disclaimer: "只列请核对清单", markdown: "# 论文提交前规范体检\n",
  };

  // ── 成功用例 ──
  let run = makeRunner({ ok: true, status: 200, json: async () => REPORT });
  await run();
  const h = els.result.innerHTML;
  if (!h.includes("发现 2 条待核对")) fail("成功用例：结论未渲染");
  if (!h.includes("必改 1") || !h.includes("建议改 1")) fail("成功用例：分级计数未渲染");
  if (!h.includes("文献表中 [6]") || !h.includes("重算 p≈.022")) fail("成功用例：问题条目未渲染");
  if (!h.includes("参考文献 6 条") || !h.includes("复算 p 值 5 处")) fail("成功用例：概况行未渲染");
  if (h.includes("<script>")) fail("成功用例：论文摘录里的 <script> 未转义（XSS）");
  if (!h.includes("&lt;script&gt;")) fail("成功用例：摘录内容丢失");
  if (/正在.*体检/.test(els.status.textContent) || !els.status.textContent.includes("12345")) fail("成功用例：状态行未收尾");
  if (els.exportBtn.style.display === "none") fail("成功用例：导出按钮没出现");
  if (global._lastPaperLintMarkdown !== REPORT.markdown) fail("成功用例：导出用的 markdown 未更新");
  if (els.input.value !== "") fail("成功用例：未清空文件框（改完论文再选同一文件不会触发重体检）");

  // ── 失败用例 ──
  run = makeRunner({ ok: false, status: 400, json: async () => ({ ok: false, error: "请上传论文文件或粘贴论文文本。" }) });
  await run();
  if (!els.status.textContent.includes("请上传论文文件")) fail("失败用例：后端 error 文案没有透出到状态行（会静默白屏）");
  if (els.result.innerHTML) fail("失败用例：结果区不应被写入报告");
  if (els.exportBtn.style.display !== "none") fail("失败用例：导出按钮不应出现");
  if (global._lastPaperLintMarkdown) fail("失败用例：不应还能导出上一次的旧报告");

  // ── 干净用例 ──
  run = makeRunner({ ok: true, status: 200, json: async () => Object.assign({}, REPORT, {
    findings: [], counts: { P0: 0, P1: 0, P2: 0, total: 0 }, headline: "未发现规则可识别的问题。" }) });
  await run();
  if (!els.result.innerHTML.includes("没有发现规则可识别的问题")) fail("干净用例：0 条问题时没有给出通过提示");

  console.log("[PASS] paper_lint_probe：规范体检区 成功渲染 / 摘录转义 / 失败透出 / 干净提示 四契约通过");
  process.exit(0);
})().catch(e => fail(`异常：${e.message}`));

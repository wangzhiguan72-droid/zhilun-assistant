/**
 * 前端探针 ③：AI 痕迹自查（runAiAudit）渲染与降级契约
 * =====================================================
 * 从 templates/index.html 抽出 runAiAudit 的真实源码，配上 DOM mock 与
 * fetch mock **真跑两遍**：
 *   成功用例 → 结果区必须渲染出分数与「优先级建议」，状态行不再是"正在扫描"
 *   失败用例 → 必须把后端 error 文案透出给用户，而不是静默白屏
 * 这守护的是 v2.15 自查区的"永不白屏"契约。
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");

// ── 抽函数源码（括号配对）──
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

const escapeHtmlSrc = extractFn(html, "function escapeHtml(s)");
const auditSrc = extractFn(html, "async function runAiAudit()");

// ── DOM mock ──
function makeEl() {
  return { value: "", textContent: "", innerHTML: "", style: {},
           classList: { add() {}, remove() {}, contains() { return false; } },
           files: [{ name: "paper.docx" }], addEventListener() {} };
}
const els = {};
function mockDom() {
  els.status = makeEl();
  els.result = makeEl();
  els.input = makeEl();
  els.input.files = [{ name: "paper.docx" }];
  global.document = {
    getElementById(id) {
      if (id === "aiAuditStatus") return els.status;
      if (id === "aiAuditResult") return els.result;
      if (id === "aiAuditInput") return els.input;
      return makeEl();
    },
    createElement() { return makeEl(); },
    addEventListener() {},
    querySelectorAll() { return []; },
    body: makeEl(),
  };
  global.window = global;
}

function makeRunner(fakeResponse) {
  mockDom();
  const escapeHtml = new Function(`return (${escapeHtmlSrc.replace(/^function escapeHtml/, "function escapeHtml")});`)();
  const runAiAudit = new Function(
    "$", "fetch", "toast", "escapeHtml", "aiAuditInput",
    `"use strict";${auditSrc}\nreturn runAiAudit;`,
  )(
    (id) => (document.getElementById(id)),
    async () => fakeResponse,
    () => {},
    escapeHtml,
    els.input,
  );
  return runAiAudit;
}

(async () => {
  const REPORT = { ok: true, score: 87, level: "高", level_text: "明显模板化",
    breakdown: { "提示词残留": 25 }, suggestions: [{ priority: "P0", issue: "对话残留", action: "删除" }],
    manual_checks: ["示例检查项"], disclaimer: "只评估风格风险" };

  // ── 成功用例 ──
  let runner = makeRunner({ ok: true, status: 200, json: async () => REPORT });
  await runner();
  const okHtml = els.result.innerHTML;
  if (!okHtml.includes("87")) { console.error("[探针] 成功用例：结果区未渲染出分数"); process.exit(1); }
  if (!okHtml.includes("P0") || !okHtml.includes("对话残留")) {
    console.error("[探针] 成功用例：优先级建议未渲染"); process.exit(1); }
  if (/正在.*扫描/.test(els.status.textContent)) {
    console.error("[探针] 成功用例：状态行未收尾"); process.exit(1); }

  // ── 失败用例（后端 400 + error 文案）──
  runner = makeRunner({ ok: false, status: 400, json: async () => ({ ok: false, error: "请上传论文文件或粘贴论文文本。" }) });
  await runner();
  // 契约：失败文案写在**状态行**（aiAuditStatus），不静默白屏
  if (!els.status.textContent.includes("请上传论文文件")) {
    console.error("[探针] 失败用例：后端 error 文案没有透出到状态行（会静默白屏）");
    process.exit(1);
  }
  if (els.result.innerHTML) {
    console.error("[探针] 失败用例：结果区不应被写入旧报告"); process.exit(1);
  }

  console.log("[PASS] ai_audit_probe：自查区 成功渲染 / 失败透出文案 两契约通过");
  process.exit(0);
})().catch(e => { console.error(`[探针] 异常：${e.message}`); process.exit(1); });

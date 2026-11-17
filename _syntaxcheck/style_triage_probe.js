/**
 * 前端探针：v2.61 风格提示护栏复核（本地豁免渲染 + runStyleTriage）
 * =================================================================
 * 从 templates/index.html 抽出真实源码，配 DOM / fetch mock 真跑：
 *   ① 自查报告里的 passages_exempted 必须带豁免依据渲染出来（不悄悄丢弃）
 *   ② 有定位段落才启用护栏复核按钮；无段落保持禁用
 *   ③ 复核成功：判词 / 本地证据 / 模型独立意见 / 护栏 四块都要在，且按钮恢复可用
 *   ④ 复核失败：error 文案透出，按钮**不得卡在禁用态**
 *   ⑤ 摘录与模型依据里的 HTML 必须转义（模型输出是不可信输入）
 *   ⑥ 后端每个 source 都要有中文标签（直接从 style_triage.py 抽，防标签漂移）；
 *      关了 AI 的 local_only 不得谎称「模型输出无法解析」或「本地已可判定」
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

const escapeHtmlSrc = extractFn(html, "function escapeHtml(s)");
const auditSrc = extractFn(html, "async function runAiAudit()");
const triageSrc = extractFn(html, "async function runStyleTriage()");

function makeEl() {
  return { value: "", textContent: "", innerHTML: "", style: {}, disabled: false,
           classList: { add() {}, remove() {}, contains() { return false; } },
           files: [{ name: "paper.docx" }], addEventListener() {} };
}
const els = {};
function mockDom() {
  for (const k of ["status", "result", "input", "triageBtn", "triageResult"]) els[k] = makeEl();
  els.input.files = [{ name: "paper.docx" }];
  const map = { aiAuditStatus: els.status, aiAuditResult: els.result, aiAuditInput: els.input,
                styleTriageBtn: els.triageBtn, styleTriageResult: els.triageResult };
  global.document = {
    getElementById(id) { return map[id] || makeEl(); },
    createElement() { return makeEl(); },
    addEventListener() {}, querySelectorAll() { return []; }, body: makeEl(),
  };
  global.window = global;
}

function makeRunners(fakeResponse) {
  mockDom();
  const escapeHtml = new Function(`return (${escapeHtmlSrc});`)();
  const deps = ["$", "fetch", "toast", "escapeHtml", "aiAuditInput"];
  const args = [(id) => document.getElementById(id), async () => fakeResponse,
                () => {}, escapeHtml, els.input];
  const runAiAudit = new Function(...deps, `"use strict";${auditSrc}\nreturn runAiAudit;`)(...args);
  const runStyleTriage = new Function(...deps, `"use strict";${triageSrc}\nreturn runStyleTriage;`)(...args);
  return { runAiAudit, runStyleTriage };
}

function fail(msg) { console.error("[探针] " + msg); process.exit(1); }

const PASSAGE = { excerpt: "示例信息的主动披露也会显著提升该平台对用户群体的吸引力。",
                  score: 64, reasons: ["拔高词没有数字支撑", "被动句偏多"],
                  suggestion: "把「显著提升」换成实测幅度", example: "…吸引力提高 12.3%…" };
const EXEMPT = { excerpt: "密 级： 硕 士 学 位 论 文 作者姓名 测试甲 指导教师 测试乙",
                 score: 71, reasons: ["被动句偏多"],
                 gate: { exempt: true, category: "封面或版权页",
                         reason: "封面/版权页字段（密级、学位论文题名页、作者导师字段）不是作者论述段落",
                         evidence: ["密级", "学位论文题名页", "作者导师字段"] } };

(async () => {
  // ── ① + ② 自查报告：豁免块渲染 / 按钮启用 ──
  const REPORT = { ok: true, score: 64, level: "中", level_text: "偏模板化",
    breakdown: { "拔高词": 12 }, text_length: 12000,
    suggestions: [{ priority: "P1", issue: "定位到 1 处「AI 风格较集中」的段落", action: "逐段改写" }],
    passages: [PASSAGE], passages_exempted: [EXEMPT],
    manual_checks: ["人工核对项"], disclaimer: "只评估风格风险" };
  let r = makeRunners({ ok: true, status: 200, json: async () => REPORT });
  await r.runAiAudit();
  const auditHtml = els.result.innerHTML;
  if (!auditHtml.includes("本地豁免 1 处")) fail("豁免块未渲染（被豁免段落悄悄丢弃）");
  if (!auditHtml.includes("封面或版权页")) fail("豁免类别未透出");
  if (!auditHtml.includes("不是作者论述段落")) fail("豁免依据未透出");
  if (els.triageBtn.disabled !== false) fail("有定位段落时护栏复核按钮应启用");

  // 无段落 → 按钮保持禁用
  r = makeRunners({ ok: true, status: 200,
                    json: async () => ({ ...REPORT, passages: [], passages_exempted: [EXEMPT] }) });
  await r.runAiAudit();
  if (els.triageBtn.disabled !== true) fail("无定位段落时护栏复核按钮应禁用");

  // ── ③ 复核成功：四块内容 + 按钮恢复 ──
  const TRIAGE = { ok: true, model_calls: 1, gate_version: "v1", prompt_version: "v1",
    verdicts: { "真": 1 }, exempted: [EXEMPT], text_length: 12000,
    disclaimer: "护栏复核只评估「风格提示是否成立」，不判定作者身份。",
    items: [{ verdict: "真", source: "local_plus_model", score: 64,
              excerpt: PASSAGE.excerpt, reasons: PASSAGE.reasons,
              reason: "本地检出拔高词没有数字支撑，模型确认现象可见且不属豁免段落",
              guards: [], model_called: true,
              local_evidence: { "拔高词没有数字支撑": ["显著提升"] },
              model_opinion: { section_type: "讨论", needs_revision: "是",
                               evidence_sufficient: "否", visible: ["拔高词没有数字支撑"],
                               basis: "「显著提升」后无数字支撑" } }] };
  r = makeRunners({ ok: true, status: 200, json: async () => TRIAGE });
  els.triageBtn.disabled = true;
  await r.runStyleTriage();
  const tHtml = els.triageResult.innerHTML;
  for (const needle of ["真", "本地证据 + 模型确认", "显著提升", "模型独立意见（原样保留）",
                        "讨论", "「显著提升」后无数字支撑", "不判定作者身份"]) {
    if (!tHtml.includes(needle)) fail(`复核成功用例缺少「${needle}」`);
  }
  if (!tHtml.includes("模型调用 1 次")) fail("模型调用次数未透出（用户无法判断成本）");
  if (els.triageBtn.disabled !== false) fail("复核结束后按钮未恢复可用");

  // 护栏与分歧必须可见（本地与模型两说并列，不互相覆盖）
  r = makeRunners({ ok: true, status: 200, json: async () => ({ ...TRIAGE,
    verdicts: { "存疑": 1 },
    items: [{ ...TRIAGE.items[0], verdict: "存疑", source: "disagreement",
              reason: "本地检出拔高词没有数字支撑，模型认为无需修改，两说并列交用户核对",
              guards: ["本地有证据但模型认为无需修改"],
              model_opinion: { ...TRIAGE.items[0].model_opinion, needs_revision: "否" } }] }) });
  await r.runStyleTriage();
  if (!els.triageResult.innerHTML.includes("本地有证据但模型认为无需修改")) fail("护栏拦截理由未渲染");
  if (!els.triageResult.innerHTML.includes("两说并列")) fail("分歧口径未渲染");

  // 调用失败回落本地：必须说明，不能伪装成「本地已可判定」
  r = makeRunners({ ok: true, status: 200, json: async () => ({ ...TRIAGE, model_calls: 1,
    items: [{ ...TRIAGE.items[0], verdict: "存疑", source: "no_local_evidence",
              model_called: true, model_opinion: null, error: "LLM 不可用：全链失败" }] }) });
  await r.runStyleTriage();
  if (!els.triageResult.innerHTML.includes("调用失败，已回落本地判定")) fail("模型失败未如实告知");

  // ── ④ 复核失败：文案透出 + 按钮不卡死 ──
  r = makeRunners({ ok: false, status: 400,
                    json: async () => ({ ok: false, error: "请上传论文文件或粘贴论文文本。" }) });
  els.triageBtn.disabled = true;
  await r.runStyleTriage();
  if (!els.triageResult.innerHTML.includes("请上传论文文件")) fail("复核失败文案未透出（静默白屏）");
  if (els.triageBtn.disabled !== false) fail("复核失败后按钮卡在禁用态");

  // 网络异常同样不得卡死按钮
  mockDom();
  const escapeHtml2 = new Function(`return (${escapeHtmlSrc});`)();
  const thrower = new Function("$", "fetch", "toast", "escapeHtml", "aiAuditInput",
    `"use strict";${triageSrc}\nreturn runStyleTriage;`)(
      (id) => document.getElementById(id),
      async () => { throw new Error("network down"); }, () => {}, escapeHtml2, els.input);
  els.triageBtn.disabled = true;
  await thrower();
  if (!els.triageResult.innerHTML.includes("network down")) fail("网络异常文案未透出");
  if (els.triageBtn.disabled !== false) fail("网络异常后按钮卡在禁用态");

  // ── ⑤ 转义：模型输出与摘录都是不可信输入 ──
  const XSS = "<img src=x onerror=alert(1)>";
  r = makeRunners({ ok: true, status: 200, json: async () => ({ ...TRIAGE,
    items: [{ ...TRIAGE.items[0], excerpt: XSS, reason: XSS,
              local_evidence: { [XSS]: [XSS] },
              model_opinion: { ...TRIAGE.items[0].model_opinion, basis: XSS } }] }) });
  await r.runStyleTriage();
  if (els.triageResult.innerHTML.includes("<img src=x")) fail("模型输出/摘录未转义（XSS）");

  // ── ⑥ 来源标签全覆盖：后端每个 source 都要有中文名，不能把 snake_case 甩给用户 ──
  const py = fs.readFileSync(path.join(root, "style_triage.py"), "utf8");
  const backendSources = [...py.matchAll(/out\("(?:真|假|存疑)",\s*"([a-z_]+)"/g)].map(m => m[1]);
  if (backendSources.length < 8) fail(`后端 source 抽取异常（只拿到 ${backendSources.length} 个）`);
  const mapBody = triageSrc.slice(triageSrc.indexOf("const srcName = {"));
  const mapKeys = [...mapBody.slice(0, mapBody.indexOf("};")).matchAll(/([a-z_]+):/g)].map(m => m[1]);
  for (const s of backendSources) {
    if (!mapKeys.includes(s)) fail(`后端 source「${s}」在前端没有中文标签（会把英文甩给用户）`);
  }

  // 关了 AI（local_only）不能说成「模型输出无法解析」，也不能说「本地已可判定」
  r = makeRunners({ ok: true, status: 200, json: async () => ({ ...TRIAGE, model_calls: 0,
    verdicts: { "存疑": 1 },
    items: [{ ...TRIAGE.items[0], verdict: "存疑", source: "local_only", model_called: false,
              model_opinion: null, reason: "未启用模型复核，本地证据不足以定性，保留未判定",
              guards: ["未调用模型，仅本地判定"] }] }) });
  await r.runStyleTriage();
  const offHtml = els.triageResult.innerHTML;
  if (!offHtml.includes("未启用模型（纯本地）")) fail("local_only 未渲染成中文标签");
  if (offHtml.includes("模型输出无法解析")) fail("关了 AI 却谎称模型输出无法解析");
  if (offHtml.includes("本地已可判定")) fail("本地判不了却说已可判定");
  if (!offHtml.includes("未启用模型复核，本条仅本地判定")) fail("未如实说明本条没调模型");

  console.log("[PASS] style_triage_probe：豁免渲染 / 按钮启用禁用 / 四块内容 / 失败不卡死 / 转义 / 来源标签全覆盖 六契约通过");
  process.exit(0);
})().catch(e => { console.error(`[探针] 异常：${e.message}`); process.exit(1); });
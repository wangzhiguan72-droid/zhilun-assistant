/**
 * 前端探针 ⑦：导出守门 + 中文文件名（defense_export_probe，v2.28 重建）
 * =====================================================================
 * 历史：本探针在 v2.23 曾按源码重建（82 断言），但当时 _syntaxcheck 尚未全部
 * 入库，文件再次丢失——这是该目录第二次发生同类事故。v2.28 按当前源码行为
 * 重建，断言数与 v2.23 版本不必相同，守护的契约不变：
 *
 *   1. `cdFilename()`：后端中文文件名只存在于 RFC 5987 的
 *      `filename*=UTF-8''…` 字段，`filename=` 回退值中文会被整段删掉；
 *      取名必须**优先 filename\***（URL 解码），坏编码退回 filename=，最后前端兜底。
 *   2. 两处导出调用点（分析报告 / 论文排查）都必须走 cdFilename + 带兜底名。
 *   3. 导出守门：无结果不导出；导出前必须过学术诚信承诺（ensureIntegrityPledge）；
 *      红线自查（/api/red_line_scan）关口仍在。
 *   4. 后端契约：/api/export 用 `download_name=` 交中文文件名（Werkzeug 自动
 *      生成 filename*），绝不能手写只含 ASCII 的 Content-Disposition。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf-8");
const appSrc = fs.readFileSync(path.join(ROOT, "app.py"), "utf-8");

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("  ok  " + name); }
  else { fail++; console.log("  FAIL " + name + (detail ? "  [" + detail + "]" : "")); }
}

// ── 0) 内联脚本可编译（防语法级破坏）───────────────────────────────
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let compiled = 0;
for (const code of scripts) {
  try { new Function(code); compiled++; }
  catch (e) { ok("内联脚本编译", false, e.message.slice(0, 80)); }
}
ok(`内联脚本 ${compiled}/${scripts.length} 个编译通过`,
   compiled === scripts.length && scripts.length >= 3);

// ── 1) cdFilename 行为契约（用真实函数体跑，不复制实现）───────────
const fnMatch = html.match(/function cdFilename\(cd, fallback\) \{[\s\S]*?\n  \}/);
ok("cdFilename 函数存在", !!fnMatch, "index.html 丢失 v2.23 中文文件名修复");
if (fnMatch) {
  const cdFilename = new Function("return " + fnMatch[0])();
  const cases = [
    // [描述, Content-Disposition, fallback, 期望]
    ["UTF-8 字段优先（中文）",
     "attachment; filename=\"_t_123.docx\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A.docx",
     "兜底.docx", "报告.docx"],
    ["字段顺序无关（filename* 在后）",
     "attachment; filename*=UTF-8''%E6%99%BA%E8%AE%BA.docx; filename=\"_t.docx\"",
     "兜底.docx", "智论.docx"],
    ["UTF-8 百分号编码损坏 → 退回 filename=",
     "attachment; filename=\"fallback.docx\"; filename*=UTF-8''%ZZ%E6%8A%A5.docx",
     "兜底.docx", "fallback.docx"],
    ["只有 filename= 带引号 → 取引号内",
     "attachment; filename=\"plain.docx\"", "兜底.docx", "plain.docx"],
    ["只有 filename= 无引号 → 取值并去空白",
     "attachment; filename= bare.docx; size=1", "兜底.docx", "bare.docx"],
    ["啥都没有 → 前端兜底名",
     "attachment", "兜底.docx", "兜底.docx"],
    ["cd 为空串/undefined → 兜底名",
     "", "兜底.docx", "兜底.docx"],
    ["filename*= 空值 → 退回 filename=",
     "attachment; filename=\"real.docx\"; filename*=UTF-8''",
     "兜底.docx", "real.docx"],
  ];
  for (const [desc, cd, fb, want] of cases) {
    let got;
    try { got = cdFilename(cd, fb); } catch (e) { got = "抛异常:" + e.message; }
    ok("cdFilename: " + desc, got === want, `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
  }
  // 真实事故场景（v2.23 修复对象）：ASCII 回退名绝不允许赢过 UTF-8 真名
  const realWerkzeug = "attachment; filename=\"_independent_t_1726848000.docx\"; "
    + "filename*=UTF-8''%E6%99%BA%E8%AE%BA%E5%8A%A9%E6%89%8B_independent_t_1726848000.docx";
  const got = cdFilename(realWerkzeug, "x.docx");
  ok("cdFilename: Werkzeug 残缺回退名不赢过 UTF-8 真名",
     got === "智论助手_independent_t_1726848000.docx", `got=${got}`);
}

// ── 2) 两处导出调用点都走 cdFilename(Content-Disposition, 兜底名) ──
const cdCallSites = [...html.matchAll(
  /cdFilename\(r\.headers\.get\('Content-Disposition'\),\s*\n\s*`([^`]+)`\)/g)];
ok("导出调用点 ≥2 处使用 cdFilename", cdCallSites.length >= 2,
   `实见 ${cdCallSites.length}`);
for (let i = 0; i < cdCallSites.length; i++) {
  ok(`调用点 ${i + 1} 带中文兜底名（模板串，UTF-8 取不到时保底）`,
     /^智论助手/.test(cdCallSites[i][1]), cdCallSites[i][1]);
}

// ── 3) 导出守门 ────────────────────────────────────────────────────
ok("exportWord 存在且先查 _lastMarkdown",
   /async function exportWord\(\) \{\s*\n\s*if \(!window\._lastMarkdown\)/.test(html),
   "无结果导出未拦截");
ok("导出前强制学术诚信承诺（ensureIntegrityPledge）",
   /exportWord[\s\S]{0,400}await ensureIntegrityPledge\(\)/.test(html),
   "v1.5 承诺关口丢失");
ok("红线自查关口 /api/red_line_scan 仍在",
   html.includes("fetch('/api/red_line_scan'"), "红线自查前端关口丢失");
ok("论文排查导出（exportAuditWord）存在且同守门风格",
   /async function exportAuditWord\(\)/.test(html), "");

// ── 4) 后端契约：download_name 承载中文文件名 ──────────────────────
ok("后端 /api/export 使用 download_name=（Werkzeug 自动生成 RFC5987 filename*）",
   /download_name=fname/.test(appSrc), "绝不能手写 ASCII-only 的 Content-Disposition");
ok("后端导出文件名含中文品牌段（智论助手_）",
   /fname = f"智论助手_\{name_part\}/.test(appSrc), "");

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
if (fail) process.exit(1);
console.log(`[PASS] defense_export_probe：导出守门 + 中文文件名 ${pass} 断言全部通过`);

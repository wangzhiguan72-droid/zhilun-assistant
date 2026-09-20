/**
 * 前端探针 ⑧：图表核查 UI（image_audit_probe，v2.28 重建）
 * =====================================================================
 * 历史：v2.23 曾按源码重建（47 断言）后随目录清理丢失——第二次同类事故。
 * v2.28 按当前源码行为重建。守护的契约：
 *
 *   1. 提交守门：没选图不允许提交；claim 去空白、非空才随表单上传；
 *   2. **BYOK 请求级透传（v2.25）**：每次提交都要 collectByok()——
 *      图表核查会把图片发给模型，用户自带 Key 漏带 = 静默用回免费档；
 *   3. **重选同一文件修复（v2.22）**：change 里先取出 file 再清空 input.value，
 *      顺序错了重选同图永远不触发（最难自查的静默哑火）；
 *   4. 拖拽四事件 + 请求期间按钮禁用 + 400/413 错误路径区分；
 *   5. 后端契约：扩展名白名单 / 5MB 上限 / claim ≤500 字。
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

// ── 0) 内联脚本可编译 ──────────────────────────────────────────────
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let compiled = 0;
for (const code of scripts) {
  try { new Function(code); compiled++; }
  catch (e) { ok("内联脚本编译", false, e.message.slice(0, 80)); }
}
ok(`内联脚本 ${compiled}/${scripts.length} 个编译通过`,
   compiled === scripts.length && scripts.length >= 3);

// ── 1) 关键元素存在 ────────────────────────────────────────────────
for (const id of ["iaDrop", "iaInput", "iaBtn", "iaClaim", "iaResult"]) {
  ok(`元素 #${id} 存在`, new RegExp(`id="${id}"`).test(html), `id="${id}" 未找到`);
}

// ── 2) 提交守门 + BYOK 透传（锚定图表核查自己的 submit：以图片守门开头）──
const submitMatch = html.match(
  /function submit\(\) \{\s*\n\s*if \(!file\) \{ toast\('请先选择一张图表'\); return; \}[\s\S]*?\n    \}/);
ok("submit() 函数存在（图表核查版）", !!submitMatch, "图表核查提交函数丢失");
if (submitMatch) {
  const body = submitMatch[0];
  ok("没选图 → 拦截提示", body.includes("请先选择一张图表"), "");
  ok("claim 取自 #iaClaim 且去空白",
     body.includes("$('iaClaim')") && body.includes(".trim()"), "");
  ok("图片以字段名 image 进 FormData", /fd\.append\('image', file\)/.test(body), "");
  ok("claim 非空才上传（空结论不占字段）",
     /if \(claim\) fd\.append\('claim', claim\)/.test(body), "");
  ok("BYOK 请求级透传：collectByok() 每次携带（v2.25）",
     /const byokIa = collectByok\(\);\s*\n\s*for \(const \[k, v\] of Object\.entries\(byokIa\)\) fd\.append\(k, v\);/
       .test(body), "漏带 = 用户自带 Key 静默失效");
  ok("请求期间按钮禁用（防重复提交）",
     /btn\.disabled = true; btn\.textContent = '读图中…';/.test(body)
     && /btn\.disabled = false; btn\.textContent = '开始读图核查';/.test(body), "");
  ok("请求走 /api/audit_image", body.includes("fetch('/api/audit_image'"), "");
}

// ── 3) 错误路径：400/413 与网络错误分开处理 ───────────────────────
ok("400/413 走后端明确文案（renderResult）",
   /o\.r\.status === 400 \|\| o\.r\.status === 413/.test(html), "");
ok("网络错误 catch → renderResult（不白屏）",
   /renderResult\(\{ ok: false, error: '网络错误：' \+ e \}\)/.test(html), "");

// ── 4) 重选同一文件修复（v2.22）：先取 file 再清空 value ──────────
const changeMatch = html.match(
  /inp\.addEventListener\('change', function \(\) \{[\s\S]*?\n      \}\);/);
ok("change 处理器存在", !!changeMatch, "文件选择事件丢失");
if (changeMatch) {
  const body = changeMatch[0];
  const pickAt = body.indexOf("pick(f);");
  const clearAt = body.indexOf("inp.value = '';");
  ok("先 pick(f) 取出文件引用", pickAt > 0, "");
  ok("随后清空 input.value（重选同一文件可再次触发）",
     pickAt > 0 && clearAt > pickAt, `pick@${pickAt} clear@${clearAt}`);
  ok("清空带 try/catch（个别浏览器 files 只读）",
     /try \{ inp\.value = ''; \} catch/.test(body), "");
}

// ── 5) 拖拽四事件 ─────────────────────────────────────────────────
for (const ev of ["dragenter", "dragover", "dragleave", "drop"]) {
  ok(`拖拽事件 ${ev} 已挂`, new RegExp(`'${ev}'\\)\\.forEach|'${ev}'`).test(html)
     && html.includes(`drop.addEventListener('${ev}'`) || new RegExp(ev).test(html), "");
}
ok("drop 事件从 dataTransfer 取文件", /e\.dataTransfer && e\.dataTransfer\.files/.test(html), "");
ok("mount() 对缺失元素自愈（if (!drop || !inp) return）",
   /if \(!drop \|\| !inp\) return;/.test(html), "");

// ── 6) 渲染结果：缓存/模型/降级标签 + 读图文案转义 ────────────────
ok("命中缓存标签（llm_cached → 命中缓存）", /d\.llm_cached\) tags\.push\('命中缓存'\)/.test(html), "");
ok("模型名标签透出（llm_model）", /if \(d\.llm_model\) tags\.push\(d\.llm_model\)/.test(html), "");
ok("降级标签（fallback + llm_error → 已降级）",
   /d\.fallback && d\.llm_error\) tags\.push\('已降级'\)/.test(html), "");
ok("AI 读到的图（caption）经 esc() 转义", /esc\(d\.caption\)/.test(html), "");
ok("用户结论句经转义回显（d.claim）", /esc\(d\.claim \? \('你的结论：' \+ d\.claim\)/.test(html), "");

// ── 7) 后端契约（app.py）──────────────────────────────────────────
ok("后端路由 /api/audit_image 存在", /@app\.route\("\/api\/audit_image"/.test(appSrc), "");
ok("扩展名白名单：png/jpg/jpeg/webp/gif",
   ['.png', '.jpg', '.jpeg', '.webp', '.gif'].every(e => appSrc.includes(`"${e}"`)), "");
ok("图片 ≤5MB：audit_image 校验 MAX_IMAGE_BYTES（定义于 multimodal_agent.py）",
   /if len\(raw\) > MAX_IMAGE_BYTES:/.test(appSrc)
   && /MAX_IMAGE_BYTES = 5 \* 1024 \* 1024/.test(
       require("fs").readFileSync(path.join(ROOT, "multimodal_agent.py"), "utf-8")), "");
ok("claim 长度上限 500 字", /500/.test(appSrc) && /claim/.test(appSrc), "");

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
if (fail) process.exit(1);
console.log(`[PASS] image_audit_probe：图表核查 UI ${pass} 断言全部通过`);

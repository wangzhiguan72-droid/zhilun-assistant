/**
 * 前端探针 ②：BYOK 字段前后端契约一致
 * ====================================
 * 验证 templates/index.html 的 collectByok FIELD_MAP（前端收集哪些 Key 输入框）
 * 与 app.py 的 _BYOK_FIELDS（后端认哪些字段名）**完全一致**。
 * 这是 BYOK 六家平台的单一契约——历史上出现过"前端加了输入框、后端不认"
 * 的静默失效（填了 Key 也不生效，用户完全看不出来）。
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");
const appSrc = fs.readFileSync(path.join(root, "app.py"), "utf8");

// ── 后端：_BYOK_FIELDS: tuple[tuple[str, str], ...] = ( (...) , ... ) ──
// 起点定位到赋值行，终点用**括号配对**找（内层元组自带括号，不能找第一个右括号）
const defStart = appSrc.indexOf("_BYOK_FIELDS");
if (defStart < 0) { console.error("[探针] app.py 找不到 _BYOK_FIELDS"); process.exit(1); }
const eq = appSrc.indexOf("=", defStart);
let depth = 0, defEnd = -1, started = false;
for (let j = eq; j < appSrc.length; j++) {
  if (appSrc[j] === "(") { depth++; started = true; }
  if (appSrc[j] === ")") { depth--; if (started && depth === 0) { defEnd = j; break; } }
}
if (defEnd < 0) { console.error("[探针] _BYOK_FIELDS 括号不配对"); process.exit(1); }
const backendBlock = appSrc.slice(defStart, defEnd);
const backendFields = [...backendBlock.matchAll(/"([a-z]+_key)"/g)].map(m => m[1]);
if (backendFields.length < 6) {
  console.error(`[探针] app.py _BYOK_FIELDS 只抽到 ${backendFields.length} 个字段，抽取逻辑疑似失效`);
  process.exit(1);
}

// ── 前端：var FIELD_MAP = { settingsKey_x: 'field', ... } ──
const fmStart = html.indexOf("var FIELD_MAP = {");
if (fmStart < 0) { console.error("[探针] index.html 找不到 FIELD_MAP"); process.exit(1); }
const fmEnd = html.indexOf("};", fmStart);
const frontendFields = [...html.slice(fmStart, fmEnd).matchAll(/'([a-z]+_key)'/g)].map(m => m[1]);

// ── 集合一致（双向）──
const onlyBackend = backendFields.filter(f => !frontendFields.includes(f));
const onlyFrontend = frontendFields.filter(f => !backendFields.includes(f));
if (onlyBackend.length || onlyFrontend.length) {
  console.error(`[探针] BYOK 字段前后端漂移：仅后端有=${JSON.stringify(onlyBackend)} 仅前端有=${JSON.stringify(onlyFrontend)}`);
  console.error("修法：app.py 的 _BYOK_FIELDS 与 index.html 的 FIELD_MAP 同步（两端各一行）");
  process.exit(1);
}

console.log(`[PASS] byok_fields_probe：BYOK ${backendFields.length} 个字段前后端完全一致`);

// ── 透传点契约（v2.25）：BYOK 改为请求级隔离后，服务端不再持久保存用户 Key，
//    每个 LLM 入口的每次请求都必须带上 collectByok()，漏带 = 用户 Key 静默失效
//    （回退服务端免费档，用户以为自己在用自己的 Key）。 ──
const attachPoints = [
  ["/api/analyze", /Object\.assign\(payload, collectByok\(\)\)/],
  ["/api/check_paper", /const byok = collectByok\(\)/],
  ["/api/audit_chat", /Object\.assign\(\{ summary: summary, question: q \}, collectByok\(\)\)/],
  ["/api/audit_image", /const byokIa = collectByok\(\)/],
];
for (const [endpoint, re] of attachPoints) {
  if (!re.test(html)) {
    console.error(`[探针] ${endpoint} 前端调用未带 collectByok() —— 请求级隔离后 BYOK 会静默失效`);
    process.exit(1);
  }
}
console.log(`[PASS] byok_fields_probe：${attachPoints.length} 个 LLM 入口全部透传 collectByok()`);

// ── v2.52 补记：浏览器存储写入失败时不得谎报「已保存」──
//   真实故障：localStorage 被禁 / 配额满 → setItem 抛异常，旧代码 catch 里空着，
//   随后照样 toast「模型设置已保存」。用户以为下次打开还在，实际刷新即丢。
//   正确行为：Key 仍在输入框里（collectByok 直接读 DOM，本次会话可用），
//   但必须如实告知「刷新后需重填」。
const skStart = html.indexOf("window.saveKeys = function");
if (skStart < 0) { console.error("[探针] 找不到 window.saveKeys"); process.exit(1); }
const skEnd = html.indexOf("  };", skStart);
const saveKeysBody = html.slice(skStart, skEnd);
if (!/storeOk\s*=\s*false/.test(saveKeysBody)) {
  console.error("[探针] saveKeys 未记录 localStorage 写入失败（storeOk 标记缺失）");
  process.exit(1);
}
if (!/if \(!storeOk\)/.test(saveKeysBody) || !/刷新后需重填/.test(saveKeysBody)) {
  console.error("[探针] saveKeys 存储失败时未给出如实提示（仍可能谎报已保存）");
  process.exit(1);
}
if (/setItem\(LS[^)]*\);\s*\}\s*catch \(e\) \{\s*\}/.test(saveKeysBody)) {
  console.error("[探针] saveKeys 仍有静默 catch —— 写入失败会被吞掉");
  process.exit(1);
}
console.log("[PASS] byok_fields_probe：存储失败不再谎报「已保存」");

/**
 * 前端探针 ①：methodLabel 与 methods_registry 单一真源契约
 * ========================================================
 * CI 的 Front-end probes 步骤会自动发现 *_probe.js 并用 node 真跑。
 * 本探针验证：templates/index.html 里的 methodLabel() 对**内置注册表的
 * 每一个 key** 都能给出中文 label（≠ key 本身）——防止"注册表加了方法、
 * 前端映射忘了同步"这种漂移（历史上真发生过：Pearson 相关 vs 相关分析）。
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");
const registrySrc = fs.readFileSync(path.join(root, "methods_registry.py"), "utf8");

// ── 从注册表抽全部内置 key（MethodSpec(key="...")）──
const registryKeys = [...registrySrc.matchAll(/key="([a-z][a-z0-9_]*)"/g)].map(m => m[1]);
if (registryKeys.length < 10) {
  console.error(`[探针] 从 methods_registry.py 只抽到 ${registryKeys.length} 个 key，抽取逻辑疑似失效`);
  process.exit(1);
}

// ── 从 index.html 抽主 methodLabel 函数源码（括号配对找函数体）──
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

const fnSrc = extractFn(html, "function methodLabel(key)");

// ── 极简 window mock（methodLabel 只依赖 window.__pluginLabels）──
const sandboxWindow = { __pluginLabels: {} };
const methodLabel = new Function("window", `return (${fnSrc.replace(/^function methodLabel/, "function methodLabel")});`)(sandboxWindow);

// ── 断言：每个内置 key 都有 ≠ key 本身的中文 label ──
const missing = registryKeys.filter(k => {
  const label = methodLabel(k);
  return !label || label === k;
});
if (missing.length) {
  console.error(`[探针] methodLabel 缺少以下注册表 key 的中文映射：${missing.join(", ")}`);
  console.error("修法：在 templates/index.html 的 methodLabel 映射表里补齐（单一真源是 methods_registry.py）");
  process.exit(1);
}

// ── 反向抽查：映射表里的每个 key 都真实存在于注册表（防止删了方法留死映射）──
const labelSrc = fnSrc;
const mapped = [...labelSrc.matchAll(/^\s{4,}([a-z_0-9]+):\s*'/gm)].map(m => m[1]);
const unknown = mapped.filter(k => !registryKeys.includes(k));
if (unknown.length) {
  console.error(`[探针] methodLabel 里有注册表中不存在的死映射：${unknown.join(", ")}`);
  process.exit(1);
}

console.log(`[PASS] method_label_probe：注册表 ${registryKeys.length} 个 key 全部有中文映射，映射表无死项（${mapped.length} 条）`);

/**
 * 前端探针 ⑥：向导函数族完整性（防 v2.26 合并事故回退）
 * ======================================================
 * 真实事故（2026-09-18 修）：index.html 曾出现两个 `function updateWizard()` ——
 * 旧版未闭合，新版连同 validateMethod/methodDesc/setStep4/resetStep4 全部变成
 * 它的嵌套函数，`filterSelectByType` 的声明行丢失只剩孤体（`const sel = $(selId)`）。
 * 文件**语法完全合法**（嵌套函数声明是合法 JS），node --check 抓不到；
 * 但运行时选完因变量即 ReferenceError，runBtn 永远 disabled —— 主流程全灭。
 *
 * 本探针两道防线：
 *   1. 所有内联 <script> 块用 new Function() 做语法编译（抓真语法错误）
 *   2. 向导函数族每个名字**全文件只能声明一次**（抓「嵌套吞并 + 声明丢失」）
 */
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "..");

const html = fs.readFileSync(path.join(root, "templates", "index.html"), "utf8");

// ── 1. 内联脚本语法编译（跳过 src= 外链块）──
const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
if (blocks.length === 0) { console.error("[探针] 没找到任何内联 script 块"); process.exit(1); }
for (let i = 0; i < blocks.length; i++) {
  try {
    new Function(blocks[i][1]);   // 只编译不执行
  } catch (e) {
    console.error(`[探针] 第 ${i + 1} 个内联 script 块语法错误：${e.message}`);
    process.exit(1);
  }
}

// ── 2. 向导函数族：每个名字恰好声明一次（嵌套吞并会导致目标函数 0 次、
//        被吞的函数 2 次——两种情况都能抓到）──
const REQUIRED = [
  "updateWizard", "validateMethod", "methodDesc",
  "setStep4", "resetStep4", "filterSelectByType", "getColType",
];
let bad = 0;
for (const name of REQUIRED) {
  const re = new RegExp(`function\\s+${name}\\s*\\(`, "g");
  const n = (html.match(re) || []).length;
  if (n !== 1) {
    console.error(`[探针] function ${name}( 声明了 ${n} 次（应为 1 次）——疑似合并事故/嵌套吞并`);
    bad++;
  }
}

// ── 3. 向导函数族的调用点必须存在（声明有了、没人调用也是断链）──
for (const name of ["updateWizard", "validateMethod", "filterSelectByType"]) {
  const callRe = new RegExp(`[^\\w]${name}\\s*\\(`, "g");
  const calls = (html.match(callRe) || []).length;
  if (calls < 2) {   // 1 次是声明本身，至少还应有 1 次真调用
    console.error(`[探针] ${name} 只有声明没有调用（calls=${calls}），向导链路疑似断掉`);
    bad++;
  }
}

if (bad) process.exit(1);
console.log(`[PASS] wizard_integrity_probe：${blocks.length} 个内联脚本语法通过，向导函数族 ${REQUIRED.length} 个声明各一次、调用链完整`);

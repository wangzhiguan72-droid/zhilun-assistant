"""v2.0 · 数据体检「前端渲染」行为探针（datacheck UI probe）
================================================================
为什么需要：`node --check` 只能保证 templates/index.html 的内联脚本**语法**正确，
查不出 `renderDataCheck` 的**逻辑**错误（字段取错、忘记转义、空数据崩掉）。
本测试把该函数从 HTML 里抽出来，用极简 DOM mock 真跑一遍。

覆盖：
  1. 高风险度报告：标题 / 行×列标签 / 三条文案 / 已跑检测 / 免责声明
  2. 无 issue：绿标 + 兜底文案
  3. XSS：标题与证据里的尖括号必须被转义（防注入）
  4. 容错：summary 为空对象时不抛异常（后端契约变更时的最后一道防线）

生成的探针脚本保留在 `_syntaxcheck/datacheck_probe.js`，可直接 node 运行、便于排查。

跑法：.venv/Scripts/python.exe datacheck_ui_test.py
"""
import re
import subprocess
import sys
from pathlib import Path

HTML_PATH = Path('templates/index.html')
PROBE_DIR = Path('_syntaxcheck')
PROBE_PATH = PROBE_DIR / 'datacheck_probe.js'
NODE = r'C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'


def _extract_fn(lines: list[str], sig: str) -> str:
    """按行抽取一个顶层函数源码（结束判据：缩进两格的 `}`）。"""
    try:
        start = next(i for i, l in enumerate(lines) if sig in l)
    except StopIteration:
        raise SystemExit(f'✗ 在 templates/index.html 里找不到 {sig!r}（检查是否被改名/删除）')
    for j in range(start + 1, len(lines)):
        if lines[j] == '  }':
            return '\n'.join(lines[start:j + 1])
    raise SystemExit(f'✗ {sig!r} 找不到结束行，文件可能不完整')


def main() -> int:
    html = HTML_PATH.read_text(encoding='utf-8')
    blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, flags=re.S | re.I)
    if not blocks:
        raise SystemExit('✗ templates/index.html 里没有内联脚本块')
    lines = blocks[0].split('\n')

    escape_html = _extract_fn(lines, 'function escapeHtml(')
    render_dc = _extract_fn(lines, 'function renderDataCheck(data) {')
    render_fix = _extract_fn(lines, 'function renderDataFix(res) {')

    probe = _PROBE_TEMPLATE % '\n\n'.join([escape_html, '', render_dc, '', render_fix])
    PROBE_DIR.mkdir(exist_ok=True)
    PROBE_PATH.write_text(probe, encoding='utf-8')

    r = subprocess.run([NODE, str(PROBE_PATH)], capture_output=True, text=True, encoding='utf-8')
    print(r.stdout)
    if r.stderr.strip():
        print('--- node stderr ---')
        print(r.stderr[:3000])
    return r.returncode


_PROBE_TEMPLATE = """
%s

// ================= 极简 DOM mock =================
const els = {};
function makeEl(id) {
  const el = {
    id: id, innerHTML: '', style: {}, _cls: new Set(),
    classList: {
      toggle(n, v) { if (v) el._cls.add(n); else el._cls.delete(n); },
      add(n) { el._cls.add(n); }, remove(n) { el._cls.delete(n); },
      contains(n) { return el._cls.has(n); },
    },
    addEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  return el;
}
function $(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; }
global.$ = $;
global.document = { createElement: () => makeEl('tmp'), body: { appendChild() {} }, addEventListener() {} };
global.window = { addEventListener() {} };
global.escapeHtml = escapeHtml;

let PASS = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ok   ' + name); }
  else { FAIL++; console.log('  FAIL ' + name + (extra ? '  <<< ' + extra : '')); }
}

console.log('=== 1. 高风险度报告 ===');
renderDataCheck({
  summary: { rows: 30, cols: 9, high: 1, mid: 2, low: 0,
             verdict: '发现 1 处高优先级问题', disclaimer: '只提示可疑',
             checks_run: ['合计一致性', '重复行'] },
  issues: [
    { level: 'high', category: '一致性', title: '「总分」与分项之和对不上',
      evidence: '第 7 行对不上', explain: '说明', suggestion: '建议核对' },
    { level: 'mid', category: '规律', title: '差值过于规律',
      evidence: '恒为 +5', explain: 'e2', suggestion: 's2' },
  ],
});
let html = els['datacheckBox'].innerHTML;
check('含「数据体检」', html.includes('数据体检'));
check('含行×列标签', html.includes('30 行 × 9 列'), html.slice(0, 160));
check('含 high 标题与计数', html.includes('发现 1 处高优先级问题'));
check('严重度标记与样式类', html.includes('lv-high') && html.includes('🔴 高'));
check('证据/解释/建议三段齐全',
      html.includes('证据：') && html.includes('意味着什么：') && html.includes('建议：'));
check('列出已跑检测', html.includes('合计一致性 / 重复行'));
check('含免责声明', html.includes('只提示可疑'));
check('mid 条也渲染', html.includes('🟡 中'));

console.log('=== 2. 干净数据：无 issue ===');
renderDataCheck({
  summary: { rows: 40, cols: 5, high: 0, mid: 0, low: 0,
             verdict: '未发现明显数据问题', disclaimer: 'D', checks_run: ['a'] },
  issues: [],
});
html = els['datacheckBox'].innerHTML;
check('绿标 + 未发现明显数据问题', html.includes('🟢') && html.includes('未发现明显数据问题'));
check('空列表给兜底文案', html.includes('逐项检查未发现问题'));
check('默认折叠（不含 open 类）', !els['dcWrap']._cls.has('open'));

console.log('=== 3. XSS 转义 ===');
renderDataCheck({
  summary: { rows: 1, cols: 1, high: 1, mid: 0, low: 0, verdict: 'v', disclaimer: 'd', checks_run: [] },
  issues: [{ level: 'high', title: '<img src=x onerror=alert(1)>',
             evidence: 'a<b>c', explain: 'e', suggestion: 's' }],
});
html = els['datacheckBox'].innerHTML;
check('标题里的标签被转义', !html.includes('<img src=x') && html.includes('&lt;img'));
check('证据里的尖括号被转义', html.includes('a&lt;b&gt;c'));

console.log('=== 4. 字段缺失容错 ===');
let threw = false;
try { renderDataCheck({}); } catch (e) { threw = true; console.log('    err=' + e.message); }
check('summary 为空对象时不抛异常', !threw);

console.log('=== 5. 清洗副本：自动 + 人工 ===');
renderDataFix({
  rows: 9, cols: 4, changes_truncated: false,
  stats: { cells_changed: 3, rows_removed: 1, auto_actions: 2, manual_actions: 1 },
  actions: [
    { kind: 'reverse_score', auto: true, level: 'mid', title: '对 1 个反向题做了反向计分',
      detail: '按 (1+7) − 原值折算', columns: ['q4'], rows: [], n: 3 },
    { kind: 'dedup_exact', auto: true, level: 'mid', title: '删除 1 行完全重复记录',
      detail: '只保留第一条', columns: ['编号', '得分'], rows: [10], n: 1 },
    { kind: 'sum_overwrite', auto: false, level: 'high', title: '建议核对「总分」与分项之和',
      detail: '未自动修改', columns: ['总分', '分项1'], rows: [7], n: 1 },
  ],
});
html = els['dcFix'].innerHTML;
check('含「清洗副本已生成」', html.includes('清洗副本已生成'));
check('清洗后行×列标签', html.includes('9 行 × 4 列'), html.slice(0, 160));
check('自动/人工分组标题', html.includes('已自动修复 2 项') && html.includes('需人工确认（未自动改）'));
check('自动徽标', html.includes('dc-badge auto') && html.includes('已自动处理'));
check('人工徽标', html.includes('dc-badge manual'));
check('统计文案', html.includes('共改动 3 个单元格') && html.includes('删除 1 行'));
check('下载按钮', html.includes('下载清洗副本'));

console.log('=== 6. 清洗副本：无动作 ===');
renderDataFix({ rows: 5, cols: 2,
  stats: { cells_changed: 0, rows_removed: 0, auto_actions: 0, manual_actions: 0 }, actions: [] });
html = els['dcFix'].innerHTML;
check('无动作给兜底文案', html.includes('未发现需要处理的问题'));
check('仍给下载按钮', html.includes('下载清洗副本'));

console.log('=== 7. 清洗副本：XSS 转义 ===');
renderDataFix({ rows: 1, cols: 1, stats: {},
  actions: [{ kind: 'manual', auto: false, level: 'high',
              title: '<script>x</script>', detail: 'a<b>', columns: ['<i>'], n: 0 }] });
html = els['dcFix'].innerHTML;
check('动作标题被转义', !html.includes('<script>x') && html.includes('&lt;script&gt;'));
check('列名被转义', html.includes('&lt;i&gt;'));

console.log('');
console.log('结果：' + PASS + ' 通过 / ' + FAIL + ' 失败');
process.exit(FAIL ? 1 : 0);
"""


if __name__ == '__main__':
    sys.exit(main())

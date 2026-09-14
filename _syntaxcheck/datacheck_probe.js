
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }



  function renderDataCheck(data) {
    const box = $('datacheckBox');
    if (!box) return;
    const s = data.summary || {};
    const issues = data.issues || [];
    _dcOpen = issues.length > 0;
    // v2.16：供「协作审阅」分享用。markdown 由后端 datacheck.render_markdown 产出，
    // 前端**不自己拼报告**（避免和 CLI / 流水线 / 导出口径不一致）。
    window._lastDatacheckMarkdown = data.markdown || '';
    window._lastDatacheckIssues = issues;

    const lv = s.high > 0 ? 'lv-high' : (s.mid > 0 ? 'lv-mid' : 'lv-ok');
    const icon = s.high > 0 ? '🔴' : (s.mid > 0 ? '🟡' : '🟢');
    const title = s.high > 0 ? '发现 ' + s.high + ' 处高优先级问题'
      : (s.mid > 0 ? s.mid + ' 处可疑规律待确认' : '未发现明显数据问题');

    const cards = issues.map(function (it) {
      const sev = it.level === 'high' ? '🔴 高' : (it.level === 'mid' ? '🟡 中' : '🔵 提示');
      return '<div class="dc-issue lv-' + escapeHtml(it.level) + '">'
        + '<div class="dc-t2">' + sev + ' · ' + escapeHtml(it.title) + '</div>'
        + (it.evidence ? '<div class="dc-row"><b>证据：</b>' + escapeHtml(it.evidence) + '</div>' : '')
        + (it.explain ? '<div class="dc-row"><b>意味着什么：</b>' + escapeHtml(it.explain) + '</div>' : '')
        + (it.suggestion ? '<div class="dc-row"><b>建议：</b>' + escapeHtml(it.suggestion) + '</div>' : '')
        + '</div>';
    }).join('');

    box.innerHTML =
      '<div class="dc-wrap' + (_dcOpen ? ' open' : '') + '" id="dcWrap">'
      + '<div class="dc-hd ' + lv + '" id="dcHd">'
      + '<span class="dc-t">' + icon + ' 数据体检'
      + '<span class="dc-tag">' + escapeHtml(String(s.rows)) + ' 行 × ' + escapeHtml(String(s.cols)) + ' 列</span>'
      + escapeHtml(title) + '</span>'
      + '<span class="dc-v"><span class="dc-caret">▶</span> ' + (issues.length ? issues.length + ' 条' : '展开') + '</span>'
      + '</div>'
      + '<div class="dc-verdict">' + escapeHtml(s.verdict || '') + '</div>'
      + '<div class="dc-issues">' + (cards || '<div class="dc-row">逐项检查未发现问题。</div>') + '</div>'
      + '<div class="dc-note">' + escapeHtml(s.disclaimer || '')
      + ' · 已跑检测：' + escapeHtml((s.checks_run || []).join(' / ')) + '</div>'
      + (issues.length
          ? '<div class="dc-fixbar"><button class="dc-btn" id="dcFixBtn">🩹 生成清洗副本</button>'
            + '<span class="dc-stat">自动修掉能确定的（重复行 / 反向计分），其余逐条给建议 —— 原文件绝不动。</span></div>'
          : '')
      + '<div class="dc-fix" id="dcFix" style="display:none"></div>'
      // v2.16：体检报告也能分享 —— 它是产品入口第一站，
      // 比论文排查更适合先发给导师/同门看（"我的数据本身有没有问题"）。
      + '<div class="dc-fixbar">'
      + '<button class="dc-btn" id="dcShareBtn" onclick="ReviewShare.fromDatacheck()">🔗 协作审阅</button>'
      + '<span class="dc-stat">把这份体检报告变成链接发出去 —— 内容只存本机，不上传云端。</span>'
      + '</div>'
      + '</div>';

    const hd = $('dcHd'), wrap = $('dcWrap');
    if (!hd || !wrap) return;
    const toggleBody = function (show) {
      wrap.querySelectorAll('.dc-verdict, .dc-issues, .dc-note, .dc-fixbar, .dc-fix').forEach(function (el) {
        el.style.display = show ? '' : 'none';
      });
    };
    hd.addEventListener('click', function () {
      _dcOpen = !_dcOpen;
      wrap.classList.toggle('open', _dcOpen);
      toggleBody(_dcOpen);
    });
    toggleBody(_dcOpen);

    const fixBtn = $('dcFixBtn');
    if (fixBtn) fixBtn.addEventListener('click', function () { runDataFix(); });
  }



  function renderDataFix(res) {
    const out = $('dcFix');
    if (!out) return;
    const acts = res.actions || [];
    const st = res.stats || {};
    const auto = acts.filter(function (a) { return a.auto; });
    const manual = acts.filter(function (a) { return !a.auto; });

    const listHtml = function (list, cls) {
      return list.map(function (a) {
        const badge = a.auto
          ? '<span class="dc-badge auto">已自动处理</span>'
          : '<span class="dc-badge manual">需人工确认</span>';
        const num = a.n ? '<span class="dc-tag">' + escapeHtml(String(a.n)) + ' 处</span>' : '';
        const cols = (a.columns || []).length
          ? '<div class="dc-row"><b>相关列：</b>' + escapeHtml(a.columns.join('、')) + '</div>' : '';
        return '<div class="dc-fx ' + cls + '">'
          + '<div class="dc-t3">' + badge + escapeHtml(a.title) + num + '</div>'
          + (a.detail ? '<div class="dc-row">' + escapeHtml(a.detail) + '</div>' : '')
          + cols + '</div>';
      }).join('');
    };

    let body = '';
    if (auto.length) body += '<div class="dc-fh">✅ 已自动修复 ' + auto.length + ' 项</div>' + listHtml(auto, 'auto');
    if (manual.length) body += '<div class="dc-fh">✋ 以下 ' + manual.length + ' 项需人工确认（未自动改）</div>' + listHtml(manual, 'manual');
    if (!acts.length) body += '<div class="dc-row">未发现需要处理的问题，原始数据可放心使用。</div>';

    out.innerHTML = '<div class="dc-fh">🩹 清洗副本已生成'
      + '<span class="dc-tag">' + escapeHtml(String(res.rows)) + ' 行 × ' + escapeHtml(String(res.cols)) + ' 列</span>'
      + '</div>'
      + body
      + '<div class="dc-stat">共改动 ' + escapeHtml(String(st.cells_changed || 0)) + ' 个单元格，删除 '
      + escapeHtml(String(st.rows_removed || 0)) + ' 行；'
      + (res.changes_truncated ? '改动明细超过 300 条已截断；' : '')
      + '原文件未被修改，副本另存为 CSV。</div>'
      + '<div class="dc-fixbar" style="padding:8px 0 0"><button class="dc-btn" id="dcDlBtn">⬇ 下载清洗副本（CSV）</button></div>';

    const dlBtn = $('dcDlBtn');
    if (dlBtn) dlBtn.addEventListener('click', function () { downloadCleanCsv(res); });
  }

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

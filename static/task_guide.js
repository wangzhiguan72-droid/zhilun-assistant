/* Real-operation teaching. No upload, computation, or model call is initiated here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ZhilunGuide = api.mount(root);
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const paths = {
    paper: [
      ['paperLintBtn', '选择论文', '选择论文，解析成功后继续。', 'paper_ready'],
      ['paperBasis', '确认检查依据', '查看通用参考，或确认学校要求。', 'paper_basis'],
      ['paperLintRunBtn', '运行本地检查', '点击开始检查；失败时可重试。', 'paper_result'],
      ['paperLintResult', '展开一条提示', '展开原文、依据与建议；无提示时查看汇总。', 'paper_read'],
      ['paperLintExportBtn', '导出检查清单', '点击导出，生成 Word 文件。', 'paper_exported']
    ],
    data: [
      ['drop', '上传数据', '上传 Excel/CSV；也可主动载入演示。', 'data_ready'],
      ['datacheckBox', '查看数据体检', '展开体检报告，核对提示与未覆盖项。', 'data_read'],
      ['wizStep1', '选择分析变量', '按现有向导选列，再点击开始生成分析。', 'analysis_ready'],
      ['streamMarkdown', '阅读本地结果', '点击结果查看统计量和适用条件。', 'analysis_read'],
      ['exportBtn', '导出分析报告', '点击导出 Word，完成本次体验。', 'data_exported']
    ]
  };
  function nextIndex(path, index, facts) {
    const steps = paths[path] || [];
    while (index < steps.length && facts[steps[index][3]]) index++;
    return index;
  }
  function mount(w) {
    const d = w.document, key = 'zhilun_guide_position_v1';
    let active = false, path = 'paper', index = 0, target = null, previousFocus = null;
    const facts = {}, skipped = [];
    const box = d.createElement('aside');
    box.id = 'taskGuide'; box.className = 'task-guide'; box.hidden = true;
    box.setAttribute('role', 'region'); box.setAttribute('aria-label', '操作引导');
    box.innerHTML = '<div id="guideLive" aria-live="polite"></div><div class="guide-actions">'
      + '<button type="button" data-guide="back">上一步</button>'
      + '<button type="button" data-guide="skip">跳过说明</button>'
      + '<button type="button" data-guide="restart">重新开始</button>'
      + '<button type="button" data-guide="exit">退出引导</button></div>';
    d.body.appendChild(box);
    const spotlight = d.createElement('div'); spotlight.className = 'guide-spotlight'; spotlight.hidden = true;
    d.body.appendChild(spotlight);
    function persist() {
      try { w.sessionStorage.setItem(key, JSON.stringify({ path, index, active })); } catch (_) {}
    }
    function clearTarget() {
      if (target) target.classList.remove('guide-target');
      target = null; spotlight.hidden = true;
    }
    function position() {
      if (!active || !target) return;
      const r = target.getBoundingClientRect();
      spotlight.style.cssText = 'left:' + r.left + 'px;top:' + r.top + 'px;width:' + r.width + 'px;height:' + r.height + 'px';
      spotlight.hidden = r.width <= 0 || r.height <= 0;
      // Pick the opposite corner; the target and file/compute buttons stay clickable.
      box.style.top = ''; box.style.bottom = '';
      if (r.bottom > w.innerHeight - box.offsetHeight - 32) box.style.top = '12px';
      else box.style.bottom = '12px';
    }
    function render(scroll) {
      clearTarget(); box.hidden = !active;
      if (!active) return;
      const steps = paths[path];
      index = nextIndex(path, index, facts);
      const live = d.getElementById('guideLive');
      if (index >= steps.length) {
        live.textContent = skipped.length ? '说明已结束（含跳过项）；是否完成以实际结果为准。' : '本次体验已完成，导出文件已生成。';
        active = false; spotlight.hidden = true; persist(); return;
      }
      const step = steps[index]; target = d.getElementById(step[0]);
      if (target && !target.getClientRects().length) target = null;
      live.textContent = (index + 1) + '/' + steps.length + ' · ' + step[1] + '：' + step[2]
        + (target ? '' : ' 当前控件不可见，可返回对应任务、跳过说明或退出。');
      if (target) {
        target.classList.add('guide-target');
        if (scroll) target.scrollIntoView({ block: 'center', behavior: 'auto' });
        position();
      } else skipped.push({ step: step[0], reason: '控件缺失或不可见' });
      persist();
    }
    function start(p, resume) {
      if (!paths[p]) return;
      previousFocus = d.activeElement; path = p; index = resume ? index : 0; active = true;
      if (!resume) skipped.length = 0;
      w.closeModal('zlOnboard'); w.closeModal('zlHelp');
      if (w.EntryTriage) w.EntryTriage.go(p === 'paper' ? 'lint' : 'data');
      // Reuse facts from this page only. Reload restores instructions, never phantom files/results.
      facts.data_ready = !!(w.state && w.state.fileId);
      facts.paper_result = !!w._lastPaperLintMarkdown;
      facts.analysis_ready = !!w._lastMarkdown;
      render(true); box.querySelector('[data-guide="exit"]').focus();
    }
    function record(event) {
      facts[event] = true;
      if (active) render(true);
    }
    function invalidate(events) { events.forEach(e => delete facts[e]); }
    function stop() {
      active = false; box.hidden = true; clearTarget(); persist();
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    }
    box.addEventListener('click', e => {
      const action = e.target.getAttribute('data-guide');
      if (action === 'exit') stop();
      if (action === 'restart') { Object.keys(facts).filter(x => /_read$|_exported$/.test(x)).forEach(x => delete facts[x]); start(path); }
      if (action === 'back') {
        index = Math.max(0, index - 1); delete facts[paths[path][index][3]]; active = true; render(true);
      }
      if (action === 'skip') { skipped.push({ step: paths[path][index] && paths[path][index][0], reason: '用户跳过说明' }); index++; render(true); }
    });
    d.addEventListener('keydown', e => { if (e.key === 'Escape') stop(); });
    function read(e) {
      if (!active || !target || !target.contains(e.target)) return;
      const event = paths[path][index][3];
      if (/_read$/.test(event)) {
        if (event === 'paper_read' && !(e.type === 'toggle' && e.target.tagName === 'DETAILS' && e.target.open)) return;
        // A loading/failure box is not a result.
        if (event === 'data_read' && !w._lastDatacheckMarkdown) return;
        if (event === 'analysis_read' && !w._lastMarkdown) return;
        if (event === 'paper_read' && !w._lastPaperLintMarkdown) return;
        record(event);
      }
    }
    d.addEventListener('click', read); d.addEventListener('toggle', read, true);
    w.addEventListener('resize', position); w.addEventListener('scroll', position, true);
    try {
      const saved = JSON.parse(w.sessionStorage.getItem(key) || 'null');
      if (saved && saved.active && paths[saved.path]) {
        path = saved.path; index = Math.min(paths[path].length - 1, Math.max(0, Number(saved.index) || 0));
        // Inputs are not persisted, so return to preparation after reload.
        index = 0; start(path, true);
      }
    } catch (_) {}
    return { start, record, invalidate, stop, skipped };
  }
  return { paths, nextIndex, mount };
});

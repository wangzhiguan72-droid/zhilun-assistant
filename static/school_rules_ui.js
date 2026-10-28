(function () {
  'use strict';
  const host = document.getElementById('schoolRulesHost');
  if (!host) return;
  const fields = { east_asia_font:'正文中文字体', latin_font:'正文西文字体', font_size_pt:'正文字号（pt，小四=12）', line_spacing_multiplier:'正文行距（倍数）', first_line_indent_cm:'首行缩进（cm）', margin_top_cm:'上边距（cm，全分节）', margin_bottom_cm:'下边距（cm，全分节）', margin_left_cm:'左边距（cm，全分节）', margin_right_cm:'右边距（cm，全分节）' };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  host.innerHTML = '<details id="schoolRulesPanel"><summary>可选：学校要求与 DOCX 格式</summary>'
    + '<p>学校要求与论文分别选择；本地解析原文不会自动激活规则。首版仅人工确认正文及页边距要求，不含 OCR、自动改稿。规范和规则仅保留于本页内存。</p>'
    + '<label>学校规范附件（可多选 DOCX/PDF/TXT/MD）<input type="file" id="schoolFiles" accept=".docx,.pdf,.txt,.md" multiple></label>'
    + '<div id="schoolSourceStatus" role="status"></div><div id="schoolSources"></div>'
    + '<div class="task-fields"><label>发布单位 / 学校<input id="schoolName" placeholder="手动录入时可填个人参考"></label><label>适用年份<input id="schoolYear" placeholder="例如 2026"></label><label>学位 / 院系 / 范围<input id="schoolDegree" placeholder="请核对与本人适用"></label></div>'
    + '<p>逐条选择字段、期望值和来源；学校或模板依据需粘贴附件中的原句。自定义规则不能标记为学校要求。</p><div id="schoolRuleRows"></div>'
    + '<div class="task-tools"><button type="button" id="schoolRuleAdd">添加规则</button><button type="button" id="schoolConfirm">确认本次适用规则</button><button type="button" id="schoolGeneric">恢复通用参考</button></div>'
    + '<div id="schoolConfirmStatus" role="status"></div></details>';
  const el = id => document.getElementById(id);
  let sources = [], files = [], confirmed = null, previewGeneration = 0;
  function dirty() { confirmed = null; el('schoolConfirmStatus').textContent = '规则已更改，需重新确认。'; window.invalidatePaperBasis(); }
  function sourceOptions() {
    return '<option value="custom">自定义（不算学校要求）</option><option value="default">通用参考</option>'
      + sources.map((s,i) => '<option value="school:'+i+'">学校依据：'+esc(s.name)+'</option><option value="template:'+i+'">模板依据：'+esc(s.name)+'</option>').join('');
  }
  function addRule() {
    const row = document.createElement('div'); row.className = 'task-rules school-rule';
    row.innerHTML = '<div class="task-fields"><label>检查项目<select class="rule-field">'+Object.entries(fields).map(([v,t])=>'<option value="'+v+'">'+t+'</option>').join('')+'</select></label>'
      + '<label>期望值<input class="rule-value" placeholder="例如 宋体 / 12 / 1.5"></label><label>来源<select class="rule-source">'+sourceOptions()+'</select></label></div>'
      + '<label>规范原句（学校/模板必填）<textarea class="rule-quote" rows="2" style="width:100%"></textarea></label>'
      + '<label>适用说明 / 自定义理由<input class="rule-reason" style="width:100%"></label>'
      + '<label><input class="rule-active" type="checkbox" checked>本次启用</label> <button type="button" class="rule-remove">删除此条</button>';
    row.querySelector('.rule-remove').addEventListener('click', () => { row.remove(); dirty(); });
    el('schoolRuleRows').appendChild(row); dirty();
  }
  el('schoolRuleAdd').addEventListener('click', addRule);
  host.addEventListener('input', e => { if (e.target.id !== 'schoolFiles') dirty(); });
  host.addEventListener('change', e => { if (e.target.id !== 'schoolFiles') dirty(); });
  el('schoolFiles').addEventListener('change', async e => {
    const generation = ++previewGeneration;
    files = Array.from(e.target.files || []); sources = []; dirty();
    el('schoolSources').textContent = ''; el('schoolSourceStatus').textContent = '正在本地解析规范…';
    const fd = new FormData(); files.forEach(f => fd.append('requirements', f));
    try {
      const r = await fetch('/api/school_rules/preview', { method:'POST', body:fd }); const j = await r.json();
      if (generation !== previewGeneration) return;
      if (!r.ok || !j.ok) throw new Error(j.error || '解析失败');
      sources = j.sources;
      el('schoolSourceStatus').textContent = '已读取 '+sources.length+' 份，仅供核对；尚未确认生效。';
      el('schoolSources').innerHTML = sources.map(s => '<details><summary>'+esc(s.name)+' · '+esc(s.extraction_status)+'</summary><p>哈希：'+esc(s.sha256)+'</p><pre>'+esc(s.source_text)+'</pre></details>').join('');
    } catch (_) {
      if (generation !== previewGeneration) return;
      files = []; el('schoolSourceStatus').textContent = '未能读取要求；请检查类型、大小、加密状态。可改为手动规则。';
    }
    host.querySelectorAll('.rule-source').forEach(s => { s.innerHTML = sourceOptions(); });
  });
  function profile() {
    const rules = Array.from(host.querySelectorAll('.school-rule')).map(row => {
      const get = s => row.querySelector(s).value.trim(), field = get('.rule-field');
      const parts = get('.rule-source').split(':'), type = parts[0], source = sources[Number(parts[1])];
      const raw = get('.rule-value');
      if (!raw) throw new Error('请填写每条规则的期望值，或删除空白规则。');
      const value = /_font$/.test(field) ? raw : Number(raw);
      if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('数值字段需填写数字，单位见字段名。');
      const quote = get('.rule-quote');
      if (['school','template'].includes(type) && (!source || !quote || !source.source_text.includes(quote))) throw new Error('请从对应规范中粘贴确切原句。');
      return { field, expected:value, source_type:type, source_hash:source && ['school','template'].includes(type) ? source.sha256 : null, quote:['school','template'].includes(type) ? quote : '', reason:get('.rule-reason'), active:row.querySelector('.rule-active').checked, confirmed:true };
    });
    if (!rules.some(x => x.active)) throw new Error('请至少启用一条规则，或选择通用参考。');
    if (!el('schoolName').value.trim() || !el('schoolDegree').value.trim() || !el('schoolYear').value.trim()) throw new Error('请确认发布单位、年份及适用范围；个人规则可注明个人参考。');
    return { confirmed:true, scope:{ school:el('schoolName').value.trim(), year:el('schoolYear').value.trim(), degree:el('schoolDegree').value.trim(), body:true }, rules };
  }
  function useGeneric() { confirmed = null; el('schoolConfirmStatus').textContent = '本次仅运行通用文字规则，未进行学校格式判定。'; }
  el('schoolGeneric').addEventListener('click', () => { useGeneric(); window.confirmPaperBasis('通用参考'); });
  el('schoolConfirm').addEventListener('click', () => {
    try {
      confirmed = { profile:profile(), files:files.slice() };
      el('schoolConfirmStatus').textContent = '已确认适用范围及原文；冲突将单列，不据此判定全篇合规。';
      window.confirmPaperBasis('人工确认格式规则');
    } catch (e) { el('schoolConfirmStatus').textContent = e.message; }
  });
  async function check(paper, snapshot) {
    const result = document.createElement('div'); result.className = 'task-rules'; el('paperLintResult').appendChild(result);
    result.textContent = '正在核对确认的格式规则…';
    try {
      const fd = new FormData(); fd.append('paper', paper); fd.append('profile', JSON.stringify(snapshot.profile)); snapshot.files.forEach(f => fd.append('requirements',f));
      const r = await fetch('/api/format_check', {method:'POST', body:fd}); const j = await r.json();
      if (!r.ok || !j.ok) throw new Error('格式检查未完成，请核对规则和文件后重新运行。');
      const labels = {compliant:'符合',noncompliant:'不符合，请核对',undetermined:'未判定',not_applicable:'不适用',conflict:'规则冲突'};
      result.innerHTML = '<b>确认的格式规则 · 仅限已覆盖范围</b><p>档案 '+esc(j.profile_version)+' · 论文 SHA256 '+esc(j.paper_hash)+'</p>'
        + '<p>'+Object.entries(j.counts || {}).map(([k,v])=>esc(labels[k] || k)+' '+Number(v)).join(' · ')+'</p>'
        + (j.findings || []).map(x => '<details><summary>'+esc(labels[x.status] || x.status)+' · '+esc(x.location)+' · '+esc(fields[x.field] || x.field || '')+'</summary><p>实际：'+esc(JSON.stringify(x.actual))+'；期望：'+esc(JSON.stringify(x.expected))+'</p><p>依据：'+esc(({school:'学校依据',template:'模板依据',custom:'用户自定义',default:'通用参考'})[x.source] || (typeof x.source === 'object' ? JSON.stringify(x.source) : x.source))+' '+esc(x.quote || '')+'</p>'+(x.reason?'<p>用户覆盖理由：'+esc(x.reason)+'；不能据此判定符合学校要求。</p>':'')+'<p>修改：'+esc(x.action)+'</p></details>').join('')
        + '<details><summary>本次规则与来源档案</summary><pre>'+esc(JSON.stringify(j.provenance,null,2))+'</pre></details>'
        + '<details><summary>已检查与未覆盖范围</summary><pre>'+esc(JSON.stringify(j.coverage,null,2))+'</pre></details>';
      window._lastPaperLintMarkdown += '\n\n' + j.markdown;
    } catch (e) {
      result.textContent = e.message;
      window._lastPaperLintMarkdown += '\n\n## 格式检查未完成\n本次未取得格式结果，不代表符合学校要求。请重新核对规则和文件。';
    }
  }
  window.SchoolRules = { useGeneric, snapshot:() => confirmed ? {profile:JSON.parse(JSON.stringify(confirmed.profile)),files:confirmed.files.slice()} : null, check };
})();

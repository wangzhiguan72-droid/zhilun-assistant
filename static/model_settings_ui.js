(function () {
  'use strict';
  const host = document.getElementById('modelSettingsHost'); if (!host) return;
  const key = 'zhilun_model_policy_v1', el = id => document.getElementById(id);
  const tasks = {write_text:'结果解释 / 副驾驶润色',paper_check:'论文深度审计',audit_chat:'比对追问',audit_image:'图表理解',recommend:'数据规则建议'};
  const providers = {custom_primary:'自填兼容服务',zhipu:'智谱',sf:'硅基流动',deepseek:'DeepSeek',dashscope:'百炼',kimi:'Kimi',mimo:'MiMo',maas:'服务端 MaaS'};
  for (let i=1;i<=8;i++) providers['custom'+i] = '更多自定义配置 '+i;
  const providerOptions = Object.entries(providers).map(([v,t])=>'<option value="'+v+'">'+t+'</option>').join('');
  host.innerHTML = '<div class="task-rules"><label>基础模式 <select id="modelMode"><option value="off">无需 AI（本地计算）</option><option value="unified">统一使用一个自选模型</option><option value="advanced">高级：按任务选择</option></select></label>'
    + '<div class="task-fields"><label>服务配置<select id="modelProvider">'+providerOptions+'</select></label><label>模型 ID<input id="modelID" placeholder="填写服务支持的准确 ID"></label>'
    + '<label>兼容服务 Base URL<input id="modelURL" placeholder="https://.../v1 或本地服务"></label><label>兼容服务 Key<input type="password" id="modelKey" autocomplete="off" placeholder="仅用于自填兼容服务"></label></div>'
    + '<p>平台配置使用下方已存 Key 或当前实例环境配置。自填兼容服务暂未验证视觉能力；图表任务可单独关闭或选已支持的配置。</p>'
    + '<details id="modelAdvanced"><summary>按任务覆盖与回退授权</summary>'
    + Object.entries(tasks).map(([id,t])=>'<div class="task-fields"><label>'+t+'<select id="taskMode_'+id+'"><option value="inherit">继承统一配置</option><option value="off">关闭此任务 AI</option><option value="select">指定配置</option></select></label><label>服务<select id="taskProvider_'+id+'">'+providerOptions+'</select></label><label>模型 ID<input id="taskID_'+id+'"></label></div>').join('')
    + '<label><input type="checkbox" id="modelFallback">失败时允许按以下候选顺序切换</label><p>每行 provider/model-id，例如 custom1/my-model；只会使用列出的候选。</p>'
    + '<textarea id="modelCandidates" rows="3" style="width:100%" placeholder="provider/model-id"></textarea>'
    + '<label><input type="checkbox" id="modelExternal">允许从本地服务切到候选外部服务</label><br><label><input type="checkbox" id="modelPaid">允许切到候选中可能计费的服务</label>'
    + '<p>首版不提供按金额精确预算；费用以服务账单为准。未授权的切换停止 AI 步骤，保留本地结果。</p></details>'
    + '<div id="modelStatus" class="model-status" role="status">未配置 / 未测试</div>'
    + '<label><input type="checkbox" id="modelTestConsent">我知悉测试会向所选服务发送简短测试请求，可能计费</label>'
    + '<div class="task-tools"><label>测试任务<select id="modelTestTask">'+Object.entries(tasks).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></label><button type="button" id="modelTest">主动测试连接</button><button type="button" id="modelStatusRefresh">查看实例配置来源</button></div><div id="modelTestResult" class="model-status" role="status"></div><div id="modelActualResult" class="model-status" role="status">最近任务：本页尚无实际模型调用成功记录。</div></div>';
  const values = ['modelMode','modelProvider','modelID','modelURL','modelKey','modelCandidates'];
  Object.keys(tasks).forEach(t => values.push('taskMode_'+t,'taskProvider_'+t,'taskID_'+t));
  const flags = ['modelFallback','modelExternal','modelPaid'];
  function policy() {
    if(el('modelMode').value==='off')return {schema_version:1,mode:'off'};
    return { schema_version:1, mode:el('modelMode').value, selected:{provider:el('modelProvider').value,model:el('modelID').value.trim()},
      tasks:el('modelMode').value==='advanced'?Object.fromEntries(Object.keys(tasks).map(t => [t,{mode:el('taskMode_'+t).value,provider:el('taskProvider_'+t).value,model:el('taskID_'+t).value.trim()}])):{},
      allow_fallback:el('modelFallback').checked,allow_external_fallback:el('modelExternal').checked,allow_paid_fallback:el('modelPaid').checked,
      candidates:el('modelFallback').checked?el('modelCandidates').value.split('\n').map(x=>x.trim()).filter(Boolean).map(x=>{ const i=x.indexOf('/'); return {provider:i<0?'':x.slice(0,i),model:i<0?x:x.slice(i+1)}; }):[] };
  }
  function collect(out) {
    out.ai_policy = policy();
    const custom = (out.custom_providers || []).map((x,i)=>Object.assign({},x,{id:'custom'+(i+1)}));
    if (el('modelURL').value.trim() && el('modelKey').value.trim() && el('modelID').value.trim()) custom.unshift({id:'custom_primary',base_url:el('modelURL').value.trim(),key:el('modelKey').value.trim(),models:el('modelID').value.trim()});
    if (custom.length) out.custom_providers = custom;
    return out;
  }
  function save() {
    const saved = {}; values.forEach(id=>saved[id]=el(id).value); flags.forEach(id=>saved[id]=el(id).checked);
    try { localStorage.setItem(key,JSON.stringify(saved)); updateChoice(); return true; }
    catch (_) { el('modelStatus').textContent = '浏览器存储不可用；输入仅在本页有效，刷新后需重填。'; return false; }
  }
  function clear() {
    el('modelMode').value='off'; el('modelURL').value='';el('modelKey').value='';el('modelID').value='';
    flags.forEach(id=>el(id).checked=false); el('modelTestResult').textContent='';
    try {localStorage.removeItem(key);} catch (_) {}
    el('modelStatus').textContent='本页 AI 已关闭；服务端环境配置未被修改。';
  }
  function credentials(byok) {
    const map={siliconflow_key:'sf',zhipu_key:'zhipu',deepseek_key:'deepseek',dashscope_key:'dashscope',kimi_key:'kimi',mimo_key:'mimo'};
    return Object.fromEntries(Object.entries(map).filter(([k])=>byok[k]).map(([k,v])=>[v,byok[k]]));
  }
  async function refresh() {
    try {
      const r=await fetch('/api/model/status'); const j=await r.json(); if(!r.ok||!j.ok)throw new Error();
      const configured=Object.entries(j.server.providers).filter(([,v])=>v.configured).map(([k])=>k);
      el('modelStatus').textContent='实例来源：当前运行进程环境；同目录配置文件：'+(j.server.config_files_present.join('、')||'未发现')+'。\n已检测到服务端配置字段：'+(configured.join('、')||'无')+'（仅填写状态，未验证连通）。\n浏览器选择：'+(el('modelMode').value==='off'?'AI 已关闭':el('modelProvider').value+'/'+el('modelID').value+' · 已填写未测试')+'。历史测试成功不保证当前可用。';
    } catch (_) {el('modelStatus').textContent='未能读取当前实例配置状态；不代表模型可用。';}
  }
  let generation=0;
  host.addEventListener('input',()=>{generation++;el('modelTestResult').textContent='配置已更改，尚未测试。';});
  host.addEventListener('change',()=>{generation++;el('modelTestResult').textContent='配置已更改，尚未测试。';});
  el('modelStatusRefresh').addEventListener('click',refresh);
  el('modelTest').addEventListener('click',async()=>{
    if(!el('modelTestConsent').checked){el('modelTestResult').textContent='请先阅读并勾选联网及费用说明。';return;}
    const byok=window.collectByok(), chosen=JSON.stringify(byok), ticket=generation;
    el('modelTest').disabled=true;el('modelTestResult').textContent='测试中…';
    try {
      const r=await fetch('/api/model/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_network:true,task:el('modelTestTask').value,ai_policy:byok.ai_policy,credentials:credentials(byok),custom_providers:byok.custom_providers||[]})});
      const j=await r.json();
      if(ticket!==generation||chosen!==JSON.stringify(window.collectByok())){el('modelTestResult').textContent='测试期间配置已更改；当前配置尚未测试。';return;}
      el('modelTestResult').textContent=(j.ok?'连通测试成功':'连通测试失败')+' · '+(j.tested_at||'')+'\n请求：'+(j.selected_model||'')+'；实际：'+(j.actual_model||'未成功')+(j.error?'\n'+j.error:'')+(j.ok?'\n仅证明短请求连通；长文审阅仍可能超时、耗尽额度或超出模型上下文。AI 失败时本地报告仍可用。':'');
    } catch(_){el('modelTestResult').textContent='连通测试未完成，请检查本机服务。';}
    finally{el('modelTest').disabled=false;}
  });
  function record(meta) {
    if(!meta)return;
    el('modelActualResult').textContent='最近任务：'+(tasks[meta.task]||meta.task||'AI')+' · '+({success:'实际调用成功',disabled:'已关闭，未调用模型',failed:'调用失败',pending:'等待调用'}[meta.status]||'未完成')+'\n选择：'+(meta.selected_model||'无')+'；实际：'+(meta.actual_model||'未成功')+'\n尝试：'+(meta.attempts||[]).join(' → ');
  }
  function restore() {
    try {
      const saved=JSON.parse(localStorage.getItem(key)||'{}');
      values.forEach(id=>{el(id).value=typeof saved[id]==='string'?saved[id]:(id==='modelMode'?'off':id==='modelProvider'?'custom_primary':id.startsWith('taskMode_')?'inherit':id.startsWith('taskProvider_')?'custom_primary':'');});
      flags.forEach(id=>el(id).checked=saved[id]===true);
      updateChoice();
    } catch(_) { clear(); }
  }
  function updateChoice() {
    el('modelChoiceStatus').textContent=el('modelMode').value==='off'?'已选择仅本地检查；可随时启用 AI。':'已选择自选 AI 配置；各功能仍需主动开启，填写不等于调用成功。';
  }
  function chooseAI() { window.openSettings(); if(el('modelMode').value==='off')el('modelMode').value='unified'; el('modelStatus').textContent='填写或选择模型后保存。模型 ID 需与你的服务一致；本次未发起调用。'; }
  function localOnly() {el('modelMode').value='off';save();}
  restore();
  window.ModelSettings={collect,save,clear,refresh,record,restore,chooseAI,localOnly};
})();

(function () {
  'use strict';
  const rows = [
    ['第一次从哪里开始','点击开始体验，跟随高亮完成一次真实操作。','guide'],
    ['没有 API Key 能用吗','数据计算与规则检查可直接使用；自选 AI 可辅助复核语义、解释结果和追问。','model'],
    ['只有论文没有数据','使用论文规范体检；数据比对需要原始数据。','paper'],
    ['学校要求放在哪里','在论文检查依据中添加学校要求，再确认生效规则。','school'],
    ['论文和要求传反了','两个文件区分别标明用途，可重新选择后检查。','school'],
    ['格式要求会自动生效吗','不会，先核对适用范围、原文和数值，再确认。','school'],
    ['与学校要求不一致怎么办','展开来源；冲突项暂停比较，核对原文再确认。','school'],
    ['为什么字体没有结论','继承、主题或特殊区域无法可靠解析时为未判定。','school'],
    ['PDF 和 DOCX 哪个更完整','字体字号结构检查使用 DOCX；PDF 只做文字规则。','paper'],
    ['未判定等于通过吗','不等于；该项证据不足或本次没有检查。','paper'],
    ['有提示就一定要改吗','先看原文、适用条件和依据，再决定如何修改。','paper'],
    ['能直接改论文吗','当前提供清单与建议，原文件保持不变。','paper'],
    ['现在有没有使用模型','查看设置中的本页最近实际调用记录；填 Key 不等于调用成功。','model'],
    ['如何给某一步换模型','高级模式按真实 AI 任务选择、继承或关闭。','model'],
    ['模型失败会偷偷换吗','新配置默认禁止回退；启用后只使用你允许的候选。','model'],
    ['重新引导会丢文件吗','不会；引导只改变说明位置，不发起计算和模型调用。','guide'],
    ['内容会传给谁','本地规则不发送到模型；可选 AI 按功能发送摘要、摘录或图片到所选服务。','model'],
    ['应下载哪一个版本','参赛基线看原材料包；本轮开发版本及校验结果另行标记。','version']
  ];
  const faq = document.querySelector('#zlHelp .zl-faq');
  const intro = document.querySelector('#zlHelp .zl-sub');
  if (!faq) return;
  intro.textContent='按当前任务先显示相关短答，其余可搜索查看。';
  const search = document.createElement('input'); search.placeholder='搜索帮助'; search.setAttribute('aria-label','搜索帮助'); search.style.cssText='width:100%;padding:8px;margin-bottom:10px'; faq.before(search);
  let context = 'paper';
  const nodes = rows.map(([q,a,target])=>{
    const detail=document.createElement('details'),summary=document.createElement('summary'),p=document.createElement('p'),button=document.createElement('button');
    summary.textContent=q;p.textContent=a;button.className='zl-btn ghost';button.type='button';button.textContent='前往相关位置';
    button.addEventListener('click',()=>{
      window.closeModal('zlHelp');
      if(target==='guide')ZhilunGuide.start(context==='data'?'data':'paper');
      else if(target==='model')window.openSettings();
      else if(target==='school'){EntryTriage.go('lint');document.getElementById('schoolRulesPanel').open=true;EntryTriage._flash('paperBasis');}
      else if(target==='version') { EntryTriage._clickTab('analyze'); EntryTriage._flash('landing','版本以页头标识与本次构建校验记录为准。'); }
      else EntryTriage.go('lint');
    });
    detail.append(summary,p,button);return detail;
  });
  faq.replaceChildren(...nodes);
  function filter() {
    const q=search.value.trim();
    nodes.forEach((n,i)=>{ n.hidden=q?!rows[i].slice(0,2).join('').toLowerCase().includes(q.toLowerCase()):!(context==='data'?[0,1,12,15]:[2,3,8,9]).includes(i); });
  }
  search.addEventListener('input',filter);
  const more=document.createElement('button');more.type='button';more.className='zl-btn ghost';more.textContent='查看全部 18 条';more.addEventListener('click',()=>nodes.forEach(n=>n.hidden=false));faq.after(more);
  function open(c){context=c||'paper';search.value='';filter();document.getElementById('zlHelp').classList.add('on');}
  window.TaskHelp={open}; window.openHelp=()=>open(document.getElementById('tab-check').style.display==='none'?'data':'paper');
  const tools=document.createElement('div');tools.className='task-tools';tools.innerHTML='<button type="button" id="dataGuideStart">数据操作引导</button><button type="button" id="dataHelpOpen">为什么与怎么办</button>';
  document.getElementById('fileInfo').after(tools);
  document.getElementById('dataGuideStart').addEventListener('click',()=>ZhilunGuide.start('data'));
  document.getElementById('dataHelpOpen').addEventListener('click',()=>open('data'));
  // Opening an introduction is not completing a tutorial. Remember only a dismissal preference.
  document.getElementById('zlOnboard').addEventListener('click', e=>{
    if (e.target.closest('button')) {try{localStorage.setItem('zhilun_onboarded','1');}catch(_){} }
  });
  filter();
})();

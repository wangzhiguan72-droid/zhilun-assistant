/* Execute the real submit handler: expanded send scope requires active AI opt-in. */
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
const signature="$('checkBtn').addEventListener('click', async () => {";
const start=html.indexOf(signature);
assert(start>=0,'paper check submit handler exists');
// The next function begins at the same indentation; all nested blocks are deeper.
const end=html.indexOf('\n  });',start)+7;
assert(end>start);
const source=html.slice(start,end);
const els={};
function el(id){return els[id]||(els[id]={value:'',checked:false,style:{},disabled:false,textContent:'',innerHTML:'',addEventListener(_event,fn){this.callback=fn;}});}
el('auditContextMode').value='evidence';el('auditOutputTokens').value='8192';
el('auditResearchTools').checked=true;
let posted;
class Form {constructor(){this.fields={};}append(k,v){this.fields[k]=v;}}
const context={console,FormData:Form,document:{getElementById:el},$:el,sleep:async()=>{},
  checkState:{paperFile:{},dataFile:{}},collectByok:()=>({ai_policy:{schema_version:1,mode:'off'}}),
  toast(){},escapeHtml:x=>x,renderCheckResult(){},window:{},
  fetch:async(_url,opts)=>{posted=opts.body.fields;return {ok:true,json:async()=>({ok:true,audit:{}})};}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
  await el('checkBtn').callback();assert(!('use_llm' in posted));assert(!('ai_context_mode' in posted));assert(!('ai_research_tools' in posted));
  el('useLLMCheck').checked=true;
  await el('checkBtn').callback();assert.equal(posted.use_llm,'1');
  assert.equal(posted.ai_context_mode,'evidence');assert.equal(posted.ai_output_tokens,'8192');
  assert.equal(posted.ai_research_tools,'1');
  el('auditResearchTools').checked=false;
  await el('checkBtn').callback();assert.equal(posted.ai_research_tools,'0');
  assert(html.includes('value="head"')&&html.includes('value="full"'));
  console.log('[PASS] audit_context: AI off sends no scope / explicit scope and budget forwarded');
})().catch(e=>{console.error(e);process.exit(1);});

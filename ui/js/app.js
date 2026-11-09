// ===== basics =====
const $=s=>document.querySelector(s), thread=$('#thread');
const NAMES={cursor:'Cursor',claude:'Claude',codex:'Codex',chatgpt:'ChatGPT',ollama:'Ollama'},BACKENDS=Object.keys(NAMES);
const INITIAL={cursor:'C',claude:'A',codex:'X',chatgpt:'G',ollama:'O'};
const SVC={cursor:'cursor',claude:'claude',codex:'codex',chatgpt:'chatgpt',ollama:'ollama'};
const NOFOLDER=b=>b==='chatgpt'||b==='ollama'; // plain chat: no files, no Agent mode
const MODES={ask:['Ask','Answers questions. Can read files, changes nothing.'],
  plan:['Plan','Writes a step-by-step plan as a .md file first.'],
  agent:['Agent','Edits files and runs commands in the folder.']};
const fresh=()=>({cursor:null,claude:null,codex:null,chatgpt:null,ollama:null});
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
const color=b=>`var(--${b})`;
const ICON={copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
  brain:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5a3 3 0 0 0-5.8-1A3 3 0 0 0 4 9a3 3 0 0 0 0 5 3 3 0 0 0 3 4 3 3 0 0 0 5 1z"/><path d="M12 5a3 3 0 0 1 5.8-1A3 3 0 0 1 20 9a3 3 0 0 1 0 5 3 3 0 0 1-3 4 3 3 0 0 1-5 1z"/></svg>',
  chev:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>',
  check:'<svg class="check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12l5 5 9-10"/></svg>',
  plan:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/></svg>',
  trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  up:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop:'<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="1.5"/></svg>'};
const inflight=new Map();
function newTurnId(){return [...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');}
function stopTurns(){for(const [id,ac] of inflight){post('/api/cancel',{turn_id:id});ac.abort();}}

const S={backend:store.get('backend')||'claude',mode:store.get('mode')||'ask',busy:false,none:false,
  workdir:store.get('workdir')||'',home:''};
let CHAT=null; // current conversation
function newChatState(){return {id:null,title:'',created:Date.now(),backend:S.backend,items:[],sessions:fresh(),ctx:fresh(),sent:fresh()};}

// ===== markdown =====
function inline(t){
  let s=esc(t);
  const codes=[];s=s.replace(/`([^`\n]+)`/g,(_,c)=>{codes.push(c);return '\u0000'+(codes.length-1)+'\u0000';});
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a data-href="$2">$1</a>')
     .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,'$1<a data-href="$2">$2</a>')
     .replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_\n]+)__/g,'<strong>$1</strong>')
     .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g,'$1<em>$2</em>').replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g,'$1<em>$2</em>')
     .replace(/~~([^~\n]+)~~/g,'<del>$1</del>');
  return s.replace(/\u0000(\d+)\u0000/g,(_,i)=>'<code>'+codes[i]+'</code>');}
function md(src){
  const out=[];const parts=String(src).replace(/\r/g,'').split(/^```/m);
  parts.forEach((part,i)=>{
    if(i%2){const nl=part.indexOf('\n');const lang=nl<0?'':part.slice(0,nl).trim();const code=nl<0?part:part.slice(nl+1).replace(/\n$/,'');
      out.push('<div class="codeblock"><div class="codehead"><span>'+esc(lang||'code')+'</span><button type="button" data-copycode>'+ICON.copy+'Copy</button></div><pre><code>'+esc(code)+'</code></pre></div>');return;}
    out.push(blocks(part));});
  return out.join('');}
function blocks(text){
  const L=text.split('\n');let h='',i=0;
  const isTable=k=>/^\s*\|.*\|\s*$/.test(L[k]||'')&&/^\s*\|?\s*:?-{2,}/.test(L[k+1]||'');
  while(i<L.length){
    const l=L[i];
    if(!l.trim()){i++;continue;}
    let m;
    if((m=l.match(/^(#{1,6})\s+(.*)$/))){const n=Math.min(m[1].length,4);h+=`<h${n}>${inline(m[2])}</h${n}>`;i++;continue;}
    if(/^\s*([-*_])\s*(\1\s*){2,}$/.test(l)){h+='<hr>';i++;continue;}
    if(isTable(i)){const row=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      h+='<table><thead><tr>'+row(L[i]).map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>';i+=2;
      while(i<L.length&&/^\s*\|/.test(L[i])){h+='<tr>'+row(L[i]).map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>';i++;}
      h+='</tbody></table>';continue;}
    if(/^\s*>/.test(l)){const q=[];while(i<L.length&&/^\s*>/.test(L[i])){q.push(L[i].replace(/^\s*>\s?/,''));i++;}h+='<blockquote>'+blocks(q.join('\n'))+'</blockquote>';continue;}
    if(/^\s*([-*+]|\d+[.)])\s+/.test(l)){h+=list();continue;}
    const p=[];while(i<L.length&&L[i].trim()&&!/^(#{1,6}\s|\s*>|\s*([-*+]|\d+[.)])\s+|```)/.test(L[i])&&!isTable(i)){p.push(inline(L[i]));i++;}
    h+='<p>'+p.join('<br>')+'</p>';}
  return h;
  function list(){ // nested lists by indentation
    const base=L[i].match(/^\s*/)[0].length;const ordered=/^\s*\d+[.)]\s/.test(L[i]);let s=ordered?'<ol>':'<ul>';
    while(i<L.length){const l=L[i];if(!l.trim()){if(/^\s*([-*+]|\d+[.)])\s+/.test(L[i+1]||'')){i++;continue;}break;}
      const ind=l.match(/^\s*/)[0].length;const m=l.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if(ind<base||(!m&&ind<=base))break;
      if(m&&ind>base){s=s.replace(/<\/li>$/,'')+list()+'</li>';continue;}
      if(!m){s=s.replace(/<\/li>$/,'')+'<br>'+inline(l.trim())+'</li>';i++;continue;}
      const t=m[2].match(/^\[([ xX])\]\s+(.*)$/);
      s+=t?'<li class="task"><input type="checkbox" disabled'+(t[1]!==' '?' checked':'')+'>'+inline(t[2])+'</li>':'<li>'+inline(m[2])+'</li>';i++;}
    return s+(ordered?'</ol>':'</ul>');}}

// ===== rendering messages =====
function scrollDown(){const sc=$('#scroll');sc.scrollTop=sc.scrollHeight;}
function setEmpty(){const e=!CHAT.items.length;$('#main').classList.toggle('is-empty',e);$('#suggest').hidden=!e;
  $('#title').textContent=e?'':(CHAT.title||'New chat');}
function actionsHTML(withRemember=true){return '<div class="actions"><button type="button" data-copy>'+ICON.copy+'Copy</button>'
  +(withRemember?'<button type="button" data-remember>'+ICON.brain+'Remember</button>':'')+'</div>';}
function render(item, parent){ // one stored item -> DOM. parent defaults to the thread.
  const d=document.createElement('div');d.item=item;
  if(item.r==='you'){d.className='m-you';d.innerHTML='<div class="bubble">'+esc(item.t)+'</div>'+actionsHTML();}
  else if(item.r==='bot'||item.r==='err'){d.className='m-bot';const b=item.b;
    d.innerHTML='<div class="avatar" style="background:'+color(b)+'">'+INITIAL[b]+'</div><div class="body"><div class="name">'+NAMES[b]
      +(item.mode?'<span class="meta">'+MODES[item.mode][0]+(item.model?' · '+esc(item.model):'')+(item.u?usageMeta(item.u):'')+'</span>':'')+'</div>'
      +(item.r==='err'?'<div class="err">'+esc(item.t)+'</div>'+(/Open Accounts/.test(item.t)?'<div class="row" style="margin-top:8px"><button class="ghost" type="button" data-openacc>Open Accounts</button></div>':'')
        :'<div class="md">'+md(item.t)+'</div>'+actionsHTML())+'</div>';}
  else if(item.r==='plan'){d.className='card';
    d.innerHTML='<div class="ic">'+ICON.plan+'</div><div class="tx"><b>Plan saved</b><span title="'+esc(item.path)+'">'+esc(item.path)+'</span></div>'
      +'<button class="ghost" type="button" data-planopen>Open</button>'+(NOFOLDER(item.b)?'':'<button class="btn" type="button" data-plango>Implement plan</button>');}
  else{d.className='note';d.innerHTML=esc(item.t)+(item.memId?' <button type="button" data-undo>Undo</button>':'');}
  (parent||thread).appendChild(d);return d;}
function renderThread(){
  thread.innerHTML='';
  const items=CHAT.items;let i=0;
  while(i<items.length){
    const it=items[i];
    if(it.group&&(it.r==='bot'||it.r==='err')){
      const gid=it.group,g=[];
      while(i<items.length&&items[i].group===gid&&(items[i].r==='bot'||items[i].r==='err'))g.push(items[i++]);
      const wrap=document.createElement('div');wrap.className='compare';
      g.forEach(item=>render(item,wrap));thread.appendChild(wrap);
    }else render(items[i++]);
  }
  $('#main').classList.toggle('comparing',!!thread.querySelector('.compare'));
  setEmpty();
}
function addItem(item,save=true){CHAT.items.push(item);const d=render(item);setEmpty();scrollDown();if(save)saveChat();return d;}
function note(t){return addItem({r:'note',t,temp:true},false);} // temporary, not saved
thread.addEventListener('click',async e=>{
  const t=e.target.closest('button,a');if(!t)return;const row=t.closest('.m-you,.m-bot,.note,.card');const item=row&&row.item;
  if(t.dataset.href!==undefined&&t.tagName==='A'){e.preventDefault();post('/api/open',{url:t.dataset.href});return;}
  if(t.hasAttribute('data-copy')){copy(item.t,t);return;}
  if(t.hasAttribute('data-copycode')){copy(t.closest('.codeblock').querySelector('code').textContent,t);return;}
  if(t.hasAttribute('data-remember')){const r=await post('/api/memory',{action:'add',text:item.t.slice(0,500)});
    t.innerHTML=ICON.brain+(r.error?esc(r.error):'Saved to memory');t.disabled=true;refreshMemCount();return;}
  if(t.hasAttribute('data-undo')){await post('/api/memory',{action:'delete',id:item.memId});row.textContent='Memory removed';delete item.memId;item.t='Memory removed';saveChat();refreshMemCount();return;}
  if(t.hasAttribute('data-openacc')){openAccounts();return;}
  if(t.hasAttribute('data-planopen')){post('/api/openfile',{path:item.path});return;}
  if(t.hasAttribute('data-plango')){if(!ready(item.b)){note(NAMES[item.b]+' is not connected.');return;}
    setBackend(item.b);setMode('agent');
    send('Implement the plan in "'+item.path+'". Work through the checklist step by step, and tick off each item in that file ("- [x]") as you finish it.');}});
async function copy(text,btn){try{await navigator.clipboard.writeText(text);}catch(e){const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}
  const old=btn.innerHTML;btn.innerHTML=ICON.copy+'Copied';setTimeout(()=>btn.innerHTML=old,1400);}

// ===== chat history =====
let saveTimer=null;
function saveChat(){if(!CHAT.items.some(i=>i.r==='you'))return;
  if(!CHAT.id){CHAT.id=[...crypto.getRandomValues(new Uint8Array(6))].map(x=>x.toString(16).padStart(2,'0')).join('');}
  if(!CHAT.title){const f=CHAT.items.find(i=>i.r==='you');CHAT.title=f.t.replace(/\s+/g,' ').slice(0,60);}
  CHAT.backend=S.backend;clearTimeout(saveTimer);
  saveTimer=setTimeout(async()=>{const items=CHAT.items.filter(i=>!i.temp);
    renderChatList(await post('/api/chats',{action:'save',chat:{...CHAT,items}}));},250);
  setEmpty();}
function renderChatList(list){
  if(!Array.isArray(list))return;
  const now=new Date(),day=86400000,start=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
  const groups=[['Today',c=>c.updated*1000>=start],['Yesterday',c=>c.updated*1000>=start-day],['Previous 7 days',c=>c.updated*1000>=start-7*day],['Previous 30 days',c=>c.updated*1000>=start-30*day],['Older',()=>true]];
  const used=new Set();let h='';
  for(const [name,test] of groups){const g=list.filter(c=>!used.has(c.id)&&test(c));g.forEach(c=>used.add(c.id));
    if(g.length)h+='<div class="group">'+name+'</div>'+g.map(c=>'<div class="chatitem'+(CHAT&&c.id===CHAT.id?' cur':'')+'" data-id="'+c.id+'"><span class="bdot" style="background:'+color(c.backend||'claude')+'"></span><span class="t">'+esc(c.title||'Untitled')+'</span><button class="icon del" type="button" title="Delete chat">'+ICON.trash+'</button></div>').join('');}
  $('#chatlist').innerHTML=h||'<div class="side-empty">Your chats will show up here.</div>';}
$('#chatlist').addEventListener('click',async e=>{const it=e.target.closest('.chatitem');if(!it)return;const id=it.dataset.id;
  if(e.target.closest('.del')){if(!confirm('Delete this chat?'))return;renderChatList(await post('/api/chats',{action:'delete',id}));if(CHAT.id===id)startNew();return;}
  if(S.busy){note('Stop the current reply before opening another chat.');return;}
  const c=await fetch('/api/chats/'+id).then(r=>r.json());if(c.error)return;
  CHAT={...newChatState(),...c,sessions:{...fresh(),...c.sessions},ctx:{...fresh(),...c.ctx},sent:{...fresh(),...c.sent}};
  renderThread();
  if(c.workdir){S.workdir=c.workdir;store.set('workdir',c.workdir);}
  if(c.backend&&ready(c.backend))setBackend(c.backend,true);paint();
  document.querySelectorAll('.chatitem').forEach(x=>x.classList.toggle('cur',x.dataset.id===id));
  if(innerWidth<760)$('#app').classList.add('noside');scrollDown();$('#msg').focus();});
function startNew(){if(S.busy)return;CHAT=newChatState();thread.innerHTML='';$('#main').classList.remove('comparing');setEmpty();
  document.querySelectorAll('.chatitem').forEach(x=>x.classList.remove('cur'));$('#msg').focus();}
$('#newchat').onclick=startNew;
$('#sideclose').onclick=()=>{$('#app').classList.add('noside');store.set('noside','1');};
$('#sideopen').onclick=()=>{$('#app').classList.remove('noside');store.set('noside','');};
if(store.get('noside')==='1'||innerWidth<760)$('#app').classList.add('noside');

// ===== pickers (assistant / mode / model) =====
function pick(id,btnHTML,itemsHTML){const p=$(id);p.querySelector('.pickbtn').innerHTML=btnHTML;p.querySelector('.menu').innerHTML=itemsHTML;}
document.addEventListener('click',e=>{const pb=e.target.closest('.pickbtn');const inside=e.target.closest('.pick');
  const p=pb&&pb.parentElement.classList.contains('pick')?pb.parentElement:null;
  document.querySelectorAll('.pick.open').forEach(x=>{if(x!==p&&x!==inside)x.classList.remove('open');});if(p)p.classList.toggle('open');});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.pick.open').forEach(x=>x.classList.remove('open'));});
function chosen(){
  let list=null;try{list=JSON.parse(store.get('backends')||'null');}catch(e){list=null;}
  if(!Array.isArray(list)||!list.length) list=[S.backend];
  list=list.filter((b,i)=>BACKENDS.includes(b)&&list.indexOf(b)===i);
  return list.length?list:[S.backend];
}
function activeChoice(){
  if(!ACCLOADED) return chosen();
  const list=chosen().filter(ready);
  if(list.length) return list;
  const one=BACKENDS.find(ready);
  return one?[one]:[S.backend];
}
function paint(){
  const picked=activeChoice();
  const b=picked[0]||S.backend;
  if(b!==S.backend){S.backend=b;store.set('backend',b);}
  const blabel=picked.length>1?NAMES[b]+' +'+(picked.length-1):NAMES[b];
  pick('#p-backend','<span class="bdot" style="background:'+color(b)+'"></span><span class="lbl">'+blabel+'</span>'+ICON.chev,
    '<div class="mh">Assistant'+(picked.length>1?' · select more than one':'')+'</div>'+BACKENDS.map(x=>{const ok=ready(x);const a=ACC[SVC[x]];
      const on=picked.includes(x);
      const sub=!ok?(x==='ollama'?(a&&a.installed?'No models yet':'Not running')+' — see Accounts':(a&&!a.installed?'Not installed':'Not connected')+' — set up in Accounts')
        :on&&picked.length>1?'Asked together':x==='ollama'?'Local models, plain chat':x==='chatgpt'?'Your ChatGPT plan · plain chat':x==='codex'?'Coding agent by OpenAI':x==='claude'?'Claude Code by Anthropic':'Cursor agent';
      return '<button type="button" class="mi'+(on?' sel':'')+'" data-b="'+x+'"'+(ok?'':' disabled')+'><span class="bdot" style="background:'+color(x)+'"></span><span class="mt"><b>'+NAMES[x]+'</b><span>'+sub+'</span></span>'+ICON.check+'</button>';}).join('')
      +'<button type="button" class="mi" data-openacc><span class="mt"><span>Manage accounts…</span></span></button>');
  const chat=picked.every(NOFOLDER);if(chat&&S.mode==='agent')S.mode='ask';
  const limited=picked.filter(NOFOLDER);
  pick('#p-mode','<span class="lbl">'+MODES[S.mode][0]+'</span>'+ICON.chev,
    '<div class="mh">Mode</div>'+Object.entries(MODES).map(([k,[n,d]])=>{
      const agentSub=limited.length?'Ask for '+limited.map(x=>NAMES[x]).join(' and ')+' — Agent is not available there':d;
      return '<button type="button" class="mi'+(k===S.mode?' sel':'')+'" data-m="'+k+'"'+(chat&&k==='agent'?' disabled':'')+'><span class="mt"><b>'+n+'</b><span>'+(chat&&k==='agent'?'Not available for '+picked.map(x=>NAMES[x]).join(', '):(k==='agent'?agentSub:d))+'</span></span>'+ICON.check+'</button>';}).join(''));
  $('#folderbtn').hidden=chat;
  $('#folderlbl').textContent=(S.workdir||S.home).replace(/\/+$/,'').split('/').pop()||'/';
  $('#folderbtn').title='Working folder: '+(S.workdir||S.home);
  paintModels();
  const any=BACKENDS.some(ready);S.none=!any&&ACCLOADED;
  $('#msg').disabled=S.none;$('#send').disabled=S.none;
  $('#send').classList.toggle('stop',!!S.busy);
  $('#send').title=S.busy?'Stop':'Send (Enter)';
  $('#send').innerHTML=S.busy?ICON.stop:ICON.up;
  $('#msg').placeholder=S.none?'No assistant is connected yet — open Accounts to sign in.':S.mode==='plan'?'Describe what to build — you’ll get a plan to review first…':S.mode==='agent'?'What should '+NAMES[b]+' build or change?':'How can I help you today?';
  const who=picked.length>1?picked.map(x=>NAMES[x]).join(', '):NAMES[b];
  $('#hellosub').textContent=S.none?'Connect an assistant in Accounts to get started.':(picked.length>1?'Asking ':'Chatting with ')+who+' · '+MODES[S.mode][0]+' mode';
  $('#foot').textContent=picked.length>1?'Asking '+who+' at the same time. Each one uses its own sign-in, and Stop cancels all of them.'
    :'Uses your own subscriptions. Assistants can make mistakes — check important info.';
  $('#accbtn').classList.toggle('warn',S.none);}
$('#p-backend').addEventListener('click',e=>{const t=e.target.closest('.mi');if(!t)return;
  if(t.hasAttribute('data-openacc')){openAccounts();$('#p-backend').classList.remove('open');return;}
  if(t.disabled)return;toggleBackend(t.dataset.b);});
$('#p-mode').addEventListener('click',e=>{const t=e.target.closest('.mi');if(!t||t.disabled)return;setMode(t.dataset.m);$('#p-mode').classList.remove('open');});
function setBackend(b,quiet){
  const same=b===S.backend&&chosen().length===1;
  S.backend=b;store.set('backend',b);store.set('backends',JSON.stringify([b]));
  if(same){paint();return;}
  if(!quiet&&CHAT.items.length)note('Switched to '+NAMES[b]+(CHAT.sessions[b]?' — continuing its part of this chat':''));
  paint();loadModels();}
function toggleBackend(b){
  if(ACCLOADED&&!ready(b))return;
  let list=activeChoice().slice();
  if(list.includes(b)){if(list.length===1)return;list=list.filter(x=>x!==b);}
  else list.push(b);
  S.backend=list[0];store.set('backend',S.backend);store.set('backends',JSON.stringify(list));
  if(CHAT.items.length)note(list.length>1?'Asking '+list.map(x=>NAMES[x]).join(', ')+' together.':'Now chatting with '+NAMES[list[0]]+' only.');
  list.forEach(loadModelsFor);paint();}
function setMode(m){S.mode=m;store.set('mode',m);paint();}

const modelCache={};
async function loadModels(){return loadModelsFor(S.backend);}
async function loadModelsFor(b){
  if(!b)return;
  if(!modelCache[b]){modelCache[b]='loading';if(b===S.backend)paintModels();
    try{modelCache[b]=await fetch('/api/models?backend='+encodeURIComponent(b)).then(r=>r.json());}catch(e){modelCache[b]=[];}}
  if(b===S.backend)paintModels();}
function modelFor(b){const list=Array.isArray(modelCache[b])?modelCache[b]:[];const v=store.get('model_'+b)||'';
  return list.some(m=>m[0]===v)?v:'';}
function currentModel(){return modelFor(S.backend);}
function paintModels(){const list=modelCache[S.backend];const cur=currentModel();
  const several=activeChoice().length>1;
  const label=list==='loading'?'Loading…':cur?((list.find(m=>m[0]===cur)||[cur,cur])[1]):'Auto';
  pick('#p-model','<span class="lbl">'+esc(several?NAMES[S.backend]+' · '+label:label)+'</span>'+ICON.chev,
    '<div class="mh">Model'+(several?' for '+NAMES[S.backend]:'')+'</div>'+(several?'<div class="mh" style="text-transform:none;letter-spacing:0;font-size:12px">Other selected assistants use their own saved model.</div>':'')
    +'<button type="button" class="mi'+(cur?'':' sel')+'" data-model=""><span class="mt"><b>Auto</b><span>'+(S.backend==='ollama'?'Your most recently downloaded model':'Let '+NAMES[S.backend]+' choose')+'</span></span>'+ICON.check+'</button>'
    +(Array.isArray(list)?list.map(([id,l])=>'<button type="button" class="mi'+(id===cur?' sel':'')+'" data-model="'+esc(id)+'"><span class="mt"><b>'+esc(l)+'</b></span>'+ICON.check+'</button>').join(''):''));}
$('#p-model').addEventListener('click',e=>{const t=e.target.closest('.mi');if(!t)return;store.set('model_'+S.backend,t.dataset.model);paintModels();$('#p-model').classList.remove('open');});

// working folder
$('#folderbtn').onclick=()=>{$('#folderinput').value=S.workdir||S.home;$('#dfolder').showModal();$('#folderinput').select();};
$('#folderform').onsubmit=e=>{e.preventDefault();const v=$('#folderinput').value.trim();$('#dfolder').close();
  if(!v||v===S.workdir)return;S.workdir=v;store.set('workdir',v);
  if(CHAT.items.length){CHAT.sessions=fresh();CHAT.sent=fresh();note('Folder changed to '+v+' — assistants start fresh conversations.');}
  paint();};

// ===== composer =====
const msg=$('#msg');
function grow(){msg.style.height='auto';msg.style.height=Math.min(msg.scrollHeight,240)+'px';}
msg.addEventListener('input',grow);
msg.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#f').requestSubmit();}});
$('#f').onsubmit=e=>{e.preventDefault();if(S.busy||S.none)return;const t=msg.value.trim();if(!t)return;msg.value='';grow();send(t);};
$('#send').addEventListener('click',e=>{if(!S.busy)return;e.preventDefault();stopTurns();});
const SUGGEST=[['Plan a feature','plan','Plan how to add '],['Explain some code','ask','Explain what this code does: '],['Review my project','ask','Look at this folder and tell me what could be improved.'],['Remember something','','/remember ']];
$('#suggest').innerHTML=SUGGEST.map((s,i)=>'<button type="button" data-i="'+i+'">'+s[0]+'</button>').join('');
$('#suggest').onclick=e=>{const b=e.target.closest('button');if(!b)return;const s=SUGGEST[b.dataset.i];if(s[1])setMode(s[1]);msg.value=s[2];grow();msg.focus();};

async function readEvents(resp, onEvent){
  if(!resp.ok) throw new Error('The chat request failed ('+resp.status+').');
  const take=text=>{text.split('\n').forEach(line=>{line=line.trim();if(line)onEvent(JSON.parse(line));});};
  if(!resp.body||!resp.body.getReader){take(await resp.text());return;}
  const reader=resp.body.getReader(), dec=new TextDecoder();
  let buf='';
  for(;;){
    const {done,value}=await reader.read();
    if(done) break;
    buf+=dec.decode(value,{stream:true});
    let i;
    while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);if(line.trim())onEvent(JSON.parse(line));}
  }
  buf+=dec.decode();
  if(buf.trim()) onEvent(JSON.parse(buf));
}
function modelLabel(b, model, usage){
  if(b==='ollama') return (usage&&usage.model)||model||'';
  if(!model) return '';
  const list=modelCache[b];
  return (Array.isArray(list)&&list.find(m=>m[0]===model)||[model,model])[1];
}
function modeFor(b, mode){return NOFOLDER(b)&&mode==='agent'?'ask':mode;}
function turnParts(b, mode, model, r, partial, group){
  if(r.session_id)CHAT.sessions[b]=r.session_id;
  if(r.context_version!==undefined)CHAT.ctx[b]=r.context_version;
  const label=modelLabel(b, model, r.usage);
  const u=r.usage?{tok:(r.usage.input||0)+(r.usage.output||0),s:r.usage.seconds,cost:r.usage.cost}:null;
  const bots=[], rest=[];
  const bot=item=>{if(group)item.group=group;bots.push(item);};
  if(r.stopped){
    if(partial&&partial.trim()) bot({r:'bot',t:partial,b,mode,model:label});
    else bot({r:'err',t:'Stopped.',b,mode});
    rest.push({r:'note',t:group?'You stopped '+NAMES[b]+'.':'You stopped this reply.'});
  }else if(r.type==='done'||(r.reply!==undefined&&!r.error)){
    bot({r:'bot',t:r.reply||'(empty reply)',b,mode,model:label,u});
  }else{
    if(partial&&partial.trim()) bot({r:'bot',t:partial,b,mode,model:label});
    bot({r:'err',t:(r.error||'No reply.'),b,mode});
  }
  if(r.plan_file) rest.push({r:'plan',path:r.plan_file,b});
  if(r.plan_error) rest.push({r:'note',t:r.plan_error});
  if(r.notes_used&&r.notes_used.length){CHAT.sent[b]=(CHAT.sent[b]||[]).concat(r.notes_used.map(n=>n.path));
    rest.push({r:'note',t:'📓 Used from Obsidian: '+r.notes_used.map(n=>n.title).join(', ')});}
  (r.saved_memories||[]).forEach(m=>rest.push({r:'note',t:'🧠 Saved to memory: '+m.text,memId:m.id}));
  return {bots, rest, memories:!!(r.saved_memories&&r.saved_memories.length)};
}
function finishTurn(b, mode, model, r, partial){
  const parts=turnParts(b, mode, model, r, partial, null);
  parts.bots.concat(parts.rest).forEach(item=>addItem(item));
  if(parts.memories) refreshMemCount();
}
function pendingBubble(b, mode){
  const pend=document.createElement('div');pend.className='m-bot';
  const phase=mode==='plan'?'Planning':mode==='agent'?'Working':'Thinking';
  pend.innerHTML='<div class="avatar" style="background:'+color(b)+'">'+INITIAL[b]+'</div><div class="body"><div class="name">'+NAMES[b]+'</div><div class="thinking"><span class="dots"><i></i><i></i><i></i></span><span class="tm">'+phase+'…</span></div></div>';
  pend.dataset.phase=phase;return pend;
}
async function streamInto(pend, b, text, mode, model){
  const phase=pend.dataset.phase||'Thinking';
  let live='', r={}, raf=0;
  const turnId=newTurnId(), ac=new AbortController();
  inflight.set(turnId, ac);
  const t0=Date.now();
  const tick=setInterval(()=>{const s=Math.round((Date.now()-t0)/1000);const tm=pend.querySelector('.tm');if(tm)tm.textContent=phase+'… '+s+'s';},1000);
  const paintLive=()=>{let box=pend.querySelector('.md');
    if(!box){const th=pend.querySelector('.thinking');if(th)th.outerHTML='<div class="md"></div>';box=pend.querySelector('.md');}
    if(box)box.innerHTML=md(live);scrollDown();};
  try{
    const resp=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},signal:ac.signal,
      body:JSON.stringify({backend:b,message:text,session_id:CHAT.sessions[b],mode,context_version:CHAT.ctx[b],
        sent_notes:CHAT.sent[b]||[],model,workdir:S.workdir||S.home,turn_id:turnId})});
    await readEvents(resp, ev=>{
      if(ev.type==='delta'){live+=(ev.text||'');if(!raf)raf=requestAnimationFrame(()=>{raf=0;paintLive();});}
      else if(ev.type==='replace'){live=ev.text||'';if(!raf)raf=requestAnimationFrame(()=>{raf=0;paintLive();});}
      else if(ev.type==='status'){let s=pend.querySelector('.statusline');
        if(!s){s=document.createElement('div');s.className='statusline';const body=pend.querySelector('.body');if(body)body.appendChild(s);}
        if(s)s.textContent=ev.text||'';}
      else if(ev.type==='done'||ev.type==='error') r=ev;
    });
  }catch(err){
    if(err.name==='AbortError'){if(!r.type)r={type:'error',error:'Stopped.',stopped:true};}
    else if(!r.type) r={type:'error',error:String(err)};
  }finally{inflight.delete(turnId);clearInterval(tick);if(raf)cancelAnimationFrame(raf);}
  return {r:r.type?r:{type:'error',error:r.error||'No reply.'}, live};
}

async function send(text){
  const rm=text.match(/^\/remember\s+([\s\S]+)/i);
  if(rm){const r=await post('/api/memory',{action:'add',text:rm[1]});
    if(r.error)note(r.error);else{const m=r.memories.find(x=>x.text.toLowerCase()===rm[1].trim().replace(/\s+/g,' ').toLowerCase())||r.memories[r.memories.length-1];
      addItem({r:'note',t:'🧠 Saved to memory: '+m.text,memId:m.id});refreshMemCount();}return;}
  let targets=activeChoice().filter(ready);
  if(!targets.length) targets=[S.backend];
  if(targets.length>1) return sendMany(text, targets);
  const b=targets[0], mode=modeFor(b, S.mode), model=modelFor(b);
  CHAT.workdir=S.workdir||S.home; // each chat keeps its own folder (sessions belong to it)
  addItem({r:'you',t:text,b});
  const pend=pendingBubble(b, mode);
  thread.appendChild(pend);scrollDown();
  S.busy=true;paint();
  const {r,live}=await streamInto(pend, b, text, mode, model);
  pend.remove();S.busy=false;
  finishTurn(b, mode, model, r, live);
  saveChat();paint();msg.focus();}
async function sendMany(text, targets){
  const mode=S.mode;
  CHAT.workdir=S.workdir||S.home;
  addItem({r:'you',t:text,b:targets[0]});
  await Promise.all(targets.map(loadModelsFor));
  const wrap=document.createElement('div');wrap.className='compare';
  const cols=targets.map(b=>{
    const m=modeFor(b, mode), pend=pendingBubble(b, m);
    wrap.appendChild(pend);
    return {b, mode:m, model:modelFor(b), pend};
  });
  thread.appendChild(wrap);$('#main').classList.add('comparing');scrollDown();
  S.busy=true;paint();
  const results=await Promise.all(cols.map(async c=>({c, ...await streamInto(c.pend, c.b, text, c.mode, c.model)})));
  const group=newTurnId().slice(0,12);
  let memories=false;
  const bots=[], rest=[];
  results.forEach(({c,r,live})=>{
    const parts=turnParts(c.b, c.mode, c.model, r, live, group);
    bots.push(...parts.bots);rest.push(...parts.rest);memories=memories||parts.memories;
  });
  bots.forEach(item=>CHAT.items.push(item));
  rest.forEach(item=>CHAT.items.push(item));
  renderThread();scrollDown();saveChat();
  if(memories) refreshMemCount();
  S.busy=false;paint();msg.focus();}

// ===== settings & memory =====
function renderSettings(p){
  $('#use_memory').checked=p.settings.use_memory;$('#auto_memory').checked=p.settings.auto_memory;
  $('#memory_in_vault').checked=p.settings.memory_in_vault;$('#vault_search').checked=p.settings.vault_search;
  $('#vaultopts').hidden=!p.vault;$('#vaulthint').hidden=!!p.vault;
  if(document.activeElement!==$('#instructions'))$('#instructions').value=p.settings.instructions||'';
  $('#mempath').textContent=p.memories.length+' saved · '+p.path;$('#memclear').disabled=!p.memories.length;
  $('#memlist').innerHTML=p.memories.length?p.memories.map(m=>'<div class="mem" data-id="'+m.id+'"><input class="field" value="'+esc(m.text)+'" maxlength="500"><span class="src">'+(m.source==='assistant'?'by assistant':m.source==='obsidian'?'in Obsidian':'by you')+(m.created?' · '+esc(m.created):'')+'</span><button class="icon" type="button" title="Delete">'+ICON.trash+'</button></div>').join('')
    :'<div class="muted">No memories yet.</div>';
  $('#memcount').textContent=p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';}
async function memAct(body){const r=await post(body.settings?'/api/settings':'/api/memory',body);if(r.error)alert(r.error);renderSettings(r);CHAT.ctx=fresh();return r;}
function refreshMemCount(){fetch('/api/settings').then(r=>r.json()).then(p=>{$('#memcount').textContent=p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';if($('#set').open)renderSettings(p);});}
$('#setbtn').onclick=async()=>{$('#set').showModal();renderSettings(await fetch('/api/settings').then(r=>r.json()));};
for(const k of ['use_memory','auto_memory','memory_in_vault','vault_search'])$('#'+k).onchange=e=>memAct({settings:{[k]:e.target.checked}});
$('#instsave').onclick=async()=>{await memAct({settings:{instructions:$('#instructions').value}});
  $('#instmsg').textContent='Saved — new chats will use it.';setTimeout(()=>$('#instmsg').textContent='',3000);};
$('#memadd').onsubmit=async e=>{e.preventDefault();const t=$('#memtext').value.trim();if(!t)return;const r=await memAct({action:'add',text:t});if(!r.error)$('#memtext').value='';};
$('#memlist').addEventListener('change',e=>{const row=e.target.closest('.mem');if(row)memAct({action:'edit',id:row.dataset.id,text:e.target.value});});
$('#memlist').addEventListener('click',e=>{const row=e.target.closest('.mem');if(row&&e.target.closest('button'))memAct({action:'delete',id:row.dataset.id});});
$('#memclear').onclick=()=>{if(confirm('Delete all memories? This cannot be undone.'))memAct({action:'clear'});};
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());

// ===== Obsidian =====
let VAULT=null;
function renderObsidian(p){
  VAULT=p.vault;$('#savebtn').hidden=!VAULT;
  let h='<div class="sec-h"><b>Obsidian</b><span class="pill '+(VAULT?'ok">Connected · '+esc(VAULT.name)+' · '+VAULT.notes+' notes':'bad">Not connected')+'</span></div>'
    +'<div class="muted">Long-term memory shared by every assistant. Your vault stays on this computer; no sign-in needed. Related notes are sent only to the assistant you chat with.</div>';
  if(VAULT)h+='<code class="path">'+esc(VAULT.path)+'</code><div class="row"><button class="ghost" data-obs="disconnect">Disconnect</button></div>';
  else{h+=(p.detected||[]).map(d=>'<div class="row"><button class="btn" data-obs="connect" data-path="'+esc(d)+'">Connect “'+esc(d.split('/').pop())+'”</button><code class="path">'+esc(d)+'</code></div>').join('');
    h+='<form class="row" id="obsform"><input class="field" id="obspath" placeholder="Or type the vault folder, e.g. ~/Documents/MyVault" style="flex:1;min-width:200px"><button class="ghost">Connect</button></form>';}
  $('#obs').innerHTML=h;
  $('#memcount').textContent=p.memories&&p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';}
async function obsAct(body){const r=await post('/api/obsidian',body);if(r.error)alert(r.error);renderObsidian(r);if($('#set').open)renderSettings(r);CHAT.ctx=fresh();return r;}
$('#obs').addEventListener('click',e=>{const t=e.target.closest('[data-obs]');if(!t)return;
  if(t.dataset.obs==='disconnect'&&!confirm('Disconnect Obsidian? Your notes stay where they are; memories go back to Chatbox’s own list.'))return;
  obsAct({action:t.dataset.obs,path:t.dataset.path});});
$('#obs').addEventListener('submit',e=>{e.preventDefault();const v=$('#obspath').value.trim();if(v)obsAct({action:'connect',path:v});});
$('#savebtn').onclick=async()=>{const msgs=CHAT.items.filter(i=>i.r==='you'||i.r==='bot').map(i=>({role:i.r==='you'?'you':'bot',who:i.r==='you'?'You':NAMES[i.b],text:i.t}));
  if(!msgs.length){note('Nothing to save yet.');return;}
  const r=await post('/api/obsidian',{action:'save_chat',messages:msgs,backend:NAMES[S.backend]});
  addItem({r:'note',t:r.error?r.error:'📓 Saved to Obsidian: '+r.saved});};

// ===== Accounts =====
let ACC={},ACCLOADED=false,accJSON='',accTimer=null;const codes={};
const ready=b=>{const a=ACC[SVC[b]];return !!(a&&a.installed&&a.logged_in);};
function markBackends(){
  ACCLOADED=true;const any=BACKENDS.some(ready);
  if(!ready(S.backend)&&any){const was=S.backend;S.backend=BACKENDS.find(ready);store.set('backend',S.backend);
    if(CHAT.items.length)note(NAMES[was]+' is not connected, so switched to '+NAMES[S.backend]+'.');loadModels();}
  paint();}
async function refreshAccounts(fresh){
  let list;try{list=await fetch('/api/accounts'+(fresh?'?fresh=1':'')).then(r=>r.json());}catch(e){return;}
  const wasChat=ACC.chatgpt&&ACC.chatgpt.logged_in;
  ACC=Object.fromEntries(list.map(a=>[a.id,a]));
  if(ACC.chatgpt&&ACC.chatgpt.logged_in&&!wasChat){delete modelCache.chatgpt;if(S.backend==='chatgpt'||activeChoice().includes('chatgpt'))loadModelsFor('chatgpt');}
  markBackends();
  const j=JSON.stringify(list);if(j!==accJSON){accJSON=j;renderAccounts(list);}
  clearTimeout(accTimer);
  if($('#acc').open)accTimer=setTimeout(()=>refreshAccounts(),list.some(a=>a.job&&a.job.state==='running')?1500:15000);
  return list;}
let ollamaTyped=null; // address being typed (survives the list refreshing)
function ollamaCard(a){
  const st=!a.installed?['bad','Not running']:a.models?['ok','Running · '+esc(a.account)]:['bad','Running · no models yet'];
  let h='<div class="sec"><div class="sec-h"><span class="bdot" style="background:var(--ollama)"></span><b>Ollama</b><span class="pill '+st[0]+'">'+st[1]+'</span></div>'
    +'<div class="muted">'+esc(a.plan)+'. Plain chat: no files or commands.</div>';
  if(!a.installed)h+='<div class="muted">Install Ollama and start it, then click “Check again”.</div>'
    +'<div class="row"><button class="btn" data-act="open" data-url="https://ollama.com/download">Get Ollama</button><button class="ghost" data-act="ollamacheck">Check again</button></div>';
  else if(!a.models)h+='<div class="muted">Download a model in a terminal, then click “Check again”. For example:</div>'
    +'<div class="row"><code>ollama pull llama3.2</code><button class="ghost" data-act="copycode" data-code="ollama pull llama3.2">Copy</button><button class="ghost" data-act="ollamacheck">Check again</button></div>';
  else h+='<div class="row"><button class="ghost" data-act="ollamacheck">Refresh models</button></div>';
  h+='<form class="row" id="ollamaform"><span class="muted">Address</span><input class="field" id="ollamaurl" value="'+esc(ollamaTyped??a.url)+'" placeholder="http://localhost:11434" style="flex:1;min-width:200px"><button class="ghost">Save</button></form></div>';
  return h;}
function renderAccounts(list){
  $('#acclist').innerHTML=list.map(a=>{if(a.id==='ollama')return ollamaCard(a);
    const st=!a.installed?['bad','Not installed']:a.logged_in?['ok','Connected'+(a.account?' · '+esc(a.account):'')]:['bad','Not signed in'];
    const busy=a.job&&a.job.state==='running';const dis=busy?' disabled':'';
    let btns='';
    if(!a.installed)btns='<button class="btn" data-act="install" data-s="'+a.id+'"'+dis+'>Install</button>';
    else if(!a.logged_in)btns=(a.id==='chatgpt'
      ?'<button class="btn" data-act="login" data-s="chatgpt" data-oai-siwc-button'+dis+'>Continue with ChatGPT</button>'
        +(a.has_client?' <button class="ghost" data-act="login_new" data-s="chatgpt"'+dis+'>Use a different account</button>':'')
      :'<button class="btn" data-act="login" data-s="'+a.id+'"'+dis+'>Sign in</button>'
      +(a.id==='codex'?' <button class="ghost" data-act="login_browser" data-s="codex"'+dis+' title="Older method: the browser sends you back to Codex on this computer">Sign in with browser redirect</button>':''));
    else btns='<button class="ghost" data-act="logout" data-s="'+a.id+'"'+dis+'>Sign out</button>';
    if(a.installed&&a.id==='codex'&&a.source)btns+=' <button class="ghost" data-act="install" data-s="codex"'+dis+' title="OpenAI’s standalone Codex is newer and supports more models">Install standalone Codex</button>';
    let job='';
    if(a.job){const J=a.job,what={install:'Installing',login:'Signing in',logout:'Signing out'}[J.kind];
      const state=J.state==='running'?what+'…':J.state==='done'?{install:'Installed',login:'Sign-in finished',logout:'Signed out'}[J.kind]+' ✓':J.state==='cancelled'?'Cancelled':'Something went wrong';
      job='<div class="job"><b>'+state+'</b>';
      if(J.state==='running'&&J.kind==='login'&&J.user_code){ // Codex device code
        job+='<div class="muted">1. A sign-in page should open in your browser. Sign in to your ChatGPT account.</div>'
          +'<div class="muted">2. Enter this one-time code there (it expires in 15 minutes):</div>'
          +'<div class="row"><code style="font-size:20px;letter-spacing:.08em;padding:4px 12px">'+esc(J.user_code)+'</code><button class="ghost" data-act="copycode" data-code="'+esc(J.user_code)+'">Copy code</button>'
          +(J.url?'<button class="ghost" data-act="open" data-url="'+esc(J.url)+'">Open sign-in page</button>':'')+'</div>'
          +'<div class="muted">This finishes by itself once you enter the code. If the page says device sign-in is off, turn on “device code authorization” in ChatGPT’s security settings, or use “Sign in with browser redirect”.</div>';}
      else if(J.state==='running'&&J.kind==='login'){
        job+='<div class="muted">Your browser should open. Sign in there'+(a.id==='claude'?', then copy the code it shows and paste it below.':' and this finishes by itself.')+'</div>';
        if(J.url)job+='<div class="row"><button class="ghost" data-act="open" data-url="'+esc(J.url)+'">Open sign-in page</button></div>';
        if(a.id==='codex')job+='<div class="muted">Keep the ChatGPT app closed while signing in: it can cancel this sign-in.</div>';}
      if(J.last&&J.state!=='cancelled'&&!/^(Opening browser|If the browser)/.test(J.last))job+='<div class="muted">'+esc(J.last)+'</div>';
      if(J.asks_code)job+='<div class="row"><input class="field" id="code_'+a.id+'" placeholder="Paste the code from the browser" value="'+esc(codes[a.id]||'')+'"><button class="btn" data-act="input" data-s="'+a.id+'">Submit code</button></div>';
      if(J.output)job+='<details'+(J.state==='failed'?' open':'')+'><summary class="muted">Details</summary><pre>'+esc(J.output)+'</pre></details>';
      job+='<div class="row">'+(J.state==='running'?'<button class="ghost" data-act="cancel" data-s="'+a.id+'">Cancel</button>':'<button class="ghost" data-act="dismiss" data-s="'+a.id+'">OK</button>')+'</div></div>';}
    const dots='<span class="bdot" style="background:'+color(a.id==='codex'?'codex':a.id)+'"></span>';
    return '<div class="sec"><div class="sec-h">'+dots+'<b>'+esc(a.name)+'</b><span class="pill '+st[0]+'">'+st[1]+'</span></div>'
      +'<div class="muted">Needs: '+esc(a.plan)+(a.source?' · Codex '+esc(a.source):'')+'</div>'
      +'<div class="row">'+btns+'</div>'+job+'</div>';}).join('');}
$('#acclist').addEventListener('input',e=>{if(e.target.id==='ollamaurl')ollamaTyped=e.target.value;if(e.target.id&&e.target.id.startsWith('code_'))codes[e.target.id.slice(5)]=e.target.value;});
$('#acclist').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id&&e.target.id.startsWith('code_')){e.preventDefault();e.target.nextElementSibling.click();}});
function ollamaRefresh(){delete modelCache.ollama;if(S.backend==='ollama')loadModels();accJSON='';return refreshAccounts(true);}
$('#acclist').addEventListener('submit',async e=>{if(e.target.id!=='ollamaform')return;e.preventDefault();
  const r=await post('/api/settings',{settings:{ollama_url:$('#ollamaurl').value.trim()}});if(r.error)alert(r.error);
  ollamaTyped=null;ollamaRefresh();});
$('#acclist').addEventListener('click',async e=>{
  const t=e.target.closest('[data-act]');if(!t)return;const act=t.dataset.act,s=t.dataset.s,name=ACC[s]&&ACC[s].name;
  if(act==='open'){post('/api/open',{url:t.dataset.url});return;}
  if(act==='copycode'){copy(t.dataset.code,t);return;}
  if(act==='ollamacheck'){t.disabled=true;ollamaRefresh();return;}
  if(act==='install'&&!confirm('Download and run the official '+name+' installer?'))return;
  if(act==='logout'&&!confirm('Sign out of '+name+' on this computer?'+(s==='codex'?'\n\nThis also signs out Codex in the ChatGPT app.':s==='chatgpt'?'\n\nThis disconnects Chatbox from your ChatGPT plan here. You can also remove Chatbox in ChatGPT settings.':'')))return;
  const body={service:s,action:act};
  if(act==='input'){body.text=codes[s]||'';if(!body.text.trim())return;codes[s]='';}
  t.disabled=true;const r=await post('/api/account',body);if(r.error)alert(r.error);
  if(act==='dismiss'){for(const k in modelCache)delete modelCache[k];loadModels();}
  accJSON='';refreshAccounts(act==='dismiss');});
function openAccounts(){if(!$('#acc').open)$('#acc').showModal();accJSON='';refreshAccounts(true);fetch('/api/settings').then(r=>r.json()).then(renderObsidian);}
$('#accbtn').onclick=openAccounts;
$('#acc').addEventListener('close',()=>{clearTimeout(accTimer);refreshAccounts(true);});


// ===== Usage =====
const UORDER=['claude','cursor','codex','chatgpt','ollama']; // fixed chart order (validated palette)
const fmtN=n=>n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+'k':String(Math.round(n));
const fmtS=s=>s>=90?Math.round(s/60)+'m':s>=10?Math.round(s)+'s':(Math.round(s*10)/10)+'s';
const fmt$=c=>'$'+(c>=100?c.toFixed(0):c>=1?c.toFixed(2):c.toFixed(3));
function usageMeta(u){return (u.tok?' · '+fmtN(u.tok)+' tokens':'')+(u.s?' · '+fmtS(u.s):'')+(typeof u.cost==='number'?' · '+fmt$(u.cost):'');}
const U={days:+(store.get('u_days')||30),metric:store.get('u_metric')||'messages',data:null};
async function loadUsage(){U.data=await fetch('/api/usage?days='+U.days).then(r=>r.json());renderUsage();}
function renderUsage(){const d=U.data;if(!d)return;
  document.querySelectorAll('#u-range button').forEach(b=>b.classList.toggle('on',+b.dataset.d===U.days));
  document.querySelectorAll('#u-metric button').forEach(b=>b.classList.toggle('on',b.dataset.k===U.metric));
  $('#u-tiles').innerHTML=UORDER.map(b=>{const t=d.totals[b];
    return '<div class="tile"><div class="tn"><span class="bdot" style="background:'+color(b)+'"></span>'+NAMES[b]+'</div>'
      +'<div class="big">'+fmtN(t.messages)+'</div><div class="sm">'+(t.messages===1?'message':'messages')+(t.errors?' · '+t.errors+' failed':'')+'</div>'
      +'<div class="sm">'+fmtN(t.input+t.output)+' tokens'+(t.messages?' · avg '+fmtS(t.seconds/t.messages):'')+'</div>'
      +(t.has_cost?'<div class="sm">≈ '+fmt$(t.cost)+' at API prices</div>':'')+'</div>';}).join('');
  $('#u-charttitle').textContent=(U.metric==='tokens'?'Tokens':'Messages')+' per day';
  $('#u-legend').innerHTML=UORDER.map(b=>'<span><i style="background:'+color(b)+'"></i>'+NAMES[b]+'</span>').join('');
  drawChart(d.series,U.metric);
  $('#u-modes').textContent=d.count?'Ask '+d.modes.ask+' · Plan '+d.modes.plan+' · Agent '+d.modes.agent:'';
  $('#u-models').innerHTML=d.models.length?'<table class="utable"><thead><tr><th>Assistant</th><th>Model</th><th class="n">Messages</th><th class="n">Tokens in</th><th class="n">Tokens out</th><th class="n">Avg time</th><th class="n">Cost</th></tr></thead><tbody>'
    +d.models.map(m=>'<tr><td><span class="row" style="gap:6px"><span class="bdot" style="background:'+color(m.backend)+'"></span>'+NAMES[m.backend]+'</span></td><td>'+esc(m.model)+'</td><td class="n">'+m.messages+'</td><td class="n">'+fmtN(m.input)+'</td><td class="n">'+fmtN(m.output)+'</td><td class="n">'+fmtS(m.seconds/m.messages)+'</td><td class="n">'+(m.has_cost?fmt$(m.cost):'—')+'</td></tr>').join('')+'</tbody></table>'
    :'<div class="uempty">No usage yet — send a message and it shows up here.</div>';}
function drawChart(series,key){
  const box=$('#u-chart'),W=box.clientWidth||800,H=box.clientHeight||220,L=40,B=22,T=8;
  const vals=series.map(d=>UORDER.reduce((a,b)=>a+d[b][key],0));const max=Math.max(1,...vals);
  const step=Math.pow(10,Math.floor(Math.log10(max)));const top=Math.ceil(max/step)*step;
  const n=series.length,cw=(W-L)/n,bw=Math.max(2,Math.min(28,cw*0.62)),y=v=>T+(H-B-T)*(1-v/top);
  let g='';
  for(let k=0;k<=3;k++){const v=top*k/3,yy=y(v);g+='<line class="gl" x1="'+L+'" x2="'+W+'" y1="'+yy+'" y2="'+yy+'"/><text class="ax" x="'+(L-6)+'" y="'+(yy+4)+'" text-anchor="end">'+fmtN(v)+'</text>';}
  const every=Math.ceil(n/8);
  series.forEach((d,i)=>{const x=L+i*cw+(cw-bw)/2;let base=H-B;let seg='';
    const parts=UORDER.filter(b=>d[b][key]>0);
    parts.forEach((b,j)=>{const h=(H-B-T)*d[b][key]/top;const gap=j<parts.length-1?2:0;let yy=base-h;const hh=Math.max(1,h-gap);
      const r=j===parts.length-1?Math.min(4,hh,bw/2):0;
      seg+=r?'<path fill="'+color(b)+'" d="M'+x+','+(base)+'V'+(yy+r)+'Q'+x+','+yy+' '+(x+r)+','+yy+'H'+(x+bw-r)+'Q'+(x+bw)+','+yy+' '+(x+bw)+','+(yy+r)+'V'+base+'Z"/>'
        :'<rect fill="'+color(b)+'" x="'+x+'" y="'+(base-hh)+'" width="'+bw+'" height="'+hh+'"/>';
      base=yy;});
    const lbl=((i%every===0&&n-1-i>=every/2)||i===n-1)?'<text class="ax" x="'+(x+bw/2)+'" y="'+(H-6)+'" text-anchor="middle">'+d.date.slice(5).replace('-','/')+'</text>':'';
    g+='<g class="col" data-i="'+i+'"><rect class="hit" x="'+(L+i*cw)+'" y="'+T+'" width="'+cw+'" height="'+(H-B-T)+'" rx="4"/>'+seg+lbl+'</g>';});
  box.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+(key==='tokens'?'Tokens':'Messages')+' per day by assistant">'+g+'</svg><div class="tip"></div>';
  const tip=box.querySelector('.tip');
  box.querySelectorAll('.col').forEach(c=>{c.addEventListener('mousemove',e=>{const d=series[+c.dataset.i];const tot=UORDER.reduce((a,b)=>a+d[b][key],0);
      tip.innerHTML='<b>'+new Date(d.date+'T12:00').toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'})+'</b>'
        +UORDER.map(b=>'<div><span class="bdot" style="background:'+color(b)+'"></span>'+NAMES[b]+'<em>'+fmtN(d[b][key])+'</em></div>').join('')
        +'<div style="border-top:1px solid var(--line);margin-top:4px;padding-top:4px">Total<em>'+fmtN(tot)+'</em></div>';
      tip.style.display='block';const r=box.getBoundingClientRect();let lx=e.clientX-r.left+14;if(lx+170>r.width)lx-=190;tip.style.left=lx+'px';tip.style.top=Math.max(0,e.clientY-r.top-40)+'px';});
    c.addEventListener('mouseleave',()=>tip.style.display='none');});}
$('#usebtn').onclick=()=>{$('#usage').showModal();loadUsage();};
$('#u-range').onclick=e=>{const b=e.target.closest('button');if(!b)return;U.days=+b.dataset.d;store.set('u_days',U.days);loadUsage();};
$('#u-metric').onclick=e=>{const b=e.target.closest('button');if(!b)return;U.metric=b.dataset.k;store.set('u_metric',U.metric);renderUsage();};
$('#u-note').addEventListener('click',e=>{const a=e.target.closest('a[data-href]');if(a)post('/api/open',{url:a.dataset.href});});
$('#u-clear').onclick=async()=>{if(!confirm('Clear all usage statistics? Chats and memories are not affected.'))return;await post('/api/usage',{action:'clear'});loadUsage();};
addEventListener('resize',()=>{if($('#usage').open)renderUsage();});

// ===== start =====
CHAT=newChatState();setEmpty();paint();
const h=new Date().getHours();$('#greet').textContent=h<5?'Up late? What can I help with?':h<12?'Good morning. What can I help with?':h<18?'Good afternoon. What can I help with?':'Good evening. What can I help with?';
fetch('/api/status').then(r=>r.json()).then(s=>{S.home=s.home;paint();loadModels();});
fetch('/api/settings').then(r=>r.json()).then(renderObsidian);
fetch('/api/chats').then(r=>r.json()).then(renderChatList);
refreshAccounts(true).then(list=>{if(list&&!list.some(a=>a.installed&&a.logged_in))openAccounts();});
msg.focus();

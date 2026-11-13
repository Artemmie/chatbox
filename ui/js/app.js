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
let carry=null; // backend whose next fresh turn should include the transcript
function newChatState(){return {id:null,title:'',created:Date.now(),backend:S.backend,project:'',project_lock:false,items:[],sessions:fresh(),ctx:fresh(),sent:fresh()};}

// In-app dialog. The desktop window's web page does not show window.confirm,
// so those calls returned false and the button did nothing.
const askQ=[];
let askOn=false;
function ask(opts){
  return new Promise(resolve=>{
    askQ.push({opts:opts||{}, resolve});
    if(!askOn) runAsk();
  });
}
function runAsk(){
  const job=askQ.shift();
  if(!job){askOn=false;return;}
  askOn=true;
  presentAsk(job.opts).then(result=>{job.resolve(result);runAsk();});
}
function presentAsk(o){
  const dlg=$('#ask'), title=$('#asktitle'), text=$('#asktext'), input=$('#askinput'), box=$('#askbtns');
  const buttons=o.buttons||[{id:'ok',label:o.ok||'OK',primary:true},{id:'cancel',label:o.cancel||'Cancel',cancel:true}];
  const wants=!!o.input;
  title.textContent=o.title||'Chatbox';
  text.textContent=o.message||'';
  text.hidden=!o.message;
  input.hidden=!wants;
  input.value=o.value||'';
  if(wants) input.setAttribute('aria-label', o.message||'Answer');
  box.innerHTML=buttons.map(b=>'<button type="button" class="'+(b.primary?'btn':'ghost')+'" data-ask="'+esc(b.id)+'"'+(b.cancel?' data-cancel="1"':'')+'>'+esc(b.label)+'</button>').join('');
  return new Promise(resolve=>{
    let result=null, settled=false;
    const detach=()=>{
      dlg.removeEventListener('close', onClose);
      dlg.removeEventListener('keydown', onKey);
      dlg.removeEventListener('cancel', onCancel);
      box.removeEventListener('click', onClick);
    };
    const finish=value=>{
      if(settled) return;
      settled=true;
      result=value;
      detach();
      if(dlg.open) dlg.close();
      resolve(result);
    };
    const onClose=()=>{
      detach();
      if(!settled){settled=true;resolve(null);}
    };
    const onCancel=e=>{e.preventDefault();finish(null);};
    const onClick=e=>{
      const b=e.target.closest('[data-ask]');
      if(!b) return;
      e.preventDefault();
      finish(b.dataset.cancel?null:{id:b.dataset.ask, value:wants?input.value:''});
    };
    const onKey=e=>{
      if(e.key!=='Enter') return;
      const active=document.activeElement;
      if(active&&active.dataset&&active.dataset.cancel){e.preventDefault();e.stopPropagation();finish(null);return;}
      if(active&&active.dataset&&active.dataset.ask&&!active.dataset.cancel){
        e.preventDefault();e.stopPropagation();
        finish({id:active.dataset.ask, value:wants?input.value:''});
        return;
      }
      const primary=box.querySelector('.btn[data-ask]')||box.querySelector('[data-ask]:not([data-cancel])');
      if(primary){e.preventDefault();e.stopPropagation();finish({id:primary.dataset.ask, value:wants?input.value:''});}
    };
    dlg.addEventListener('cancel', onCancel);
    dlg.addEventListener('close', onClose);
    dlg.addEventListener('keydown', onKey);
    box.addEventListener('click', onClick);
    dlg.showModal();
    if(wants) input.focus();
    else{const primary=box.querySelector('.btn')||box.querySelector('button'); if(primary) primary.focus();}
  });
}
function askConfirm(message, title){
  return ask({title:title||'Confirm', message, buttons:[
    {id:'ok', label:'OK', primary:true},
    {id:'cancel', label:'Cancel', cancel:true},
  ]}).then(r=>!!(r&&r.id==='ok'));
}
function askAlert(message, title){
  return ask({title:title||'Chatbox', message, buttons:[{id:'ok', label:'OK', primary:true}]});
}
function askPrompt(message, value){
  return ask({title:'Chatbox', message, input:true, value:value||'', buttons:[
    {id:'ok', label:'OK', primary:true},
    {id:'cancel', label:'Cancel', cancel:true},
  ]}).then(r=>r&&r.id==='ok'?r.value:null);
}
$('#askform').addEventListener('submit', e=>e.preventDefault());

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
let PROJECTS=[];
const PROJICON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg>';
function renderProject(){
  const box=$('#p-project');if(!box)return;
  const show=!!VAULT;box.hidden=!show;if(!show)return;
  const current=CHAT&&CHAT.project||'';
  const names=PROJECTS.map(p=>p.name);
  if(current&&names.indexOf(current)<0)names.push(current);
  const item=(value,label,sub)=>'<button type="button" class="mi'+(value===current?' sel':'')+'" data-project="'+esc(value)+'"><span class="mt"><b>'+esc(label)+'</b>'+(sub?'<span>'+sub+'</span>':'')+'</span>'+ICON.check+'</button>';
  pick('#p-project',PROJICON+'<span class="lbl">'+esc(current||'Project')+'</span>'+ICON.chev,
    '<div class="mh">Project</div><div class="mnote">Where this chat is filed in your notes. Chatbox picks one from your message and folder unless you choose.</div>'
    +item('','No project','')
    +names.map(n=>item(n,n,'')).join('')
    +'<button type="button" class="mi" data-newproject><span class="mt"><span>New project…</span></span></button>');
}
function setEmpty(){ensureHandoff();const e=!CHAT.items.length;$('#main').classList.toggle('is-empty',e);$('#suggest').hidden=!e;
  $('#title').textContent=e?'':(CHAT.title||'New chat');const ho=$('#p-handoff');if(ho)ho.hidden=e;renderProject();}
function actionsHTML(withRemember=true){return '<div class="actions"><button type="button" data-copy>'+ICON.copy+'Copy</button>'
  +(withRemember?'<button type="button" data-remember>'+ICON.brain+'Remember</button>':'')
  +'<button type="button" data-ask>Ask another AI about this</button><div class="askpop" hidden></div></div>';}
function render(item, parent){ // one stored item -> DOM. parent defaults to the thread.
  const d=document.createElement('div');d.item=item;
  if(item.r==='you'){d.className='m-you';
    const files=item.files&&item.files.length?'<div class="files">'+item.files.map(f=>'<span>'+esc(f.name)+'</span>').join('')+'</div>':'';
    d.innerHTML='<div class="bubble">'+(item.t?esc(item.t):'')+files+'</div>'+actionsHTML();}
  else if(item.r==='bot'||item.r==='err'){d.className='m-bot';const b=item.b;const who=NAMES[b]||'Assistant';
    d.innerHTML='<div class="avatar" style="background:'+color(b)+'">'+(INITIAL[b]||who.slice(0,1))+'</div><div class="body"><div class="name">'+who
      +(item.mode?'<span class="meta">'+MODES[item.mode][0]+(item.model?' · '+esc(item.model):'')+(item.u?'<span class="more">'+usageMeta(item.u)+'</span>':'')+'</span>':'')+'</div>'
      +(item.r==='err'?'<div class="err">'+esc(item.t)+'</div>'+(/Open Accounts/.test(item.t)?'<div class="row" style="margin-top:8px"><button class="ghost" type="button" data-openacc>Open Accounts</button></div>':'')
        :'<div class="md">'+md(item.t)+'</div>'+actionsHTML())+'</div>';}
  else if(item.r==='plan'){d.className='card';
    d.innerHTML='<div class="ic">'+ICON.plan+'</div><div class="tx"><b>Plan saved</b><span title="'+esc(item.path)+'">'+esc(item.path)+'</span></div>'
      +'<button class="ghost" type="button" data-planopen>Open</button>'+(NOFOLDER(item.b)?'':'<button class="btn" type="button" data-plango>Implement plan</button>');}
  else{const mem=!!item.memId;d.className='note'+(mem?' mem':'');
    d.innerHTML=esc(item.t)+(mem?' <button type="button" data-undo>Undo</button>':'');}
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
    send('Implement the plan in "'+item.path+'". Work through the checklist step by step, and tick off each item in that file ("- [x]") as you finish it.');return;}
  if(t.hasAttribute('data-ask')||t.hasAttribute('data-askto')) e.stopPropagation();
  if(t.hasAttribute('data-ask')){openAskMenu(row,item);return;}
  if(t.hasAttribute('data-askto')){closeAskMenus();askOther(item,t.dataset.askto);}});
async function copy(text,btn){try{await navigator.clipboard.writeText(text);}catch(e){const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();}
  const old=btn.innerHTML;btn.innerHTML=ICON.copy+'Copied';setTimeout(()=>btn.innerHTML=old,1400);}

// ===== chat history =====
let saveTimer=null;
function saveChat(){if(!CHAT.items.some(i=>i.r==='you'))return;
  if(!CHAT.id){CHAT.id=[...crypto.getRandomValues(new Uint8Array(6))].map(x=>x.toString(16).padStart(2,'0')).join('');}
  if(!CHAT.title){const f=CHAT.items.find(i=>i.r==='you');CHAT.title=f.t.replace(/\s+/g,' ').slice(0,60);}
  CHAT.backend=S.backend;clearTimeout(saveTimer);
  saveTimer=setTimeout(async()=>{const items=CHAT.items.filter(i=>!i.temp);
    const list=await post('/api/chats',{action:'save',chat:{...CHAT,items}});
    const row=Array.isArray(list)&&list.find(c=>c.id===CHAT.id);
    if(row){CHAT.updated=row.updated;if(!CHAT.project_lock&&row.project!==undefined)CHAT.project=row.project||'';}
    renderChatList(list);renderProject();},250);
  setEmpty();}
let CHATLIST=[];
function renderChatList(list){
  if(!Array.isArray(list))return;
  CHATLIST=list;
  const box=$('#chatsearch'),q=(box.value||'').trim().toLowerCase();
  $('#chatsearchwrap').hidden=list.length<6&&!q;
  if(q)list=list.filter(c=>(c.title||'Untitled').toLowerCase().includes(q));
  const now=new Date(),day=86400000,start=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
  const groups=[['Today',c=>c.updated*1000>=start],['Yesterday',c=>c.updated*1000>=start-day],['Previous 7 days',c=>c.updated*1000>=start-7*day],['Previous 30 days',c=>c.updated*1000>=start-30*day],['Older',()=>true]];
  const used=new Set();let h='';
  for(const [name,test] of groups){const g=list.filter(c=>!used.has(c.id)&&test(c));g.forEach(c=>used.add(c.id));
    if(g.length)h+='<div class="group">'+name+'</div>'+g.map(c=>'<div class="chatitem'+(CHAT&&c.id===CHAT.id?' cur':'')+'" data-id="'+c.id+'"><span class="bdot" style="background:'+color(c.backend||'claude')+'"></span><span class="t">'+esc(c.title||'Untitled')+'</span><button class="icon del" type="button" title="Delete chat">'+ICON.trash+'</button></div>').join('');}
  $('#chatlist').innerHTML=h||'<div class="side-empty">'+(q?'No chats match “'+esc(box.value.trim())+'”.':'Your chats will show up here.')+'</div>';}
$('#chatsearch').addEventListener('input',()=>renderChatList(CHATLIST));
$('#chatsearch').addEventListener('keydown',e=>{if(e.key==='Escape'&&e.target.value){e.preventDefault();e.target.value='';renderChatList(CHATLIST);}});
// rename the open chat from its title
$('#title').onclick=async()=>{
  if(!CHAT.items.some(i=>i.r==='you'))return;
  const v=await askPrompt('Rename this chat',CHAT.title||'');
  const t=(v||'').replace(/\s+/g,' ').trim().slice(0,80);
  if(!t||t===CHAT.title)return;
  CHAT.title=t;saveChat();};
// jump back to the newest message
$('#scroll').addEventListener('scroll',()=>{const sc=$('#scroll');$('#tolatest').hidden=sc.scrollHeight-sc.scrollTop-sc.clientHeight<240;},{passive:true});
$('#tolatest').onclick=scrollDown;
$('#chatlist').addEventListener('click',async e=>{const it=e.target.closest('.chatitem');if(!it)return;const id=it.dataset.id;
  if(e.target.closest('.del')){if(!await askConfirm('Delete this chat?'))return;renderChatList(await post('/api/chats',{action:'delete',id}));if(CHAT.id===id)startNew();return;}
  if(S.busy){note('Stop the current reply before opening another chat.');return;}
  const c=await fetch('/api/chats/'+id).then(r=>r.json());if(c.error)return;
  carry=null;
  CHAT={...newChatState(),...c,sessions:{...fresh(),...c.sessions},ctx:{...fresh(),...c.ctx},sent:{...fresh(),...c.sent}};
  renderThread();
  if(c.workdir){S.workdir=c.workdir;store.set('workdir',c.workdir);}
  if(c.backend&&ready(c.backend))setBackend(c.backend,true);paint();
  document.querySelectorAll('.chatitem').forEach(x=>x.classList.toggle('cur',x.dataset.id===id));
  if(innerWidth<760)$('#app').classList.add('noside');scrollDown();$('#msg').focus();});
function startNew(){if(S.busy)return;carry=null;CHAT=newChatState();thread.innerHTML='';$('#main').classList.remove('comparing');setEmpty();
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
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelectorAll('.pick.open').forEach(x=>x.classList.remove('open'));closeAskMenus();}});
document.addEventListener('click',e=>{if(e.target.closest('.askpop')||e.target.closest('[data-ask]'))return;closeAskMenus();});
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
    :'Enter sends, Shift+Enter adds a line. Assistants can make mistakes — check important info.';
  $('#accbtn').classList.toggle('warn',S.none);
  if(typeof paintClips==='function') paintClips();}
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

// Chatbox's own folder list, for when the system has no folder dialog to show
const FOLDERICON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
function browseFolder(start,title){
  return new Promise(resolve=>{
    const dlg=$('#dbrowse');let cur=null,done=false;
    $('#browsetitle').textContent=title||'Choose a folder';
    const finish=v=>{if(done)return;done=true;dlg.removeEventListener('click',onClick);dlg.removeEventListener('close',onClose);if(dlg.open)dlg.close();resolve(v);};
    const load=async path=>{
      $('#browsemsg').textContent='';
      let r;try{r=await post('/api/browse-folder',{path:path||''});}catch(e){$('#browsemsg').textContent='Could not read that folder.';return;}
      if(!r||!r.path){$('#browsemsg').textContent=(r&&r.error)||'Could not read that folder.';return;}
      cur=r;
      $('#browsepath').textContent=r.path;
      $('#browseup').disabled=!r.parent;
      $('#browseuse').textContent=r.vault?'Use this vault':'Use this folder';
      $('#browselist').innerHTML=r.folders.length?r.folders.map(f=>'<button type="button" class="browseitem" role="option" data-name="'+esc(f.name)+'">'+FOLDERICON+'<span class="t">'+esc(f.name)+'</span>'+(f.vault?'<span class="pill ok">Obsidian vault</span>':'')+'</button>').join('')
        :'<div class="muted browseempty">'+(r.error?esc(r.error):'No folders in here.')+'</div>';
      $('#browselist').scrollTop=0;
      $('#browsemsg').textContent=r.more?'Showing the first '+r.folders.length+' folders.':(r.vault?'This folder is an Obsidian vault.':'');
    };
    const onClick=e=>{
      if(e.target.closest('[data-browsecancel]')){finish(null);return;}
      if(e.target.closest('#browseuse')){if(cur)finish(cur.path);return;}
      if(e.target.closest('#browseup')){if(cur&&cur.parent)load(cur.parent);return;}
      if(e.target.closest('#browsehome')){if(cur)load(cur.home);return;}
      const it=e.target.closest('.browseitem');
      if(it&&cur)load(cur.path.replace(/[\\/]+$/,'')+cur.sep+it.dataset.name);
    };
    const onClose=()=>finish(null);
    dlg.addEventListener('click',onClick);dlg.addEventListener('close',onClose);
    $('#browselist').innerHTML='';$('#browsepath').textContent='';
    dlg.showModal();load(start);
  });
}
// working folder
$('#folderbrowse').onclick=async()=>{const v=await browseFolder($('#folderinput').value.trim(),'Choose the working folder');if(v){$('#folderinput').value=v;$('#folderinput').focus();}};
$('#folderbtn').onclick=()=>{$('#folderinput').value=S.workdir||S.home;$('#dfolder').showModal();$('#folderinput').select();};
$('#folderform').onsubmit=e=>{e.preventDefault();const v=$('#folderinput').value.trim();$('#dfolder').close();
  if(!v||v===S.workdir)return;S.workdir=v;store.set('workdir',v);
  if(CHAT.items.length){CHAT.sessions=fresh();CHAT.sent=fresh();carry=null;note('Folder changed to '+v+' — assistants start fresh conversations.');}
  paint();};

// ===== composer =====
const msg=$('#msg');
function grow(){msg.style.height='auto';msg.style.height=Math.min(msg.scrollHeight,240)+'px';}
msg.addEventListener('input',grow);
msg.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#f').requestSubmit();}});
$('#f').onsubmit=e=>{e.preventDefault();if(S.busy||S.none)return;const t=msg.value.trim();if(!t&&!FILES.length)return;msg.value='';grow();send(t);};
$('#send').addEventListener('click',e=>{if(!S.busy)return;e.preventDefault();stopTurns();});
let FILES=[];
function canTake(b, kind){
  if(b==='claude'||b==='cursor'||b==='codex') return true;
  if(b==='chatgpt') return kind==='text'||kind==='image'||kind==='pdf';
  if(b==='ollama') return kind==='text'||kind==='image';
  return false;
}
function clipWarning(kind){
  const who=activeChoice().filter(b=>!canTake(b, kind));
  if(!who.length) return '';
  if(kind==='image'&&who.every(b=>b==='ollama')) return 'Ollama only sees this image if the model supports vision';
  return who.map(b=>NAMES[b]).join(', ')+' can’t take this file';
}
function paintClips(){
  $('#clips').innerHTML=FILES.map((f,i)=>{const w=clipWarning(f.kind);
    return '<span class="clip'+(w?' warn':'')+'" title="'+esc(w||f.name)+'"><span class="nm">'+esc(f.name)+(w?' · '+esc(w):'')+'</span><button type="button" data-i="'+i+'" title="Remove">×</button></span>';
  }).join('');
}
function readFile(file){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(String(r.result).split(',')[1]||'');r.onerror=()=>rej(r.error);r.readAsDataURL(file);});}
async function addFiles(list){
  for(const file of list){
    if(FILES.length>=8){note('You can attach up to 8 files.');break;}
    if(file.size>8*1024*1024){note(file.name+' is over 8 MB.');continue;}
    let data;try{data=await readFile(file);}catch(e){note('Could not read '+file.name+'.');continue;}
    const meta=await post('/api/attach',{name:file.name,type:file.type||'',data});
    if(meta.error) note(meta.error); else FILES.push(meta);
  }
  paintClips();
}
$('#attach').onclick=()=>$('#filepick').click();
$('#filepick').onchange=()=>{if($('#filepick').files)addFiles($('#filepick').files);$('#filepick').value='';};
$('#clips').onclick=e=>{const b=e.target.closest('button');if(!b)return;FILES.splice(+b.dataset.i,1);paintClips();};
$('#f').addEventListener('dragover',e=>{e.preventDefault();$('#f').classList.add('drop');});
$('#f').addEventListener('dragleave',()=>$('#f').classList.remove('drop'));
$('#f').addEventListener('drop',e=>{e.preventDefault();$('#f').classList.remove('drop');if(e.dataTransfer&&e.dataTransfer.files.length)addFiles(e.dataTransfer.files);});
msg.addEventListener('paste',e=>{const files=[...(e.clipboardData&&e.clipboardData.files||[])];if(files.length){addFiles(files);}});
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
function turnParts(b, mode, model, r, partial, group, adopt){
  if(adopt!==false){
    if(r.session_id)CHAT.sessions[b]=r.session_id;
    if(r.context_version!==undefined)CHAT.ctx[b]=r.context_version;
  }
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
  if(r.notes_used&&r.notes_used.length){if(adopt!==false)CHAT.sent[b]=(CHAT.sent[b]||[]).concat(r.notes_used.map(n=>n.path));
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
async function streamInto(pend, b, text, mode, model, fileIds, opts){
  opts=opts||{};
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
    const payload={backend:b,message:text,session_id:opts.handoff?null:CHAT.sessions[b],mode,context_version:CHAT.ctx[b],
      sent_notes:CHAT.sent[b]||[],model,workdir:S.workdir||S.home,turn_id:turnId,attachments:fileIds||[]};
    if(opts.handoff){payload.handoff=true;payload.history=opts.history||[];}
    const resp=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},signal:ac.signal,
      body:JSON.stringify(payload)});
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

async function send(text, opts){
  opts=opts||{};
  const rm=text.match(/^\/remember\s+([\s\S]+)/i);
  if(rm){const r=await post('/api/memory',{action:'add',text:rm[1]});
    if(r.error)note(r.error);else{const m=r.memories.find(x=>x.text.toLowerCase()===rm[1].trim().replace(/\s+/g,' ').toLowerCase())||r.memories[r.memories.length-1];
      addItem({r:'note',t:'🧠 Saved to memory: '+m.text,memId:m.id});refreshMemCount();}return;}
  let targets=opts.backend?[opts.backend]:activeChoice().filter(ready);
  if(!targets.length) targets=[S.backend];
  const files=opts.handoff?[]:FILES.splice(0,FILES.length);
  if(!opts.handoff) paintClips();
  const ids=files.map(f=>f.id), shown=files.map(f=>({name:f.name,kind:f.kind}));
  if(targets.length>1) return sendMany(text, targets, ids, shown);
  const b=targets[0], mode=modeFor(b, S.mode), model=modelFor(b);
  CHAT.workdir=S.workdir||S.home; // each chat keeps its own folder (sessions belong to it)
  const prior=Array.isArray(opts.history)?opts.history:historyPayload(CHAT.items);
  addItem({r:'you',t:text,b,files:shown});
  const pass=!!opts.handoff || (carry===b && !CHAT.sessions[b] && prior.length>0);
  if(pass) CHAT.sessions[b]=null; // fresh CLI session; the transcript rides in the prompt
  const pend=pendingBubble(b, mode);
  thread.appendChild(pend);scrollDown();
  S.busy=true;paint();
  const {r,live}=await streamInto(pend, b, text, mode, model, ids, pass?{handoff:true,history:prior}:null);
  pend.remove();S.busy=false;
  finishTurn(b, mode, model, r, live);
  if(pass) carry=CHAT.sessions[b]?null:b;
  saveChat();paint();msg.focus();}
async function sendMany(text, targets, ids, shown){
  const mode=S.mode;
  CHAT.workdir=S.workdir||S.home;
  addItem({r:'you',t:text,b:targets[0],files:shown||[]});
  await Promise.all(targets.map(loadModelsFor));
  const wrap=document.createElement('div');wrap.className='compare';
  const cols=targets.map(b=>{
    const m=modeFor(b, mode), pend=pendingBubble(b, m);
    wrap.appendChild(pend);
    return {b, mode:m, model:modelFor(b), pend};
  });
  thread.appendChild(wrap);$('#main').classList.add('comparing');scrollDown();
  S.busy=true;paint();
  const results=await Promise.all(cols.map(async c=>({c, ...await streamInto(c.pend, c.b, text, c.mode, c.model, ids)})));
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

// ===== handoff: switch assistant and carry the conversation =====
function historyPayload(items){
  const src=Array.isArray(items)?items:CHAT.items;
  const out=[];
  src.forEach(i=>{
    if(!i||i.temp||(i.r!=='you'&&i.r!=='bot'&&i.r!=='err')) return;
    const t=String(i.t||'').trim();
    const files=(i.files||[]).filter(f=>f&&f.name).map(f=>({name:String(f.name).slice(0,120),kind:f.kind||''}));
    if(!t&&!files.length) return;
    out.push({r:i.r,t:t.slice(0,8000),b:i.b||'',model:i.model||'',files});
  });
  return out.length>200?out.slice(-200):out;
}
function clipQuote(t){t=String(t||'').trim();return t.length>1500?t.slice(0,1500).replace(/\s+\S*$/,'')+'…':t;}
function closeAskMenus(){
  document.querySelectorAll('.askpop').forEach(p=>{p.hidden=true;});
  document.querySelectorAll('.actions.on').forEach(a=>a.classList.remove('on'));
}
function openAskMenu(row, item){
  const pop=row&&row.querySelector('.askpop'); if(!pop||!item) return;
  const was=pop.hidden;
  closeAskMenus();
  if(!was) return;
  const skip=item.r==='you'?S.backend:item.b;
  const buttons=BACKENDS.filter(b=>b!==skip).map(b=>{
    const ok=!ACCLOADED||ready(b);
    return '<button type="button" data-askto="'+b+'"'+(ok?'':' disabled')+'>'+esc(NAMES[b])+'</button>';
  }).join('');
  pop.innerHTML=buttons||'<span class="muted">No other assistant</span>';
  pop.hidden=false;
  const actions=row.querySelector('.actions'); if(actions) actions.classList.add('on');
}
function lastExchange(){
  let you=-1;
  for(let i=CHAT.items.length-1;i>=0;i--){if(CHAT.items[i].r==='you'&&!CHAT.items[i].temp){you=i;break;}}
  if(you<0) return null;
  const answers=[];
  for(let i=you+1;i<CHAT.items.length;i++){
    const it=CHAT.items[i];
    if(!it||it.temp) continue;
    if(it.r==='bot'||it.r==='err') answers.push(it);
    else if(it.r==='you') break;
  }
  return {you, question:CHAT.items[you].t||'', answers};
}
function handoffMenu(){
  const others=BACKENDS.filter(b=>b!==S.backend);
  const ex=lastExchange();
  const answered=new Set((ex&&ex.answers||[]).map(a=>a.b));
  const row=(b, kind)=>{
    const ok=ready(b);
    const enabled=kind==='compare'?!!(ok&&ex&&ex.answers.length&&!answered.has(b)):ok;
    const sub=kind==='compare'
      ?(ex&&ex.answers.length?(answered.has(b)?'Already answered this':'Same last question, side by side'):'Ask something first')
      :(ok?'Switch and include this chat':'Not connected');
    return '<button type="button" class="mi" data-'+kind+'="'+b+'"'+(enabled?'':' disabled')+'><span class="bdot" style="background:'+color(b)+'"></span><span class="mt"><b>'+NAMES[b]+'</b><span>'+sub+'</span></span></button>';
  };
  return '<div class="mh">Hand off to…</div>'+others.map(b=>row(b,'handoff')).join('')
    +'<div class="mh">Compare</div>'+others.map(b=>row(b,'compare')).join('');
}
function ensureHandoff(){
  if($('#p-handoff')) return;
  const el=document.createElement('div');
  el.className='pick down'; el.id='p-handoff'; el.hidden=true;
  el.innerHTML='<button type="button" class="pickbtn" title="Hand off this chat to another assistant, or compare the last answer"><span class="lbl">Hand off to…</span>'+ICON.chev+'</button><div class="menu"></div>';
  const title=$('#title'); if(title) title.insertAdjacentElement('afterend', el);
  el.addEventListener('click', e=>{
    const t=e.target.closest('.mi');
    if(!t){const menu=el.querySelector('.menu'); if(menu) menu.innerHTML=handoffMenu(); return;}
    el.classList.remove('open');
    if(t.disabled) return;
    if(t.dataset.handoff) handOff(t.dataset.handoff);
    else if(t.dataset.compare) compareWith(t.dataset.compare);
  });
}
async function handOff(b){
  if(S.busy){note('Stop the current reply before handing off.');return;}
  if(!ready(b)){note(NAMES[b]+' is not connected.');return;}
  setBackend(b,true);
  if(!CHAT.items.some(i=>!i.temp&&(i.r==='you'||i.r==='bot'||i.r==='err'))){note('Now chatting with '+NAMES[b]+'.');return;}
  CHAT.sessions[b]=null; carry=b;
  await send('Hand off to '+NAMES[b]+'. Continue this conversation from the earlier turns.',
    {backend:b, handoff:true, history:historyPayload()});
}
async function askOther(item, b){
  if(!item) return;
  if(S.busy){note('Stop the current reply before asking another assistant.');return;}
  if(!ready(b)){note(NAMES[b]+' is not connected.');return;}
  setBackend(b,true);
  CHAT.sessions[b]=null; carry=b;
  const who=item.r==='you'?'You':(NAMES[item.b]||'Assistant');
  const quote=clipQuote(item.t||'');
  const text=item.r==='you'
    ? 'Ask '+NAMES[b]+' about this:\n\n'+quote
    : 'Ask '+NAMES[b]+' about this message from '+who+':\n\n'+quote;
  await send(text, {backend:b, handoff:true, history:historyPayload()});
}
async function compareWith(b){
  if(S.busy){note('Stop the current reply before comparing.');return;}
  if(!ready(b)){note(NAMES[b]+' is not connected.');return;}
  const ex=lastExchange();
  if(!ex||!ex.answers.length){note('Ask something and wait for a reply, then you can compare.');return;}
  if(ex.answers.some(a=>a.b===b)){note(NAMES[b]+' already answered this.');return;}
  const group=ex.answers[0].group||newTurnId().slice(0,12);
  ex.answers.forEach(a=>{a.group=group;});
  const prior=historyPayload(CHAT.items.slice(0, ex.you));
  let question=String(ex.question||'').trim();
  if(!question){
    const names=(CHAT.items[ex.you].files||[]).map(f=>f&&f.name).filter(Boolean);
    question=names.length?'Please look at the attached files: '+names.join(', '):'Please answer the last question.';
  }
  const mode=modeFor(b, S.mode), model=modelFor(b);
  renderThread();
  const wrap=[...thread.querySelectorAll('.compare')].find(n=>{const row=n.querySelector('.m-bot,.m-you');return row&&row.item&&row.item.group===group;});
  const pend=pendingBubble(b, mode);
  (wrap||thread).appendChild(pend);
  $('#main').classList.add('comparing'); scrollDown();
  S.busy=true; paint();
  const {r,live}=await streamInto(pend, b, question, mode, model, [], {handoff:true, history:prior});
  delete r.session_id; // side-by-side answer must not replace that assistant's real session
  const parts=turnParts(b, mode, model, r, live, group, false);
  const after=ex.answers[ex.answers.length-1];
  let at=CHAT.items.indexOf(after);
  at=at<0?CHAT.items.length:at+1;
  parts.bots.forEach(item=>{CHAT.items.splice(at,0,item);at++;});
  parts.rest.forEach(item=>CHAT.items.push(item));
  renderThread(); scrollDown(); saveChat();
  if(parts.memories) refreshMemCount();
  S.busy=false; paint(); msg.focus();
}

// ===== settings & memory =====
function renderSettings(p){
  $('#use_memory').checked=p.settings.use_memory;$('#auto_memory').checked=p.settings.auto_memory;
  $('#memory_in_vault').checked=p.settings.memory_in_vault;$('#vault_search').checked=p.settings.vault_search;
  const notes=p.notes||{};
  const mode=notes.mode||p.settings.notes_storage||'off';
  document.querySelectorAll('input[name="notes"]').forEach(el=>{el.checked=el.value===mode;});
  document.querySelectorAll('.notecard').forEach(card=>{
    const input=card.querySelector('input[name="notes"]');
    card.classList.toggle('on', !!(input&&input.checked));
  });
  $('#vaultopts').hidden=mode==='off';$('#vaulthint').hidden=mode!=='off';
  if(document.activeElement!==$('#sync_folder'))$('#sync_folder').value=p.settings.sync_folder||'Chatbox';
  if(document.activeElement!==$('#vaultpickpath'))$('#vaultpickpath').value=p.settings.vault_path||'';
  if(!$('#choosevault').disabled)$('#choosevault').textContent=p.settings.vault_path?'Change…':'Choose folder…';
  $('#choosevault').classList.toggle('btn',!p.settings.vault_path);$('#choosevault').classList.toggle('ghost',!!p.settings.vault_path);
  if(p.vault)VAULT=p.vault; else if(mode!=='chatbox') VAULT=null;
  $('#obsidianopts').hidden=mode!=='obsidian';
  $('#chatboxopts').hidden=mode!=='chatbox';
  $('#obsdownload').hidden=!!notes.installed;
  $('#noteslocalpath').textContent=notes.local_path||'';
  const adv=$('#notesadvanced');
  if(mode==='off') adv.hidden=true;
  else{
    adv.hidden=false;
    const host=mode==='chatbox'?$('#chatboxopts'):$('#obsidianopts');
    if(host&&adv.parentElement!==host) host.appendChild(adv);
  }
  const banner=$('#notebanner'); if(banner) banner.hidden=!notes.banner;
  PROJECTS=p.projects||[];renderProject();
  const status=$('#notesstatus');
  status.textContent=notes.status||'';
  status.className='notesstatus'+(notes.fallback?' warn':(mode==='off'?' muted':''));
  if(document.activeElement!==$('#instructions')){$('#instructions').value=p.settings.instructions||'';instSaved=$('#instructions').value;}
  $('#auto_update').checked=p.settings.auto_update!==false;
  $('#auto_install_updates').checked=!!p.settings.auto_install_updates;
  $('#mempath').textContent=p.path;$('#memtotal').textContent=p.memories.length?p.memories.length+' saved':'';$('#memclear').disabled=!p.memories.length;
  $('#memlist').innerHTML=p.memories.length?p.memories.map(m=>'<div class="mem" data-id="'+m.id+'"><input class="field" value="'+esc(m.text)+'" maxlength="500"><span class="src">'+(m.source==='assistant'?'by assistant':m.source==='obsidian'?'in Obsidian':'by you')+(m.created?' · '+esc(m.created):'')+'</span><button class="icon" type="button" title="Delete">'+ICON.trash+'</button></div>').join('')
    :'<div class="muted">No memories yet. Add one above, or just tell an assistant something about you.</div>';
  $('#memcount').textContent=p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';
  applyNotes(p);}
async function memAct(body){const r=await post(body.settings?'/api/settings':'/api/memory',body);if(r.error)await askAlert(r.error);renderSettings(r);CHAT.ctx=fresh();return r;}
function refreshMemCount(){fetch('/api/settings').then(r=>r.json()).then(p=>{$('#memcount').textContent=p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';if($('#set').open)renderSettings(p);});}
function showSetTab(name){
  const tabs=[...document.querySelectorAll('.set-nav [data-tab]')];
  if(!tabs.some(t=>t.dataset.tab===name)) name=tabs[0].dataset.tab;
  tabs.forEach(t=>{const on=t.dataset.tab===name;t.setAttribute('aria-selected',on?'true':'false');t.tabIndex=on?0:-1;});
  document.querySelectorAll('.pane[data-pane]').forEach(p=>{p.hidden=p.dataset.pane!==name;});
  $('.set-panes').scrollTop=0;
  try{localStorage.setItem('chatbox.settab',name);}catch(e){}
}
async function openSettings(tab){
  let last='';try{last=localStorage.getItem('chatbox.settab')||'';}catch(e){}
  showSetTab(tab||last||'memory');
  if(!$('#set').open)$('#set').showModal();
  renderSettings(await fetch('/api/settings').then(r=>r.json()));
}
$('.set-nav').addEventListener('click',e=>{const t=e.target.closest('[data-tab]');if(t)showSetTab(t.dataset.tab);});
$('.set-nav').addEventListener('keydown',e=>{
  if(!['ArrowDown','ArrowUp','ArrowRight','ArrowLeft'].includes(e.key))return;
  const tabs=[...document.querySelectorAll('.set-nav [data-tab]')];
  const i=tabs.indexOf(document.activeElement);if(i<0)return;
  e.preventDefault();
  const next=tabs[(i+(e.key==='ArrowDown'||e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
  showSetTab(next.dataset.tab);next.focus();
});
$('#setbtn').onclick=()=>openSettings();
$('#acc2notes').onclick=e=>{e.preventDefault();$('#acc').close();openSettings('notes');};
for(const k of ['use_memory','auto_memory','memory_in_vault','vault_search','auto_update','auto_install_updates'])$('#'+k).onchange=e=>memAct({settings:{[k]:e.target.checked}});
function showNotesError(text){
  const el=$('#notesstatus');
  el.textContent=text;
  el.className='notesstatus err';
  if($('#set').open) showSetTab('notes');
}
function showVaultMsg(text, kind){
  const msg=$('#vaultmsg');
  if(!msg) return;
  msg.textContent=text||'';
  msg.className='vaultmsg'+(kind?(' '+kind):'');
}
let vaultSaveGen=0;
async function saveTypedVault(path){
  const btn=$('#vaultsave');
  const gen=++vaultSaveGen;
  if(btn){btn.disabled=true;btn.textContent='Saving…';}
  showVaultMsg('');
  try{
    const cur=await fetch('/api/settings').then(r=>r.json()).catch(()=>({}));
    const from=(cur.notes&&cur.notes.mode)||'';
    let copy=false;
    if(from==='chatbox') copy=await askConfirm('Copy the notes Chatbox already saved into this vault? Notes that are already there are left as they are.');
    const r=await post('/api/notes',{mode:'obsidian',path,copy});
    if(r.error){showVaultMsg(r.error,'err');return r;}
    renderSettings(r);
    showVaultMsg(r.notice||('Saved: notes go to '+(r.vault_name||'this vault')),'ok');
    if(btn) btn.textContent='Saved';
    setTimeout(()=>{if(gen===vaultSaveGen&&btn&&btn.textContent==='Saved') btn.textContent='Save';},1600);
    return r;
  }catch(e){
    showVaultMsg('Could not save the vault.','err');
    return {error:String(e)};
  }finally{
    if(btn){btn.disabled=false;if(btn.textContent==='Saving…') btn.textContent='Save';}
  }
}
document.querySelectorAll('input[name="notes"]').forEach(el=>el.onchange=async()=>{
  if(!el.checked)return;
  if(el.value==='obsidian'){
    const cur=await fetch('/api/settings').then(r=>r.json()).catch(()=>({}));
    const from=(cur.notes&&cur.notes.mode)||'';
    const path=(cur.settings&&cur.settings.vault_path)||'';
    let copy=false;
    if(from==='chatbox'&&path) copy=await askConfirm('Copy the notes Chatbox already saved into this vault? Notes that are already there are left as they are.');
    const r=await post('/api/notes',{mode:'obsidian',copy});
    if(r.error) showNotesError(r.error);
    else {renderSettings(r); if($('#set').open) $('.set-panes').scrollTop=0;}
    return;
  }
  const r=await post('/api/notes',{mode:el.value});
  if(r.error) showNotesError(r.error);
  else {renderSettings(r); if($('#set').open) $('.set-panes').scrollTop=0;}
});
$('#syncform').onsubmit=async e=>{e.preventDefault();await memAct({settings:{sync_folder:$('#sync_folder').value}});};
$('#set').addEventListener('click',e=>{const a=e.target.closest('a[data-href]');if(a){e.preventDefault();post('/api/open',{url:a.dataset.href});}});
$('#vaultpick').onsubmit=async e=>{
  e.preventDefault();
  const v=$('#vaultpickpath').value.trim();
  if(!v){showVaultMsg('Type a folder path, or click Change… to pick one.','err');return;}
  await saveTypedVault(v);
};
$('#choosevault').onclick=async()=>{
  const btn=$('#choosevault');
  btn.disabled=true; btn.textContent='Choosing…';
  try{
    const picked=await post('/api/pick-folder',{});
    if(picked.cancelled) return;
    if(picked.browse){ // no system dialog here: use Chatbox's own folder list
      const chosen=await browseFolder($('#vaultpickpath').value.trim(),'Choose your vault folder');
      if(!chosen) return;
      $('#vaultpickpath').value=chosen;
      await saveTypedVault(chosen);
      return;
    }
    if(picked.error){showVaultMsg(picked.error,'err');return;}
    if(!picked.path){showVaultMsg('No folder was chosen.','err');return;}
    $('#vaultpickpath').value=picked.path;
    await saveTypedVault(picked.path);
  }catch(e){
    showNotesError('Could not open the folder picker.');
  }finally{
    btn.disabled=false; btn.textContent=$('#vaultpickpath').value.trim()?'Change…':'Choose folder…';
  }
};
$('#p-project').addEventListener('click',async e=>{
  const t=e.target.closest('.mi');if(!t)return;
  $('#p-project').classList.remove('open');
  let value=t.dataset.project||'';
  if(t.hasAttribute('data-newproject')){
    const name=await askPrompt('Project name');
    if(!name||!name.trim())return;
    value=name.trim();
  }
  if(value===(CHAT.project||'')&&CHAT.project_lock)return;
  CHAT.project=value;CHAT.project_lock=true;saveChat();renderProject();
  if(CHAT.items.length)note(value?'This chat is now filed under '+value+'.':'This chat is no longer in a project.');
});
$('#notebanner').addEventListener('click',e=>{const a=e.target.closest('a[data-href]');if(a){e.preventDefault();post('/api/open',{url:a.dataset.href});}});
$('#opennotes').onclick=async()=>{
  const r=await post('/api/notes',{action:'open'});
  if(r.error) showNotesError(r.error);
};
$('#openobsidian').onclick=async()=>{
  const r=await post('/api/notes',{action:'obsidian'});
  if(r.error) await askAlert(r.error);
};
$('#disconnectnotes').onclick=async()=>{
  const choice=await ask({
    title:'Disconnect Obsidian?',
    message:'Notes already in the vault stay there. Where should new notes go?',
    buttons:[
      {id:'chatbox', label:'Keep notes inside Chatbox', primary:true},
      {id:'off', label:'Turn notes off'},
      {id:'cancel', label:'Cancel', cancel:true},
    ],
  });
  if(!choice||choice.id==='cancel') return;
  const r=await post('/api/notes',{mode:choice.id});
  if(r.error){await askAlert(r.error);return;}
  renderSettings(r);
  if($('#set').open) $('.set-panes').scrollTop=0;
};
let instSaved=null;
async function saveInstructions(){
  const v=$('#instructions').value;
  if(v===instSaved)return;
  instSaved=v;
  await memAct({settings:{instructions:v}});
  $('#instmsg').textContent='Saved — new chats will use it.';setTimeout(()=>$('#instmsg').textContent='',3000);
}
$('#instsave').onclick=()=>{instSaved=null;saveInstructions();};
$('#instructions').addEventListener('change',saveInstructions);
$('#memadd').onsubmit=async e=>{e.preventDefault();const t=$('#memtext').value.trim();if(!t)return;const r=await memAct({action:'add',text:t});if(!r.error)$('#memtext').value='';};
$('#memlist').addEventListener('change',e=>{const row=e.target.closest('.mem');if(row)memAct({action:'edit',id:row.dataset.id,text:e.target.value});});
$('#memlist').addEventListener('click',e=>{const row=e.target.closest('.mem');if(row&&e.target.closest('button'))memAct({action:'delete',id:row.dataset.id});});
$('#memclear').onclick=async()=>{if(await askConfirm('Delete all memories? This cannot be undone.'))memAct({action:'clear'});};
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());

// ===== notes card (one place) and manual save =====
let VAULT=null;
function applyNotes(p){
  if(!p) return;
  VAULT=p.vault||null;
  PROJECTS=p.projects||[];
  const card=(p.notes&&p.notes.card)||{};
  const title=$('#notescardtitle');
  if(title&&card.title) title.textContent=card.title;
  const disc=$('#disconnectnotes');
  if(disc) disc.hidden=!card.disconnect;
  const obs=$('#openobsidian');
  if(obs) obs.hidden=!card.open_obsidian;
  const btn=$('#savebtn');
  if(btn) btn.hidden=!card.show_save;
  if(p.memories) $('#memcount').textContent=p.memories.length?p.memories.length+(p.memories.length===1?' memory':' memories'):'';
  renderProject();
}
$('#savebtn').onclick=async()=>{
  const msgs=CHAT.items.filter(i=>i.r==='you'||i.r==='bot').map(i=>({role:i.r==='you'?'you':'bot',who:i.r==='you'?'You':NAMES[i.b],text:i.t}));
  if(!msgs.length){note('Nothing to save yet.');return;}
  if(!CHAT.id) CHAT.id=[...crypto.getRandomValues(new Uint8Array(6))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const r=await post('/api/obsidian',{action:'save_chat',id:CHAT.id,messages:msgs,backend:NAMES[S.backend],workdir:CHAT.workdir||S.workdir||'',project:CHAT.project||'',project_lock:!!CHAT.project_lock});
  if(r.settings||r.notes) applyNotes(r);
  addItem({r:'note',t:r.error?r.error:'📓 Saved: '+r.saved});
};

// ===== sign-in status (cheap /api/auth-status check) =====
let AUTHSTATUS={backends:[]},authJobSeen=false;
const AUTH_STATE={ready:1,signed_out:1,missing:1,unknown:1};
function authOf(id){return (AUTHSTATUS.backends||[]).find(a=>a.id===id);}
function renderAuth(){
  const box=$('#authrow');if(!box)return;
  const rows=AUTHSTATUS.backends||[];
  if(!rows.length){box.innerHTML='<div class="authlabel">Sign-in</div><div class="authsub">Checking…</div>';return;}
  box.innerHTML='<div class="authlabel">Sign-in</div>'+rows.map(a=>{
    if(!AUTH_STATE[a.state])return '';
    let extra='';
    if(a.state==='signed_out'&&a.id!=='ollama'){
      const tip=a.sign_in_command||(a.id==='chatgpt'?'Continue with ChatGPT':'Sign in');
      extra='<button type="button" class="authsign" data-signin="'+esc(a.id)+'" title="'+esc(tip)+'" aria-label="Sign in to '+esc(a.name)+'">Sign in</button>';}
    else if(a.state==='unknown')extra='<span class="authnote">Couldn\u2019t check</span>';
    if(a.state==='missing')extra='<button type="button" class="authsign" data-setup title="'+esc(a.install_hint||'Open Accounts')+'" aria-label="Set up '+esc(a.name)+'">Set up</button>';
    const hint='';
    return '<div class="authblock" data-auth="'+esc(a.id)+'"><div class="authitem" title="'+esc(a.detail||'')+'"><span class="sdot '+a.state+'"></span><span class="nm">'+esc(a.name)+'</span>'+extra+'</div>'+hint+'</div>';
  }).join('');}
async function refreshAuth(fresh){
  let data;try{data=await fetch('/api/auth-status'+(fresh?'?fresh=1':'')).then(r=>r.json());}catch(e){return;}
  if(!data||!Array.isArray(data.backends))return;
  AUTHSTATUS=data;renderAuth();paint();}
async function startSignIn(id,btn){
  const known={cursor:1,claude:1,codex:1,chatgpt:1};
  if(!known[id])return;
  btn.disabled=true;
  let r;try{r=await post('/api/account',{service:id,action:'login'});}
  catch(e){r={error:'Could not start sign-in.'};}
  if(!r||r.error){
    btn.disabled=false;
    const row=authOf(id);
    const cmd=(row&&row.sign_in_command)||(id==='chatgpt'?'Continue with ChatGPT in Accounts':'');
    const block=btn.closest('.authblock');
    if(block&&cmd){
      let note=block.querySelector('.authcmd');
      if(!note){note=document.createElement('div');note.className='authcmd';block.appendChild(note);}
      note.textContent=cmd;}
    else if(r&&r.error)await askAlert(r.error);
    return;}
  openAccounts();}
$('#authrow').addEventListener('click',e=>{if(e.target.closest('[data-setup]')){openAccounts();return;}const btn=e.target.closest('[data-signin]');if(!btn||btn.disabled)return;startSignIn(btn.dataset.signin,btn);});

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
  const running=list.some(a=>a.job&&a.job.state==='running');
  if(running)authJobSeen=true;
  else if(authJobSeen){authJobSeen=false;refreshAuth(true);}
  const j=JSON.stringify(list);if(j!==accJSON){accJSON=j;renderAccounts(list);}
  clearTimeout(accTimer);
  if($('#acc').open)accTimer=setTimeout(()=>refreshAccounts(),list.some(a=>a.job&&a.job.state==='running')?1500:15000);
  return list;}
let ollamaTyped=null; // address being typed (survives the list refreshing)
const ACC_ABOUT={cursor:'Coding assistant from Cursor. Can read and change files in your folder.',
  claude:'Anthropic’s assistant. Can read and change files in your folder.',
  codex:'OpenAI’s coding assistant. Can read and change files in your folder.',
  chatgpt:'Plain chat with ChatGPT. No files or commands.',
  ollama:'Free models that run on your own computer. Plain chat: no files or commands.'};
const accOpen={}; // which "More options" are open (survives the list refreshing)
function accCard(a,st,need,primary,more,extra){
  const dot='<span class="bdot" style="background:'+color(a.id)+'"></span>';
  return '<div class="sec acct" data-acc="'+a.id+'"><div class="acct-h"><div class="acct-id">'
    +'<div class="sec-h">'+dot+'<b>'+esc(a.name)+'</b><span class="pill '+st[0]+'">'+st[1]+'</span></div>'
    +'<div class="muted">'+esc(ACC_ABOUT[a.id]||'')+'</div>'
    +(need?'<div class="acct-need">'+need+'</div>':'')+'</div>'
    +'<div class="acct-act">'+primary+'</div></div>'+(extra||'')
    +(more?'<details class="advanced acct-more" data-more="'+a.id+'"'+(accOpen[a.id]?' open':'')+'><summary>More options</summary>'+more+'</details>':'')
    +'</div>';}
const moreRow=(btn,text)=>'<div class="acct-opt">'+btn+'<span class="muted">'+text+'</span></div>';
function ollamaCard(a){
  const st=!a.installed?['bad','Not running']:a.models?['ok','Running']:['warn','No models yet'];
  const need=a.installed&&a.models?esc(a.account):'Free. No account needed.';
  let primary,extra='';
  if(!a.installed){primary='<button class="btn" data-act="open" data-url="https://ollama.com/download">Get Ollama</button><button class="ghost" data-act="ollamacheck">Check again</button>';
    extra='<div class="acct-step muted">Install Ollama and start it, then click “Check again”.</div>';}
  else if(!a.models){primary='<button class="ghost" data-act="ollamacheck">Check again</button>';
    extra='<div class="job"><div class="muted">Download a model in a terminal, then click “Check again”. For example:</div>'
      +'<div class="row"><code>ollama pull llama3.2</code><button class="ghost" data-act="copycode" data-code="ollama pull llama3.2">Copy</button></div></div>';}
  else primary='<button class="ghost" data-act="ollamacheck">Refresh models</button>';
  const more='<div class="muted">Change this only if Ollama runs somewhere else, such as another computer.</div>'
    +'<form class="row" id="ollamaform"><span class="muted">Address</span><input class="field" id="ollamaurl" value="'+esc(ollamaTyped??a.url)+'" placeholder="http://localhost:11434" style="flex:1;min-width:200px"><button class="ghost">Save</button></form>';
  return accCard(a,st,need,primary,more,extra);}
function renderAccounts(list){
  const on=list.filter(a=>a.id==='ollama'?(a.installed&&a.models):(a.installed&&a.logged_in&&!(a.id==='chatgpt'&&a.plan_blocked))).length;
  const sum=$('#accsummary');
  if(sum){sum.textContent=on?on+' of '+list.length+' connected':'Nothing connected yet. Connect one assistant to start chatting.';sum.className='acct-sum'+(on?'':' none');}
  $('#acclist').innerHTML=list.map(a=>{if(a.id==='ollama')return ollamaCard(a);
    const blocked=a.id==='chatgpt'&&a.plan_blocked;
    const st=!a.installed?['bad','Not installed']:blocked?['warn','Plan not available']:a.logged_in?['ok','Connected']:['bad','Not signed in'];
    const busy=a.job&&a.job.state==='running';const dis=busy?' disabled':'';
    let btns='',more='';
    const other=a.has_client?moreRow('<button class="ghost" data-act="login_new" data-s="chatgpt"'+dis+'>Use a different account</button>','Sign in to ChatGPT with another account.'):'';
    if(!a.installed)btns='<button class="btn" data-act="install" data-s="'+a.id+'"'+dis+'>Install</button>';
    else if(blocked){btns='<button class="btn" data-act="usecodex">Use Codex</button><button class="ghost" data-act="login" data-s="chatgpt"'+dis+'>Sign in again</button>';more+=other;}
    else if(!a.logged_in){
      if(a.id==='chatgpt'){btns='<button class="btn" data-act="login" data-s="chatgpt" data-oai-siwc-button'+dis+'>Continue with ChatGPT</button>';more+=other;}
      else{btns='<button class="btn" data-act="login" data-s="'+a.id+'"'+dis+'>Sign in</button>';
        if(a.id==='codex')more+=moreRow('<button class="ghost" data-act="login_browser" data-s="codex"'+dis+'>Sign in with browser redirect</button>','Older method. Try it if the one-time code sign-in does not work.');}}
    else btns='<button class="ghost" data-act="logout" data-s="'+a.id+'"'+dis+'>Sign out</button>';
    if(a.installed&&a.id==='codex'&&a.source)more+=moreRow('<button class="ghost" data-act="install" data-s="codex"'+dis+'>Install standalone Codex</button>','You are using the Codex '+esc(a.source)+'. OpenAI’s standalone Codex is newer and supports more models.');
    const need=a.logged_in&&a.account&&a.installed?'Signed in as '+esc(a.account):'Needs: '+esc(a.plan);
    let notice=blocked?'<div class="acct-step muted">'+esc(a.plan_message||'')+'</div>':'';
    let job='';
    if(a.job){const J=a.job,what={install:'Installing',login:'Signing in',logout:'Signing out'}[J.kind];
      const state=J.state==='running'?what+'…':J.state==='done'?{install:'Installed',login:'Sign-in finished',logout:'Signed out'}[J.kind]+' ✓':J.state==='cancelled'?'Cancelled':J.state==='blocked'?'ChatGPT plan usage is off':J.state==='timeout'?'Sign-in closed':'Something went wrong';
      job='<div class="job"><b>'+state+'</b>';
      if(J.state==='running'&&J.kind==='login'&&J.user_code){ // Codex device code
        job+='<div class="muted">1. A sign-in page should open in your browser. Sign in to your ChatGPT account.</div>'
          +'<div class="muted">2. Enter this one-time code there (it expires in 15 minutes):</div>'
          +'<div class="row"><code class="acct-code">'+esc(J.user_code)+'</code><button class="ghost" data-act="copycode" data-code="'+esc(J.user_code)+'">Copy code</button>'
          +(J.url?'<button class="ghost" data-act="open" data-url="'+esc(J.url)+'">Open sign-in page</button>':'')+'</div>'
          +'<div class="muted">This finishes by itself once you enter the code. If the page says device sign-in is off, turn on “device code authorization” in ChatGPT’s security settings, or use “Sign in with browser redirect” under More options.</div>';}
      else if(J.state==='running'&&J.kind==='login'){
        job+='<div class="muted">Your browser should open. Sign in there'+(a.id==='claude'?', then copy the code it shows and paste it below.':' and this finishes by itself.')+'</div>';
        if(J.url)job+='<div class="row"><button class="ghost" data-act="open" data-url="'+esc(J.url)+'">Open sign-in page</button></div>';
        if(a.id==='codex')job+='<div class="muted">Keep the ChatGPT app closed while signing in: it can cancel this sign-in.</div>';
        if(a.id==='chatgpt')job+='<div class="muted">If you close the browser, click Cancel. Chatbox will not keep asking.</div>';}
      if(J.state==='running')job+='<div class="muted">Keep Chatbox open until this finishes.</div>';
      if(J.last&&J.state!=='cancelled'&&J.state!=='blocked'&&!/^(Opening browser|If the browser)/.test(J.last))job+='<div class="muted">'+esc(J.last)+'</div>';
      if(J.asks_code)job+='<div class="row"><input class="field" id="code_'+a.id+'" placeholder="Paste the code from the browser" value="'+esc(codes[a.id]||'')+'"><button class="btn" data-act="input" data-s="'+a.id+'">Submit code</button></div>';
      if(J.output)job+='<details'+(J.state==='failed'?' open':'')+'><summary class="muted">Details</summary><pre>'+esc(J.output)+'</pre></details>';
      job+='<div class="row">'+(J.state==='running'?'<button class="ghost" data-act="cancel" data-s="'+a.id+'">Cancel</button>':'<button class="ghost" data-act="dismiss" data-s="'+a.id+'">OK</button>')+'</div></div>';}
    return accCard(a,st,need,btns,more,notice+job);}).join('');}
$('#acclist').addEventListener('toggle',e=>{const d=e.target.closest&&e.target.closest('[data-more]');if(d)accOpen[d.dataset.more]=d.open;},true);
$('#acclist').addEventListener('input',e=>{if(e.target.id==='ollamaurl')ollamaTyped=e.target.value;if(e.target.id&&e.target.id.startsWith('code_'))codes[e.target.id.slice(5)]=e.target.value;});
$('#acclist').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id&&e.target.id.startsWith('code_')){e.preventDefault();e.target.nextElementSibling.click();}});
function ollamaRefresh(){delete modelCache.ollama;if(S.backend==='ollama')loadModels();accJSON='';refreshAuth(true);return refreshAccounts(true);}
$('#acclist').addEventListener('submit',async e=>{if(e.target.id!=='ollamaform')return;e.preventDefault();
  const r=await post('/api/settings',{settings:{ollama_url:$('#ollamaurl').value.trim()}});if(r.error)await askAlert(r.error);
  ollamaTyped=null;ollamaRefresh();});
$('#acclist').addEventListener('click',async e=>{
  const t=e.target.closest('[data-act]');if(!t)return;const act=t.dataset.act,s=t.dataset.s,name=ACC[s]&&ACC[s].name;
  if(act==='usecodex'){S.backend='codex';store.set('backend','codex');if($('#acc').open)$('#acc').close();paint();
    if(!ready('codex'))note('Codex is not signed in on this computer yet. Open Accounts and sign in. Codex uses your ChatGPT subscription.');return;}
  if(act==='open'){post('/api/open',{url:t.dataset.url});return;}
  if(act==='copycode'){copy(t.dataset.code,t);return;}
  if(act==='ollamacheck'){t.disabled=true;ollamaRefresh();return;}
  if(act==='install'&&!await askConfirm('Download and run the official '+name+' installer?'))return;
  if(act==='logout'&&!await askConfirm('Sign out of '+name+' on this computer?'+(s==='codex'?'\n\nThis also signs out Codex in the ChatGPT app.':s==='chatgpt'?'\n\nThis disconnects Chatbox from your ChatGPT plan here. You can also remove Chatbox in ChatGPT settings.':'')))return;
  const body={service:s,action:act};
  if(act==='input'){body.text=codes[s]||'';if(!body.text.trim())return;codes[s]='';}
  t.disabled=true;const r=await post('/api/account',body);if(r.error)await askAlert(r.error);
  if(act==='dismiss'){for(const k in modelCache)delete modelCache[k];loadModels();}
  accJSON='';refreshAccounts(act==='dismiss');});
function openAccounts(){if(!$('#acc').open)$('#acc').showModal();accJSON='';refreshAccounts(true);fetch('/api/settings').then(r=>r.json()).then(applyNotes);}
async function refreshUpdate(){
  let u;try{u=await fetch('/api/update').then(r=>r.json());}catch(e){return;}
  const bar=$('#updatebar');if(!bar)return;
  if(!u.available){bar.hidden=true;if(u.checking)setTimeout(refreshUpdate,3000);return;}
  bar.hidden=false;
  $('#updatetext').textContent='Update available';
  const btn=$('#updatebtn');
  btn.hidden=false;
  btn.textContent=u.can_apply?'Update now':(u.download?'Download update':(u.url?'View release':'Update now'));
  $('#updatemsg').textContent=u.message||'';
  if(u.checking)setTimeout(refreshUpdate,3000);
}
async function applyUpdate(msg, btn){
  const cur=await fetch('/api/update').then(r=>r.json()).catch(()=>null);
  if(cur&&cur.url&&!cur.can_apply){post('/api/open',{url:cur.url});return;}
  if(btn) btn.disabled=true;
  if(msg) msg.textContent='Updating…';
  const r=await post('/api/update',{action:'apply'});
  if(r.error){if(msg) msg.textContent=r.error; if(btn) btn.disabled=false; return;}
  if(msg) msg.textContent=r.restarting?'Updated. Restarting…':(r.error||'');
}
$('#updatebtn').onclick=()=>applyUpdate($('#updatemsg'), $('#updatebtn'));
$('#checknow').onclick=async()=>{
  const btn=$('#checknow'), msg=$('#checkmsg'), go=$('#setupdatenow');
  btn.disabled=true;
  msg.textContent='Checking…';
  go.hidden=true;
  let r;
  try{r=await post('/api/update',{action:'now'});}
  catch(e){msg.textContent='Could not check for an update.'; btn.disabled=false; return;}
  btn.disabled=false;
  if(r.available){
    msg.textContent=r.summary||r.message||'Update available';
    go.hidden=false;
    go.textContent=r.can_apply?'Update now':(r.download?'Download update':'Update now');
  }else{
    msg.textContent=r.summary||r.error||'Could not check for an update.';
    go.hidden=true;
  }
  refreshUpdate();
};
$('#setupdatenow').onclick=()=>applyUpdate($('#checkmsg'), $('#setupdatenow'));
refreshUpdate();
setInterval(refreshUpdate,60000);
$('#accbtn').onclick=openAccounts;
$('#acc').addEventListener('close',()=>{clearTimeout(accTimer);refreshAccounts(true);refreshAuth(true);});


// ===== Usage =====
const UORDER=['claude','cursor','codex','chatgpt','ollama']; // fixed chart order (validated palette)
const fmtN=n=>n>=1e6?(n/1e6).toFixed(n>=1e7?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e4?0:1)+'k':String(Math.round(n));
const fmtS=s=>s>=90?Math.round(s/60)+'m':s>=10?Math.round(s)+'s':(Math.round(s*10)/10)+'s';
const fmt$=c=>'$'+(c>=100?c.toFixed(0):c>=1?c.toFixed(2):c.toFixed(3));
function usageMeta(u){return (u.tok?' · '+fmtN(u.tok)+' tokens':'')+(u.s?' · '+fmtS(u.s):'')+(typeof u.cost==='number'?' · '+fmt$(u.cost):'');}
const U={days:+(store.get('u_days')||30),metric:store.get('u_metric')||'messages',data:null};
async function loadUsage(){U.data=await fetch('/api/usage?days='+U.days).then(r=>r.json());renderUsage();}
function renderUsage(){const d=U.data;if(!d)return;
  document.querySelectorAll('#u-range button').forEach(b=>{const on=+b.dataset.d===U.days;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on);});
  document.querySelectorAll('#u-metric button').forEach(b=>{const on=b.dataset.k===U.metric;b.classList.toggle('on',on);b.setAttribute('aria-pressed',on);});
  const empty=!d.count;
  $('#u-empty').hidden=!empty;$('#u-body').hidden=empty;$('#u-clearrow').hidden=empty;
  $('#u-empty').querySelector('span').textContent=U.days?'No messages in the last '+U.days+' days. Try “All time”, or send a message and it shows up here.':'Send a message and your usage shows up here.';
  const anyCost=UORDER.some(b=>d.totals[b].has_cost);
  $('#u-costnote').hidden=!anyCost;
  if(empty)return;
  const used=UORDER.filter(b=>d.totals[b].messages>0);
  const all=used.reduce((a,b)=>{const t=d.totals[b];a.m+=t.messages;a.tok+=t.input+t.output;a.s+=t.seconds;a.err+=t.errors;return a;},{m:0,tok:0,s:0,err:0});
  const top=used.slice().sort((x,y)=>d.totals[y].messages-d.totals[x].messages)[0];
  const days=d.series.filter(x=>UORDER.some(b=>x[b].messages>0)).length;
  const stat=(v,l)=>'<div class="ustat"><b>'+v+'</b><span>'+l+'</span></div>';
  $('#u-sum').innerHTML=stat(fmtN(all.m),all.m===1?'message':'messages')
    +stat(days,days===1?'active day':'active days')
    +stat(fmtS(all.s/all.m),'average reply time')
    +stat('<span class="bdot" style="background:'+color(top)+'"></span>'+NAMES[top],'most used')
    +(all.err?stat(all.err,all.err===1?'failed reply':'failed replies'):'');
  $('#u-tiles').innerHTML=UORDER.map(b=>{const t=d.totals[b];
    const head='<div class="tn"><span class="bdot" style="background:'+color(b)+'"></span>'+NAMES[b]+'</div>';
    if(!t.messages)return '<div class="tile idle">'+head+'<div class="sm">Not used'+(U.days?' in this period':' yet')+'</div></div>';
    return '<div class="tile">'+head
      +'<div class="big">'+fmtN(t.messages)+'</div><div class="sm">'+(t.messages===1?'message':'messages')+(t.errors?' · '+t.errors+' failed':'')+'</div>'
      +'<div class="sm">'+fmtN(t.input+t.output)+' tokens · avg '+fmtS(t.seconds/t.messages)+'</div>'
      +(t.has_cost?'<div class="sm">≈ '+fmt$(t.cost)+' at API prices</div>':'')+'</div>';}).join('');
  $('#u-charttitle').textContent=(U.metric==='tokens'?'Tokens':'Messages')+' per day';
  $('#u-tokhelp').hidden=U.metric!=='tokens';
  $('#u-legend').innerHTML=used.map(b=>'<span><i style="background:'+color(b)+'"></i>'+NAMES[b]+'</span>').join('');
  drawChart(d.series,U.metric);
  const MODES=[['ask','Ask','questions only, nothing changed'],['plan','Plan','wrote a plan first'],['agent','Agent','edited files or ran commands']];
  const mt=MODES.reduce((a,m)=>a+(d.modes[m[0]]||0),0)||1;
  $('#u-modes').innerHTML=MODES.map(m=>{const n=d.modes[m[0]]||0,pc=Math.round(100*n/mt);
    return '<div class="umode"><span class="um-n"><b>'+m[1]+'</b><span class="muted">'+m[2]+'</span></span><span class="um-bar"><i style="width:'+pc+'%"></i></span><span class="um-v">'+n+' · '+pc+'%</span></div>';}).join('');
  $('#u-modelcount').textContent=d.models.length+(d.models.length===1?' model':' models');
  $('#u-models').innerHTML='<table class="utable"><thead><tr><th>Assistant</th><th>Model</th><th class="n">Messages</th><th class="n" title="Text sent to the assistant">Tokens sent</th><th class="n" title="Text the assistant wrote back">Tokens received</th><th class="n">Avg time</th>'+(anyCost?'<th class="n">Cost</th>':'')+'</tr></thead><tbody>'
    +d.models.map(m=>'<tr><td><span class="row" style="gap:6px;flex-wrap:nowrap"><span class="bdot" style="background:'+color(m.backend)+'"></span>'+NAMES[m.backend]+'</span></td><td>'+esc(m.model)+'</td><td class="n">'+m.messages+'</td><td class="n">'+fmtN(m.input)+'</td><td class="n">'+fmtN(m.output)+'</td><td class="n">'+fmtS(m.seconds/m.messages)+'</td>'+(anyCost?'<td class="n">'+(m.has_cost?fmt$(m.cost):'—')+'</td>':'')+'</tr>').join('')+'</tbody></table>';}
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
$('#u-note').addEventListener('click',e=>{const a=e.target.closest('a[data-href]');if(a){e.preventDefault();post('/api/open',{url:a.dataset.href});}});
$('#u-clear').onclick=async()=>{if(!await askConfirm('Clear all usage statistics? Chats and memories are not affected.'))return;await post('/api/usage',{action:'clear'});loadUsage();};
addEventListener('resize',()=>{if($('#usage').open)renderUsage();});

// ===== start =====
CHAT=newChatState();setEmpty();paint();
const h=new Date().getHours();$('#greet').textContent=h<5?'Up late? What can I help with?':h<12?'Good morning. What can I help with?':h<18?'Good afternoon. What can I help with?':'Good evening. What can I help with?';
function showVersion(s){
  const label=s&&s.version||'';
  const url=(s&&(s.version_url||s.url))||'';
  document.querySelectorAll('a.appver').forEach(a=>{
    a.hidden=!label;
    a.textContent=label;
    if(url)a.dataset.href=url;
    a.title=label?('Open the '+label+' release'):'';
  });
  const line=$('#aboutline');if(line)line.hidden=!label;
  if(label)document.title='Chatbox '+label;
}
document.addEventListener('click',e=>{
  const a=e.target.closest('a.appver');
  if(!a||a.hidden||!a.dataset.href)return;
  e.preventDefault();
  post('/api/open',{url:a.dataset.href});
});
fetch('/api/status').then(r=>r.json()).then(s=>{S.home=s.home;showVersion(s);paint();loadModels();});
renderAuth();
refreshAuth(false);
setInterval(()=>refreshAuth(false),60000);
fetch('/api/settings').then(r=>r.json()).then(applyNotes);
fetch('/api/chats').then(r=>r.json()).then(renderChatList);
async function refreshChats(){
  if(document.hidden||S.busy)return;
  let list;try{list=await fetch('/api/chats').then(r=>r.json());}catch(e){return;}
  renderChatList(list);
  if(!CHAT||!CHAT.id||S.busy)return;
  const row=Array.isArray(list)&&list.find(c=>c.id===CHAT.id);
  if(!row||!(row.updated> (CHAT.updated||0)+0.001))return;
  const c=await fetch('/api/chats/'+CHAT.id).then(r=>r.json());
  if(c.error||S.busy||!CHAT||CHAT.id!==c.id)return;
  carry=null;
  CHAT={...newChatState(),...c,sessions:{...fresh(),...c.sessions},ctx:{...fresh(),...c.ctx},sent:{...fresh(),...c.sent}};
  renderThread();scrollDown();
}
setInterval(refreshChats,4000);
refreshAccounts(true).then(list=>{if(list&&!list.some(a=>a.installed&&a.logged_in))openAccounts();});
msg.focus();

#!/usr/bin/env python3
"""Chatbox: one chat window for Cursor, Claude, Codex and ChatGPT, using your own subscriptions.

Runs a small local web server (127.0.0.1 only) and opens it in your browser
(or in its own window via app.py). Each message is sent to the `cursor-agent`,
`claude` or `codex` command-line tool. Standard library only.

Usage:  python3 chatbox.py [--port 8765] [--no-browser]
"""
import argparse
import json
import os
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import accounts

HOME = os.path.expanduser("~")
TIMEOUT = 600  # seconds per reply


DATA_DIR = os.environ.get("CHATBOX_DATA", os.path.join(HOME, ".local/share/chatbox"))

# Codex also ships inside the ChatGPT desktop app. A standalone `codex` on PATH wins;
# otherwise use the app's copy from one of these places (or set CHATBOX_CODEX).
CODEX_APP_CANDIDATES = [
    os.environ.get("CHATBOX_CODEX", ""),
    os.path.join(HOME, "Applications/chatgpt/usr/lib/chatgpt/resources/codex"),
    os.path.join(HOME, "Applications/ChatGPT/resources/codex"),
    "/opt/ChatGPT/resources/codex",
    "/opt/chatgpt/resources/codex",
    "/usr/lib/chatgpt/resources/codex",
]
CODEX_APP_PATH = next((p for p in CODEX_APP_CANDIDATES if p and os.access(p, os.X_OK)), None)
# This Codex version rejects the newest models with a ChatGPT login, so "Auto" uses this one.
CODEX_DEFAULT_MODEL = "gpt-5.6-terra"


def find(cmd):
    path = shutil.which(cmd) or (os.path.join(HOME, ".local/bin", cmd)
                                 if os.path.exists(os.path.join(HOME, ".local/bin", cmd)) else None)
    if not path and cmd == "codex" and CODEX_APP_PATH:
        path = CODEX_APP_PATH
    return path


# "ChatGPT" = Codex told to just chat, run read-only from an empty folder.
CHATGPT_HOME = os.path.join(DATA_DIR, "chatgpt-home")
CHATGPT_INSTRUCTIONS = ("You are ChatGPT, a friendly general-purpose assistant in a plain chat window. "
                        "Do not run commands, read files or use tools; just answer conversationally.")


def build_codex_command(message, session_id, mode, model, chat=False):
    exe = find("codex")
    if not exe:
        raise RuntimeError("Codex (from the ChatGPT app) is not installed.")
    sandbox = "read-only" if mode == "ask" or chat else "workspace-write"
    cmd = [exe, "exec"] + (["resume"] if session_id else [])
    # The ChatGPT app's older Codex needs an explicit model; a standalone one picks its own.
    model = model or (CODEX_DEFAULT_MODEL if CODEX_APP_PATH and exe == CODEX_APP_PATH else "")
    cmd += ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"']
    if model:
        cmd += ["-m", model]
    if chat:
        cmd += ["-c", "developer_instructions=" + json.dumps(CHATGPT_INSTRUCTIONS)]
    if session_id:
        cmd.append(session_id)
    cmd.append(message)
    return cmd


def parse_codex(out):
    """Codex prints one JSON event per line; collect the thread id, last reply and any error."""
    session_id, reply, error = None, None, None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            session_id = ev.get("thread_id")
        elif t == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            reply = ev["item"].get("text", "")
        elif t in ("error", "turn.failed"):
            msg = ev.get("message") or ev.get("error", {}).get("message", "Error")
            try:  # the message is often JSON from the server
                msg = json.loads(msg)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            error = msg
    if reply is not None:
        return {"reply": reply, "session_id": session_id}
    return {"error": error, "session_id": session_id} if error else None


def build_command(backend, message, session_id, mode, model):
    """Return the CLI command for one turn. 'ask' = read-only, 'agent' = can edit files/run commands."""
    if backend in ("codex", "chatgpt"):
        return build_codex_command(message, session_id, mode, model, chat=backend == "chatgpt")
    if backend == "cursor":
        exe = find("cursor-agent")
        if not exe:
            raise RuntimeError("cursor-agent is not installed.")
        cmd = [exe, "-p", "--trust", "--output-format", "json"]
        if mode == "ask":
            cmd += ["--mode", "ask"]
        else:
            cmd += ["--force"]
    elif backend == "claude":
        exe = find("claude")
        if not exe:
            raise RuntimeError("claude is not installed.")
        cmd = [exe, "-p", "--output-format", "json"]
        if mode == "ask":
            cmd += ["--permission-mode", "plan"]
        else:
            cmd += ["--permission-mode", "acceptEdits"]
    else:
        raise RuntimeError(f"Unknown backend: {backend}")
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)
    return cmd


def run_turn(backend, message, session_id, mode, model, workdir):
    cmd = build_command(backend, message, session_id, mode, model)
    workdir = os.path.expanduser(workdir or HOME)
    if backend == "chatgpt":  # plain chat: no project folder
        os.makedirs(CHATGPT_HOME, exist_ok=True)
        workdir = CHATGPT_HOME
    if not os.path.isdir(workdir):
        return {"error": f"Folder not found: {workdir}"}
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": f"No reply after {TIMEOUT} seconds."}
    out = proc.stdout.strip()
    if backend in ("codex", "chatgpt"):
        result = parse_codex(out)
        if result:
            if result.get("error"):
                result["error"] = login_hint(backend, result["error"])
            return result
    # The JSON result is the last line that parses.
    for line in reversed(out.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "result" in data:
            if data.get("is_error"):
                return {"error": login_hint(backend, data.get("result") or "Error"),
                        "session_id": data.get("session_id")}
            return {"reply": data.get("result", ""), "session_id": data.get("session_id")}
    err = (proc.stderr.strip() or out or f"Exited with code {proc.returncode}")[-2000:]
    return {"error": login_hint(backend, err)}


def login_hint(backend, err):
    low = err.lower()
    if "login" in low or "logged in" in low or "not installed" in low or "auth" in low:
        err += "\n\nOpen Accounts (top right) to install or sign in, then try again."
    return err


CLAUDE_MODELS = [
    ["fable", "Fable (latest)"],
    ["opus", "Opus (latest)"],
    ["sonnet", "Sonnet (latest)"],
    ["haiku", "Haiku (latest)"],
]
# Models this Codex version accepts with a ChatGPT login (tested 2026-11-07).
CODEX_MODELS = [
    ["gpt-5.6-terra", "GPT-5.6 Terra"],
    ["gpt-5.6-luna", "GPT-5.6 Luna"],
]
_model_cache = {}


def codex_is_bundled():
    return CODEX_APP_PATH is not None and find("codex") == CODEX_APP_PATH


def codex_cached_models():
    """Models a standalone Codex lists for this account (it keeps them in ~/.codex)."""
    try:
        with open(os.path.join(HOME, ".codex/models_cache.json")) as f:
            data = json.load(f)
        models = [[m["slug"], m.get("display_name") or m["slug"]]
                  for m in data.get("models", []) if m.get("visibility") == "list"]
        return models or CODEX_MODELS
    except (OSError, ValueError, KeyError, TypeError):
        return CODEX_MODELS


def list_models(backend):
    """[[id, label], ...] for the dropdown. 'Auto' (no --model flag) is added by the page."""
    if backend == "claude":
        return CLAUDE_MODELS
    if backend in ("codex", "chatgpt"):
        return CODEX_MODELS if codex_is_bundled() else codex_cached_models()
    if backend != "cursor":
        return []
    if "cursor" not in _model_cache:
        exe = find("cursor-agent")
        models = []
        if exe:
            try:
                out = subprocess.run([exe, "--list-models"], capture_output=True, text=True,
                                     timeout=60, stdin=subprocess.DEVNULL).stdout
            except subprocess.TimeoutExpired:
                out = ""
            for line in out.splitlines():
                line = line.replace("​", "").strip()
                if " - " not in line:
                    continue
                mid, label = line.split(" - ", 1)
                if mid != "auto" and " " not in mid:
                    models.append([mid, " ".join(label.split())])
        if models:
            _model_cache["cursor"] = models
        return models
    return _model_cache["cursor"]


ACCOUNTS = accounts.Accounts(find)


def status():
    return {"cursor": bool(find("cursor-agent")), "claude": bool(find("claude")),
            "codex": bool(find("codex")), "chatgpt": bool(find("codex")), "home": HOME}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self):
        # Block requests from other websites (DNS rebinding / cross-site posts).
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        if origin and not origin.startswith(("http://127.0.0.1:", "http://localhost:")):
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return self._send(403, '{"error":"forbidden"}')
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, json.dumps(status()))
        if self.path.startswith("/api/models?backend="):
            return self._send(200, json.dumps(list_models(self.path.split("=", 1)[1])))
        if self.path.startswith("/api/accounts"):
            return self._send(200, json.dumps(ACCOUNTS.status(fresh="fresh=1" in self.path)))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._local_only():
            return self._send(403, '{"error":"forbidden"}')
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, '{"error":"bad request"}')
        if self.path == "/api/account":
            try:
                ACCOUNTS.action(req.get("service"), req.get("action"), req.get("text", ""))
                if req.get("action") in ("dismiss", "cancel"):
                    _model_cache.clear()  # a new install/login may change the lists
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path == "/api/open":
            try:
                accounts.open_url(req.get("url", ""))
                return self._send(200, '{"ok":true}')
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path != "/api/chat":
            return self._send(404, '{"error":"not found"}')
        try:
            result = run_turn(req.get("backend"), req.get("message", ""), req.get("session_id"),
                              req.get("mode", "ask"), (req.get("model") or "").strip(),
                              req.get("workdir"))
        except Exception as e:  # report any failure to the page
            result = {"error": str(e)}
        self._send(200, json.dumps(result))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chatbox</title>
<style>
:root{--bg:#f6f6f4;--panel:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;
--you:#e8eefc;--cursor:#111;--claude:#c96442;--codex:#10a37f;--chatgpt:#6e56cf;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--text:#ecebe7;
--muted:#9a9993;--line:#33322f;--you:#24304a;--cursor:#e9e9e9;--claude:#e08a6a;--codex:#19c37d;--chatgpt:#a594f9;--err:#f2b8b5}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0 8px 0 0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--text);padding:6px 14px;cursor:pointer;font:inherit}
.seg button.on[data-b=cursor]{background:var(--cursor);color:var(--panel)}
.seg button.on[data-b=claude]{background:var(--claude);color:#fff}
.seg button.on[data-b=codex]{background:var(--codex);color:#fff}
.seg button.on[data-b=chatgpt]{background:var(--chatgpt);color:#fff}
.seg button:disabled.on[data-m]{opacity:.4}
.seg button.on[data-m]{background:var(--text);color:var(--panel)}
.seg button:disabled{opacity:.4;cursor:not-allowed}
input,textarea,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
header input{width:180px}.grow{flex:1}header select{max-width:240px}
.ghost{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}
main{flex:1;overflow-y:auto;padding:16px}
.wrap{max-width:820px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
.msg{padding:10px 14px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word;max-width:100%}
.msg .who{font-size:12px;font-weight:600;margin-bottom:2px}
.you{background:var(--you);align-self:flex-end}
.bot{background:var(--panel);border:1px solid var(--line)}
.bot.cursor .who{color:var(--cursor)}.bot.claude .who{color:var(--claude)}.bot.codex .who{color:var(--codex)}.bot.chatgpt .who{color:var(--chatgpt)}
.err{color:var(--err)}.note{color:var(--muted);text-align:center;font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;white-space:pre}
code{font-family:ui-monospace,monospace;font-size:13px}
footer{border-top:1px solid var(--line);background:var(--panel);padding:12px 16px}
footer form{max-width:820px;margin:0 auto;display:flex;gap:8px}
footer textarea{flex:1;resize:none;min-height:44px;max-height:200px}
footer button{background:var(--text);color:var(--panel);border:0;border-radius:8px;padding:0 18px;font:inherit;cursor:pointer}
footer button:disabled{opacity:.5}
.seg button.off:not(.on){opacity:.55}.seg button.off::after{content:" •";color:var(--err)}
#accbtn.warn::after{content:" •";color:var(--err)}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--panel);color:var(--text);padding:0;width:min(640px,calc(100vw - 32px));max-height:calc(100vh - 48px)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dlg-h{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
.dlg-h h2{font-size:16px;margin:0}
.dlg-b{padding:8px 18px 18px;display:flex;flex-direction:column;gap:12px}
.intro{color:var(--muted);font-size:13px;margin:6px 0 0}
#acclist{display:flex;flex-direction:column;gap:10px}
.acc{border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.acc-h{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.pill.ok{color:var(--codex);border-color:var(--codex)}.pill.bad{color:var(--err);border-color:var(--err)}
.muted{color:var(--muted);font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{border:0;border-radius:8px;padding:6px 14px;font:inherit;cursor:pointer;background:var(--text);color:var(--panel)}
.btn:disabled{opacity:.5;cursor:default}
.job{background:var(--bg);border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:8px;font-size:14px}
.job pre{margin:0;max-height:140px;overflow:auto;font-size:12px;white-space:pre-wrap}
.job input{flex:1;min-width:200px}
</style></head><body>
<header>
  <h1>Chatbox</h1>
  <div class="seg" id="backend">
    <button data-b="cursor">Cursor</button><button data-b="claude">Claude</button><button data-b="codex">Codex</button><button data-b="chatgpt">ChatGPT</button>
  </div>
  <div class="seg" id="mode" title="Ask = read-only. Agent = may edit files and run commands in the folder.">
    <button data-m="ask">Ask</button><button data-m="agent">Agent</button>
  </div>
  <select id="model" title="Model"><option value="">Auto</option></select>
  <input id="workdir" class="grow" placeholder="Folder" title="Folder the assistant works in">
  <button class="ghost" id="newchat">New chat</button>
  <button class="ghost" id="accbtn" title="Install, sign in or sign out">Accounts</button>
</header>
<dialog id="acc">
  <div class="dlg-h"><h2>Accounts</h2><button class="ghost" id="accclose">Close</button></div>
  <div class="dlg-b">
    <p class="intro">Chatbox uses your own subscriptions. Install each tool once and sign in. Sign-in happens in your browser; Chatbox never sees your password.</p>
    <div id="acclist"><p class="muted">Checking accounts…</p></div>
  </div>
</dialog>
<main><div class="wrap" id="log"></div></main>
<footer><form id="f">
  <textarea id="msg" placeholder="Message… (Enter to send, Shift+Enter for new line)"></textarea>
  <button id="send">Send</button>
</form></footer>
<script>
const $=s=>document.querySelector(s), log=$('#log');
const NAMES={cursor:'Cursor',claude:'Claude',codex:'Codex',chatgpt:'ChatGPT'},BACKENDS=Object.keys(NAMES);
const fresh=()=>({cursor:null,claude:null,codex:null,chatgpt:null});
const S={backend:'cursor',mode:'ask',sessions:fresh(),busy:false};
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
S.backend=store.get('backend')||'cursor';S.mode=store.get('mode')||'ask';
const modelCache={};
async function loadModels(){ // refill the dropdown for the current backend
  const b=S.backend,sel=$('#model');
  sel.innerHTML='<option value="">Auto</option>';
  if(!modelCache[b]){sel.disabled=true;sel.firstChild.textContent='Loading models…';
    try{modelCache[b]=await fetch('/api/models?backend='+b).then(r=>r.json());}catch(e){modelCache[b]=[];}
    sel.disabled=false;}
  if(b!==S.backend)return;
  sel.innerHTML='<option value="">Auto</option>'+modelCache[b].map(([id,l])=>'<option value="'+esc(id)+'">'+esc(l)+'</option>').join('');
  const saved=store.get('model_'+b)||'';
  sel.value=modelCache[b].some(m=>m[0]===saved)?saved:'';}
const esc=t=>t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function fmt(t){ // code blocks + inline code only
  return t.split(/```/).map((p,i)=>i%2?'<pre><code>'+esc(p.replace(/^[\w-]*\n/,''))+'</code></pre>'
    :esc(p).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')).join('');}
function add(cls,who,html){const d=document.createElement('div');d.className='msg '+cls;
  d.innerHTML=(who?'<div class="who">'+who+'</div>':'')+html;log.appendChild(d);
  $('main').scrollTop=1e9;return d;}
function paint(){
  document.querySelectorAll('#backend button').forEach(b=>b.classList.toggle('on',b.dataset.b===S.backend));
  const chat=S.backend==='chatgpt'; // plain chat: no folder, always read-only
  document.querySelectorAll('#mode button').forEach(b=>{b.classList.toggle('on',b.dataset.m===S.mode);b.disabled=chat;});
  $('#workdir').disabled=chat;$('#workdir').title=chat?'ChatGPT is a plain chat and does not use a folder':'Folder the assistant works in';}
$('#backend').onclick=e=>{const b=e.target.dataset.b;if(!b||e.target.disabled)return;
  if(b!==S.backend){S.backend=b;store.set('backend',b);
    add('note','', 'Switched to '+NAMES[b]+(S.sessions[b]?' — continuing its earlier chat':''));
    loadModels();}paint();};
$('#mode').onclick=e=>{const m=e.target.dataset.m;if(!m)return;S.mode=m;store.set('mode',m);paint();};
$('#model').onchange=e=>store.set('model_'+S.backend,e.target.value);
$('#workdir').onchange=e=>{store.set('workdir',e.target.value);S.sessions=fresh();
  add('note','','Folder changed — new chat started');};
$('#newchat').onclick=()=>{S.sessions=fresh();log.innerHTML='';add('note','','New chat');};
$('#msg').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#f').requestSubmit();}};
$('#f').onsubmit=async e=>{e.preventDefault();const text=$('#msg').value.trim();if(!text||S.busy)return;
  const b=S.backend,name=NAMES[b];
  $('#msg').value='';add('you','You',esc(text));
  const pending=add('bot '+b,name,'<span class="note">Thinking…</span>');
  S.busy=true;$('#send').disabled=true;const t0=Date.now();
  const tick=setInterval(()=>{pending.querySelector('.note').textContent='Thinking… '+Math.round((Date.now()-t0)/1000)+'s'},1000);
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({backend:b,message:text,session_id:S.sessions[b],mode:S.mode,
        model:$('#model').value,workdir:$('#workdir').value})}).then(r=>r.json());
    if(r.session_id)S.sessions[b]=r.session_id;
    pending.innerHTML='<div class="who">'+name+'</div>'+(r.error?'<span class="err">'+esc(r.error)+'</span>'
      +(/Open Accounts/.test(r.error)?'<div class="row" style="margin-top:8px"><button class="btn" onclick="openAccounts()">Open Accounts</button></div>':'')
      :fmt(r.reply||'(empty reply)'));
  }catch(err){pending.innerHTML='<div class="who">'+name+'</div><span class="err">'+esc(String(err))+'</span>';}
  clearInterval(tick);S.busy=false;$('#send').disabled=false;$('#msg').focus();$('main').scrollTop=1e9;};
// ---- Accounts ----
const SVC={cursor:'cursor',claude:'claude',codex:'codex',chatgpt:'codex'};
let ACC={},accJSON='',accTimer=null;const codes={};
const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
const ready=b=>{const a=ACC[SVC[b]];return !!(a&&a.installed&&a.logged_in);};
function markBackends(){
  document.querySelectorAll('#backend button').forEach(btn=>{const ok=ready(btn.dataset.b);
    btn.classList.toggle('off',!ok);btn.title=ok?'':NAMES[btn.dataset.b]+' is not connected. Open Accounts to set it up.';});
  $('#accbtn').classList.toggle('warn',!ready(S.backend));}
async function refreshAccounts(fresh){
  let list;try{list=await fetch('/api/accounts'+(fresh?'?fresh=1':'')).then(r=>r.json());}catch(e){return;}
  ACC=Object.fromEntries(list.map(a=>[a.id,a]));markBackends();
  const j=JSON.stringify(list);if(j!==accJSON){accJSON=j;renderAccounts(list);}
  clearTimeout(accTimer);
  if($('#acc').open)accTimer=setTimeout(()=>refreshAccounts(),list.some(a=>a.job&&a.job.state==='running')?1500:15000);
  return list;}
function renderAccounts(list){
  $('#acclist').innerHTML=list.map(a=>{
    const st=!a.installed?['bad','Not installed']:a.logged_in?['ok','Connected'+(a.account?' · '+esc(a.account):'')]:['bad','Not signed in'];
    const busy=a.job&&a.job.state==='running';
    let btns='';
    if(!a.installed)btns='<button class="btn" data-act="install" data-s="'+a.id+'"'+(busy?' disabled':'')+'>Install</button>';
    else if(!a.logged_in)btns='<button class="btn" data-act="login" data-s="'+a.id+'"'+(busy?' disabled':'')+'>Sign in</button>';
    else btns='<button class="ghost" data-act="logout" data-s="'+a.id+'"'+(busy?' disabled':'')+'>Sign out</button>';
    if(a.installed&&a.id==='codex'&&a.source)btns+=' <button class="ghost" data-act="install" data-s="codex"'+(busy?' disabled':'')+' title="Install OpenAI\'s standalone Codex, which is newer and supports more models">Install standalone Codex</button>';
    let job='';
    if(a.job){const J=a.job,what={install:'Installing',login:'Signing in',logout:'Signing out'}[J.kind];
      const state=J.state==='running'?what+'…':J.state==='done'?what.replace('ing','ed').replace('Signed in','Sign-in finished').replace('Installed','Installed')+' ✓':J.state==='cancelled'?'Cancelled':'Something went wrong';
      job='<div class="job"><b>'+state+'</b>';
      if(J.state==='running'&&J.kind==='login'){
        job+='<div class="muted">Your browser should open. Sign in there'+(a.id==='claude'?', then copy the code it shows and paste it below.':' and this will finish by itself.')+'</div>';
        if(J.url)job+='<div class="row"><button class="ghost" data-act="open" data-url="'+esc(J.url)+'">Open sign-in page</button></div>';}
      if(J.last&&J.state!=='cancelled'&&!/^(Opening browser|If the browser)/.test(J.last))job+='<div class="muted">'+esc(J.last)+'</div>';
      if(J.asks_code)job+='<div class="row"><input id="code_'+a.id+'" placeholder="Paste the code from the browser" value="'+esc(codes[a.id]||'')+'"><button class="btn" data-act="input" data-s="'+a.id+'">Submit code</button></div>';
      if(J.output)job+='<details'+(J.state==='failed'?' open':'')+'><summary class="muted">Details</summary><pre>'+esc(J.output)+'</pre></details>';
      job+='<div class="row">'+(J.state==='running'?'<button class="ghost" data-act="cancel" data-s="'+a.id+'">Cancel</button>':'<button class="ghost" data-act="dismiss" data-s="'+a.id+'">OK</button>')+'</div></div>';}
    return '<div class="acc"><div class="acc-h"><b>'+esc(a.name)+'</b><span class="pill '+st[0]+'">'+st[1]+'</span></div>'
      +'<div class="muted">Needs: '+esc(a.plan)+(a.source?' · Codex '+esc(a.source):'')+'</div>'
      +'<div class="row">'+btns+'</div>'+job+'</div>';}).join('');}
$('#acclist').addEventListener('input',e=>{if(e.target.id&&e.target.id.startsWith('code_'))codes[e.target.id.slice(5)]=e.target.value;});
$('#acclist').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id&&e.target.id.startsWith('code_')){e.preventDefault();e.target.nextElementSibling.click();}});
$('#acclist').addEventListener('click',async e=>{
  const t=e.target.closest('[data-act]');if(!t)return;const act=t.dataset.act,s=t.dataset.s,name=ACC[s]&&ACC[s].name;
  if(act==='open'){post('/api/open',{url:t.dataset.url});return;}
  if(act==='install'&&!confirm('Download and run the official '+name+' installer?'))return;
  if(act==='logout'&&!confirm('Sign out of '+name+' on this computer?'+(s==='codex'?'\n\nThis also signs out Codex in the ChatGPT app.':'')))return;
  const body={service:s,action:act};
  if(act==='input'){body.text=codes[s]||'';if(!body.text.trim())return;codes[s]='';}
  t.disabled=true;const r=await post('/api/account',body);
  if(r.error)alert(r.error);
  if(act==='dismiss'){for(const k in modelCache)delete modelCache[k];loadModels();}
  accJSON='';refreshAccounts(act==='dismiss');});
function openAccounts(){if(!$('#acc').open)$('#acc').showModal();accJSON='';refreshAccounts(true);}
$('#accbtn').onclick=openAccounts;
$('#accclose').onclick=()=>{$('#acc').close();};
$('#acc').addEventListener('close',()=>{clearTimeout(accTimer);refreshAccounts(true);});

fetch('/api/status').then(r=>r.json()).then(s=>{
  $('#workdir').value=store.get('workdir')||s.home;
  paint();loadModels();});
refreshAccounts(true).then(list=>{ // first run: nothing connected yet → show Accounts
  if(list&&!list.some(a=>a.installed&&a.logged_in))openAccounts();
  else if(list&&!ready(S.backend))add('note','',NAMES[S.backend]+' is not connected yet. Open Accounts (top right) to set it up.');});
paint();add('note','','Pick Cursor, Claude, Codex or ChatGPT above. Each keeps its own conversation; switching keeps both.');
$('#msg').focus();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Chatbox running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

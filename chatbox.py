#!/usr/bin/env python3
"""Chatbox: one chat window for Cursor, Claude, Codex, ChatGPT and local Ollama models.

Runs a small local web server (127.0.0.1 only) and opens it in your browser
(or in its own window via app.py). Each message is sent to the `cursor-agent`,
`claude` or `codex` command-line tool, or to a local Ollama server. Standard library only.

Usage:  python3 chatbox.py [--port 8765] [--no-browser]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import accounts
import memory
import ollama
import usage as usagemod
import vault as vaultmod

HOME = os.path.expanduser("~")
TIMEOUT = 600  # seconds per reply


DATA_DIR = os.environ.get("CHATBOX_DATA", os.path.join(HOME, ".local/share/chatbox"))

# Apps opened from the macOS Finder (or some Linux menus) get a short PATH, so add the
# usual places the tools are installed to. The assistants' child processes inherit it.
_extra = [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
_path = os.environ.get("PATH", "").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(_path + [d for d in _extra if os.path.isdir(d) and d not in _path])

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
    sandbox = "workspace-write" if mode == "agent" and not chat else "read-only"
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
    session_id, reply, error, usage = None, None, None, {}
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
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            usage = {"input": u.get("input_tokens", 0), "cache_read": u.get("cached_input_tokens", 0),
                     "output": u.get("output_tokens", 0), "reasoning": u.get("reasoning_output_tokens", 0)}
        elif t in ("error", "turn.failed"):
            msg = ev.get("message") or ev.get("error", {}).get("message", "Error")
            try:  # the message is often JSON from the server
                msg = json.loads(msg)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            error = msg
    if reply is not None:
        return {"reply": reply, "session_id": session_id, "usage": usage}
    return {"error": error, "session_id": session_id, "usage": usage} if error else None


def build_command(backend, message, session_id, mode, model):
    """Return the CLI command for one turn. 'ask' = read-only, 'agent' = can edit files/run commands."""
    if backend in ("codex", "chatgpt"):
        return build_codex_command(message, session_id, mode, model, chat=backend == "chatgpt")
    if backend == "cursor":
        exe = find("cursor-agent")
        if not exe:
            raise RuntimeError("cursor-agent is not installed.")
        cmd = [exe, "-p", "--trust", "--output-format", "json"]
        if mode == "agent":
            cmd += ["--force"]
        else:
            cmd += ["--mode", "plan" if mode == "plan" else "ask"]
    elif backend == "claude":
        exe = find("claude")
        if not exe:
            raise RuntimeError("claude is not installed.")
        cmd = [exe, "-p", "--output-format", "json"]
        # Ask/Plan: may read files, anything needing permission is refused.
        # Agent: "auto" = edits plus commands that Claude's safety check approves.
        cmd += ["--permission-mode", "auto" if mode == "agent" else "dontAsk"]
    else:
        raise RuntimeError(f"Unknown backend: {backend}")
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)
    return cmd


def run_turn(backend, message, session_id, mode, model, workdir):
    if backend == "ollama":  # an HTTP server, not a command-line tool
        return ollama.run_turn(ollama_url(), OLLAMA, message, session_id, model, TIMEOUT)
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
            return {"reply": data.get("result", ""), "session_id": data.get("session_id"),
                    "usage": json_usage(data)}
    err = (proc.stderr.strip() or out or f"Exited with code {proc.returncode}")[-2000:]
    return {"error": login_hint(backend, err)}


def json_usage(data):
    """Token counts (and Claude's cost) from a claude / cursor-agent JSON result."""
    u = data.get("usage") or {}
    pick = lambda *keys: next((u[k] for k in keys if isinstance(u.get(k), (int, float))), 0)
    out = {"input": pick("input_tokens", "inputTokens"),
           "output": pick("output_tokens", "outputTokens"),
           "cache_read": pick("cache_read_input_tokens", "cacheReadTokens"),
           "cache_write": pick("cache_creation_input_tokens", "cacheWriteTokens")}
    if isinstance(data.get("total_cost_usd"), (int, float)):
        out["cost"] = data["total_cost_usd"]
    models = list((data.get("modelUsage") or {}).keys())
    if models:
        out["model"] = models[0]
    return out


def login_hint(backend, err):
    low = err.lower()
    if "login" in low or "logged in" in low or "not installed" in low or "auth" in low:
        err += "\n\nOpen Accounts (bottom left) to install or sign in, then try again."
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
    if backend == "ollama":  # asked fresh each time: it's fast, and you may have just pulled one
        return ollama.list_models(ollama_url())
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
MEMORY = memory.Store(DATA_DIR)
USAGE = usagemod.Log(DATA_DIR)
OLLAMA = ollama.Sessions(DATA_DIR)


def ollama_url():
    return MEMORY.settings()["ollama_url"]


PLAN_PROMPT = (
    "[Chatbox PLAN mode] Do not create, edit or delete any files and do not run commands that "
    "change anything. You may read files to understand the project. Reply with a complete "
    "implementation plan in Markdown: start with a '# <short title>' heading, then sections "
    "'## Goal', '## Steps' (a '- [ ]' checklist that names the files to touch) and "
    "'## Risks and questions'. Chatbox will save your reply as a .md plan file for the user to "
    "review before anything is built.\n\n")
PLAN_FILES = set()  # plan files Chatbox wrote this run (the page may open these)


def save_plan(reply, backend, workdir):
    """Save a plan reply as Markdown. Folder-based assistants: <folder>/plans/.
    ChatGPT / Ollama (no folder): the Obsidian vault's Chatbox/Plans, else Chatbox's data folder."""
    m = re.search(r"^#\s+(.+)$", reply, re.MULTILINE)
    title = re.sub(r"[\\/:*?\"<>|#`\n\r\t]+", " ", m.group(1) if m else "Plan").strip()[:60] or "Plan"
    if backend in ("chatgpt", "ollama"):  # no project folder
        v = MEMORY.vault()
        folder = os.path.join(v.path, "Chatbox", "Plans") if v else os.path.join(DATA_DIR, "plans")
    else:
        folder = os.path.join(os.path.expanduser(workdir or HOME), "plans")
    os.makedirs(folder, exist_ok=True)
    title = re.sub(r"\.md$", "", title, flags=re.I).strip() or "Plan"
    path = os.path.join(folder, time.strftime("%Y-%m-%d %H%M ") + title + ".md")
    body = reply.strip() + "\n"
    if not body.startswith("#"):
        body = f"# {title}\n\n" + body
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    PLAN_FILES.add(path)
    return path


def chat(req):
    """One chat turn with saved memories: send them when a chat starts (or after they
    change), and save any REMEMBER: lines the assistant adds."""
    session_id, message = req.get("session_id"), req.get("message", "")
    version = MEMORY.version()
    context = MEMORY.context() if (not session_id or req.get("context_version") != version) else ""
    notes = []
    v = MEMORY.vault()
    if v and MEMORY.settings()["use_memory"] and MEMORY.settings()["vault_search"]:
        notes = v.search(req.get("message", ""), exclude=set(req.get("sent_notes") or []))
        if notes:
            context += ("[Notes from the user's Obsidian vault (their long-term memory) that may "
                        "relate to this message. Use them if helpful.]\n\n"
                        + "\n\n".join(f"### {n['path']}\n{n['text']}" for n in notes)
                        + "\n\n[End of notes]\n\n")
    if req.get("mode") == "plan":
        context += PLAN_PROMPT
    if context:
        message = context + ("The user's message:\n" if not session_id
                             else "(Context updated.) The user's message:\n") + message
    backend, model = req.get("backend"), (req.get("model") or "").strip()
    started = time.time()
    result = run_turn(backend, message, session_id, req.get("mode", "ask"), model, req.get("workdir"))
    if backend in usagemod.BACKENDS and not str(result.get("error", "")).startswith("Folder not found"):
        if not model and backend in ("codex", "chatgpt") and codex_is_bundled():
            model = CODEX_DEFAULT_MODEL
        result["usage"] = USAGE.record(backend, req.get("mode", "ask"), model,
                                       "reply" in result, time.time() - started, result.get("usage"))
    if result.get("reply") is not None:
        result["reply"], result["saved_memories"] = MEMORY.take_memories(result["reply"])
        result["context_version"] = MEMORY.version()  # the assistant already knows what it saved
        result["notes_used"] = [{"path": n["path"], "title": n["title"]} for n in notes]
        if req.get("mode") == "plan" and result["reply"].strip():
            try:
                result["plan_file"] = save_plan(result["reply"], req.get("backend"), req.get("workdir"))
            except OSError as e:
                result["plan_error"] = f"Could not save the plan: {e}"
    else:
        result["context_version"] = req.get("context_version")
    return result


# ---- chat history: one JSON file per chat in <data>/chats ----
CHATS_DIR = os.path.join(DATA_DIR, "chats")
CHAT_ID = re.compile(r"^[a-f0-9]{12}$")


def _chat_path(cid):
    if not CHAT_ID.match(cid or ""):
        raise ValueError("Bad chat id")
    return os.path.join(CHATS_DIR, cid + ".json")


def list_chats():
    out = []
    try:
        names = os.listdir(CHATS_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CHATS_DIR, name), encoding="utf-8") as f:
                c = json.load(f)
            out.append({k: c.get(k) for k in ("id", "title", "updated", "backend")})
        except (OSError, ValueError):
            continue
    out.sort(key=lambda c: c.get("updated") or 0, reverse=True)
    return out[:300]


def load_chat(cid):
    try:
        with open(_chat_path(cid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_chat(chat):
    path = _chat_path(chat.get("id"))
    os.makedirs(CHATS_DIR, exist_ok=True)
    chat["updated"] = time.time()
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(chat, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def delete_chat(cid):
    try:
        os.remove(_chat_path(cid))
    except FileNotFoundError:
        pass


def settings_payload():
    v = MEMORY.vault()
    where = (os.path.join(v.path, vaultmod.MEMORY_NOTE) if v and MEMORY.settings()["memory_in_vault"]
             else MEMORY.memories_path)
    return {"settings": MEMORY.settings(), "memories": MEMORY.memories(), "path": where,
            "vault": v.info() if v else None, "detected": vaultmod.detect_vaults()}


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
            return self._send(200, page(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, json.dumps(status()))
        if self.path.startswith("/api/models?backend="):
            return self._send(200, json.dumps(list_models(self.path.split("=", 1)[1])))
        if self.path == "/api/settings":
            return self._send(200, json.dumps(settings_payload()))
        if self.path == "/api/chats":
            return self._send(200, json.dumps(list_chats()))
        m = re.match(r"^/api/usage(?:\?days=(\d+))?$", self.path)
        if m:
            return self._send(200, json.dumps(USAGE.summary(int(m.group(1) or 30))))
        m = re.match(r"^/api/chats/([a-f0-9]{12})$", self.path)
        if m:
            chat = load_chat(m.group(1))
            return self._send(200 if chat else 404, json.dumps(chat or {"error": "not found"}))
        if self.path.startswith("/api/accounts"):
            return self._send(200, json.dumps(ACCOUNTS.status(fresh="fresh=1" in self.path)
                                              + [ollama.status(ollama_url())]))
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
        if self.path in ("/api/settings", "/api/memory", "/api/obsidian"):
            try:
                if self.path == "/api/obsidian":
                    act = req.get("action")
                    if act == "connect":
                        MEMORY.connect_vault(req.get("path", ""))
                    elif act == "disconnect":
                        MEMORY.disconnect_vault()
                    elif act == "save_chat":
                        v = MEMORY.vault()
                        if not v:
                            raise ValueError("Connect an Obsidian vault first (Accounts).")
                        rel = v.save_chat(req.get("messages") or [], req.get("backend", ""))
                        return self._send(200, json.dumps({"saved": rel, **settings_payload()}))
                    else:
                        raise ValueError("Unknown action")
                elif self.path == "/api/settings":
                    MEMORY.update_settings(req.get("settings"))
                else:
                    act = req.get("action")
                    if act == "add":
                        item = MEMORY.add(req.get("text", ""))
                        if item is None:
                            raise ValueError("That is already in memory.")
                    elif act == "edit":
                        MEMORY.edit(req.get("id"), req.get("text", ""))
                    elif act == "delete":
                        MEMORY.delete(req.get("id"))
                    elif act == "clear":
                        MEMORY.clear()
                    else:
                        raise ValueError("Unknown action")
                return self._send(200, json.dumps(settings_payload()))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e), **settings_payload()}))
        if self.path == "/api/usage" and req.get("action") == "clear":
            USAGE.clear()
            return self._send(200, json.dumps(USAGE.summary(30)))
        if self.path == "/api/chats":
            try:
                if req.get("action") == "save":
                    save_chat(req.get("chat") or {})
                elif req.get("action") == "delete":
                    delete_chat(req.get("id", ""))
                else:
                    raise ValueError("Unknown action")
                return self._send(200, json.dumps(list_chats()))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path == "/api/openfile":
            path = req.get("path", "")
            if path not in PLAN_FILES:
                return self._send(200, '{"error":"Only plan files saved by Chatbox can be opened."}')
            accounts.open_path(path)
            return self._send(200, '{"ok":true}')
        if self.path == "/api/open":
            try:
                accounts.open_url(req.get("url", ""))
                return self._send(200, '{"ok":true}')
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path != "/api/chat":
            return self._send(404, '{"error":"not found"}')
        try:
            result = chat(req)
        except Exception as e:  # report any failure to the page
            result = {"error": str(e)}
        self._send(200, json.dumps(result))


UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


def page():
    with open(UI_FILE, encoding="utf-8") as f:
        return f.read()


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

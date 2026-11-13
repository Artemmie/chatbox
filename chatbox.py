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
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import accounts
import attachments
import chatgpt
import memory
import ollama
import streaming
import runtime
import sync as syncmod
import update as updatemod
import usage as usagemod
import vault as vaultmod

HOME = os.path.expanduser("~")
TIMEOUT = streaming.TOTAL_TIMEOUT  # seconds per reply; override with CHATBOX_TIMEOUT


DATA_DIR = runtime.default_data_dir()
HERE = runtime.source_root()
CURRENT_PORT = None
chatgpt.init(DATA_DIR, accounts.open_url)

# Apps opened from the macOS Finder (or some Linux menus) get a short PATH, so add the
# usual places the tools are installed to. The assistants' child processes inherit it.
_extra = [os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
if os.name == "nt":
    _extra = runtime.windows_tool_dirs() + _extra
_path = os.environ.get("PATH", "").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(_path + [d for d in _extra if os.path.isdir(d) and d not in _path])

# Codex also ships inside the ChatGPT desktop app. A standalone `codex` on PATH wins;
# otherwise use the app's copy from one of these places (or set CHATBOX_CODEX).
CODEX_APP_CANDIDATES = runtime.codex_app_candidates(HOME)
CODEX_APP_PATH = next((p for p in CODEX_APP_CANDIDATES if runtime.executable(p)), None)
# This Codex version rejects the newest models with a ChatGPT login, so "Auto" uses this one.
CODEX_DEFAULT_MODEL = "gpt-5.6-terra"


def find(cmd):
    path = shutil.which(cmd)
    if not path and os.name == "nt":
        path = runtime.find_in_dirs(cmd, runtime.windows_tool_dirs(), (".cmd", ".exe", ".bat", ""))
    if not path:
        local = os.path.join(HOME, ".local/bin", cmd)
        if os.path.exists(local):
            path = local
    if not path and cmd == "codex" and CODEX_APP_PATH:
        path = CODEX_APP_PATH
    return path


def build_codex_command(message, session_id, mode, model):
    exe = find("codex")
    if not exe:
        raise RuntimeError("Codex is not installed.")
    sandbox = "workspace-write" if mode == "agent" else "read-only"
    cmd = [exe, "exec"] + (["resume"] if session_id else [])
    # The ChatGPT app's older Codex needs an explicit model; a standalone one picks its own.
    model = model or (CODEX_DEFAULT_MODEL if CODEX_APP_PATH and exe == CODEX_APP_PATH else "")
    cmd += ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"']
    if model:
        cmd += ["-m", model]
    if session_id:
        cmd.append(session_id)
    cmd.append(message)
    return cmd


def parse_codex(out):
    """Codex prints one JSON event per line; collect the thread id, last reply and any error."""
    parser = streaming.CodexParser()
    for line in out.splitlines():
        parser.feed_line(line)
    return parser.finish()


def build_command(backend, message, session_id, mode, model, stream="full"):
    """Return the CLI command for one turn. 'ask' = read-only, 'agent' = can edit files/run commands.

    stream is 'full' (token deltas), 'basic' (one JSON event per message) or 'off'
    (a single JSON result). Codex already streams events with --json, so stream is ignored there.
    """
    if backend == "codex":
        return build_codex_command(message, session_id, mode, model)
    if backend == "cursor":
        exe = find("cursor-agent")
        if not exe:
            raise RuntimeError("cursor-agent is not installed.")
        fmt = "json" if stream == "off" else "stream-json"
        cmd = [exe, "-p", "--trust", "--output-format", fmt]
        if stream == "full":
            cmd.append("--stream-partial-output")
        if mode == "agent":
            cmd += ["--force"]
        else:
            cmd += ["--mode", "plan" if mode == "plan" else "ask"]
    elif backend == "claude":
        exe = find("claude")
        if not exe:
            raise RuntimeError("claude is not installed.")
        fmt = "json" if stream == "off" else "stream-json"
        cmd = [exe, "-p", "--output-format", fmt]
        if stream != "off":
            cmd.append("--verbose")  # stream-json stays quiet without it when stdout is not a terminal
        if stream == "full":
            cmd.append("--include-partial-messages")
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


def json_usage(data):
    """Token counts (and Claude's cost) from a claude / cursor-agent JSON result."""
    return streaming.json_usage(data)


BACKEND_NAMES = {"cursor": "Cursor", "claude": "Claude", "codex": "Codex",
                 "chatgpt": "ChatGPT", "ollama": "Ollama"}
_EXE = {"cursor": "cursor-agent", "claude": "claude", "codex": "codex", "chatgpt": "codex"}


def _span(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = max(1, seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _timeout_message(exc, backend=None):
    who = BACKEND_NAMES.get(backend, "The assistant")
    if exc.kind == "start":
        return (f"{who} produced no output for {_span(exc.seconds)}, so the request was stopped. "
                "It may be stuck, or still starting up. Try again — or press Stop if it hangs.")
    if exc.kind == "idle":
        return (f"{who} went quiet for {_span(exc.seconds)}, so the request was stopped. It looked stuck.")
    return (f"{who} did not finish within {_span(exc.seconds)}, so the request was stopped.")


def preflight(backend):
    """A clear error when the tool is missing or the cached sign-in says it is not connected.

    A stale cache is ignored so a sign-in that just finished is not blocked.
    """
    if backend == "ollama":
        return None
    if backend == "chatgpt":
        if not chatgpt.logged_in():
            return ("ChatGPT is not signed in. Open Accounts (bottom left) and choose "
                    "Continue with ChatGPT. That uses your ChatGPT plan — no API key.")
        return None
    if backend not in _EXE:
        return None
    name, exe = BACKEND_NAMES[backend], _EXE[backend]
    if not find(exe):
        return (f"{name} is not installed ({exe} was not found). "
                "Open Accounts (bottom left) to install it, then try again.")
    sid = backend
    with ACCOUNTS.lock:
        fresh = time.time() - ACCOUNTS._cache_time < 20
        info = (ACCOUNTS._cache or {}).get(sid) if fresh else None
    if info and info.get("installed") and not info.get("logged_in") and not info.get("error"):
        return (f"{name} is not signed in. "
                "Open Accounts (bottom left) to sign in, then try again.")
    return None


def stream_cli(backend, message, session_id, mode, model, workdir, turn):
    """Yield ('delta'|'replace'|'status', text). Return the result dict."""
    attempts = ["codex"] if backend == "codex" else ["full", "basic", "off"]
    emitted = False
    last_err, last_code = "", 1
    parser = None
    for attempt in attempts:
        if turn.cancel.is_set():
            raise streaming.Cancelled()
        parser = streaming.PARSERS[backend]()
        cmd = build_command(backend, message, session_id, mode, model,
                            stream="off" if attempt == "codex" else attempt)
        try:
            for line in streaming.read_stdout(cmd, workdir, turn):
                for kind, text in parser.feed_line(line):
                    emitted = True
                    yield (kind, text)
        except streaming.Cancelled:
            raise
        except streaming.TurnTimeout:
            raise
        if turn.cancel.is_set():
            raise streaming.Cancelled()
        result = parser.finish()
        err = (turn.stderr or "").strip()
        last_err, last_code = err, turn.returncode
        # A crash (or a kill) after some text is not a finished answer. A clean
        # exit with no terminal event still counts: the text we saw is the reply.
        if turn.returncode not in (0, None) and not getattr(parser, "saw_terminal", False):
            result = {"error": err or f"Exited with code {turn.returncode}",
                      "session_id": getattr(parser, "session_id", None)}
        if result and result.get("reply") is not None:
            return result
        if result and result.get("error") and not streaming.unknown_flag(err):
            result["error"] = login_hint(backend, result["error"])
            return result
        if emitted:
            if result and result.get("error"):
                result["error"] = login_hint(backend, result["error"])
                return result
            return {"error": login_hint(backend, err or "The assistant stopped before it finished."),
                    "session_id": getattr(parser, "session_id", None)}
        if streaming.unknown_flag(err) and attempt != attempts[-1]:
            continue
        break
    detail = (last_err or f"Exited with code {last_code}")[-2000:]
    return {"error": login_hint(backend, detail),
            "session_id": getattr(parser, "session_id", None) if parser else None}


def run_turn(backend, message, session_id, mode, model, workdir):
    """One turn, collected. Streaming callers use chat_stream; this waits for the final result."""
    turn = streaming.Turn(uuid.uuid4().hex)
    gen = iter_backend(backend, message, session_id, mode, model, workdir, turn)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value or {"error": "No reply."}
    except streaming.Cancelled:
        return {"error": "Stopped."}
    except streaming.TurnTimeout as e:
        return {"error": _timeout_message(e, backend)}
    except ollama.Stopped:
        return {"error": "Stopped."}
    except TimeoutError:
        return {"error": f"No reply after {streaming.TOTAL_TIMEOUT} seconds."}
    except (ConnectionError, RuntimeError, OSError) as e:
        return {"error": str(e) or "No reply."}


def _attach(backend, message, files, workdir, model):
    """Return (message, chatgpt_parts, ollama_images). Raises ValueError when refused."""
    if not files:
        return message, None, None
    if backend in ("claude", "cursor", "codex"):
        return message + "\n\n" + attachments.stage_for_cli(workdir, files), None, None
    if backend == "chatgpt":
        return message, attachments.for_chatgpt(files), None
    if backend == "ollama":
        if any(item["kind"] == "image" for item in files) and not model:
            models = ollama.list_models(ollama_url())
            model = models[0][0] if models else ""
        vision = ollama.supports_vision(ollama_url(), model) if any(item["kind"] == "image" for item in files) else None
        extra, images = attachments.for_ollama(files, vision)
        if extra:
            message += "\n\n" + extra
        return message, None, images or None
    return message, None, None


def iter_backend(backend, message, session_id, mode, model, workdir, turn, files=None):
    """Yield stream events and return the result dict (possibly {"error": ...})."""
    files = files or []
    if backend == "ollama":
        try:
            message, _, images = _attach(backend, message, files, workdir, model)
        except ValueError as e:
            return {"error": str(e)}
        try:
            result = yield from ollama.stream_turn(ollama_url(), OLLAMA, message, session_id, model,
                                                   turn, streaming.TOTAL_TIMEOUT, images=images)
        except TimeoutError as e:
            return {"error": str(e) or f"No reply after {streaming.TOTAL_TIMEOUT} seconds."}
        except (ConnectionError, RuntimeError, OSError) as e:
            return {"error": str(e) or "No reply."}
        return result or {"error": "No reply."}
    if backend == "chatgpt":
        try:
            message, parts, _ = _attach(backend, message, files, workdir, model)
        except ValueError as e:
            return {"error": str(e)}
        try:
            result = yield from chatgpt.stream_turn(message, session_id, model, turn,
                                                   streaming.TOTAL_TIMEOUT, attachments=parts)
        except TimeoutError as e:
            return {"error": str(e) or f"No reply after {streaming.TOTAL_TIMEOUT} seconds."}
        except (ConnectionError, RuntimeError, OSError) as e:
            return {"error": str(e) or "ChatGPT did not answer."}
        return result or {"error": "No reply."}
    workdir = os.path.expanduser(workdir or HOME)
    if not os.path.isdir(workdir):
        return {"error": f"Folder not found: {workdir}"}
    try:
        message, _, _ = _attach(backend, message, files, workdir, model)
    except ValueError as e:
        return {"error": str(e)}
    try:
        result = yield from stream_cli(backend, message, session_id, mode, model, workdir, turn)
    except RuntimeError as e:  # missing executable, unknown backend
        return {"error": login_hint(backend, str(e))}
    return result or {"error": "No reply."}


def _detail_line(err):
    line = next((ln.strip() for ln in str(err).splitlines() if ln.strip()), str(err))
    return line[:300]


def login_hint(backend, err):
    """Turn a raw CLI failure into a short explanation, keeping one line of detail."""
    name = BACKEND_NAMES.get(backend, "The assistant")
    low = str(err).lower()
    if "open accounts" in low and ("not signed in" in low or "not installed" in low):
        return err
    signed_out = any(s in low for s in (
        "not logged in", "not signed in", "please log in", "please login", "please sign in",
        "authentication required", "unauthorized", "auth login", "run /login", "claude auth login"))
    if signed_out:
        return (f"{name} is not signed in. Open Accounts (bottom left) to sign in, then try again."
                f"\n\nDetails: {_detail_line(err)}")
    if "not installed" in low or "no such file" in low or "command not found" in low:
        return (f"{name} is not installed. Open Accounts (bottom left) to install it, then try again."
                f"\n\nDetails: {_detail_line(err)}")
    if "login" in low or "logged in" in low or "auth" in low:
        return err + "\n\nOpen Accounts (bottom left) to install or sign in, then try again."
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
    if backend == "chatgpt":
        return chatgpt.list_models()
    if backend == "codex":
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


def _match_project(vault, message, workdir, ranked=None):
    """The project this turn is about, or ''. Never raises."""
    try:
        return vault.match_project(message or "", workdir or "", ranked=ranked,
                                   search=ranked is None) or ""
    except Exception:
        return ""


def _prepare_message(req):
    """Context (memories, vault map, notes, plan rules) prepended when needed."""
    session_id, message = req.get("session_id"), req.get("message", "")
    v = MEMORY.vault()
    settings = MEMORY.settings()
    use = settings["use_memory"]
    ranked = []
    project = ""
    if v and use and settings["vault_search"]:
        try:
            ranked = v.rank(req.get("message", ""), exclude=set(req.get("sent_notes") or []))
        except Exception:
            ranked = []
        project = _match_project(v, req.get("message", ""), req.get("workdir") or "", ranked)
    elif v and use:
        project = _match_project(v, req.get("message", ""), req.get("workdir") or "", ranked=[])
    version = MEMORY.version(project)
    fresh = not session_id or req.get("context_version") != version
    context = MEMORY.context(project) if (use and fresh) else ""
    notes = []
    if v and use and settings["vault_search"]:
        if req.get("backend") in ("claude", "codex", "cursor"):
            context += "[Obsidian] " + v.search_hint() + "\n\n"
        if fresh:
            try:
                context += "[Vault map]\n" + v.map_text() + "\n[End of vault map]\n\n"
            except Exception:
                pass
        try:
            notes = v.pick(ranked, project=project or None)
            hub = v.hub_snippet(project) if project else None
        except Exception:
            notes, hub = [], None
        blocks = []
        if hub:
            blocks.append(f"### {hub['breadcrumb']}\n{hub['text']}")
        for note in notes:
            if hub and note["path"] == hub["path"]:
                continue
            blocks.append(f"### {note.get('breadcrumb') or note['path']}\n{note['text']}")
        if blocks:
            context += ("[Notes from the user's Obsidian vault (their long-term memory) that may "
                        "relate to this message. Use them if helpful. When a project matches, its "
                        "hub note is first and notes inside that project rank higher.]\n\n"
                        + "\n\n".join(blocks)
                        + "\n\n[End of notes]\n\n")
    if req.get("mode") == "plan":
        context += PLAN_PROMPT
    if context:
        message = context + ("The user's message:\n" if not session_id
                             else "(Context updated.) The user's message:\n") + message
    return message, notes


def _finish_result(req, result, notes, started):
    """Usage, saved memories and the plan file. Mutates result."""
    backend, model = req.get("backend"), (req.get("model") or "").strip()
    if backend in usagemod.BACKENDS and not str(result.get("error", "")).startswith("Folder not found"):
        if not model and backend == "codex" and codex_is_bundled():
            model = CODEX_DEFAULT_MODEL
        result["usage"] = USAGE.record(backend, req.get("mode", "ask"), model,
                                       "reply" in result, time.time() - started, result.get("usage"))
    if result.get("reply") is not None:
        result["reply"], result["saved_memories"] = MEMORY.take_memories(
            result["reply"], req.get("message", ""), notes)
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


def chat_stream(req):
    """NDJSON events for one turn: started, delta/replace/status, then done or error."""
    turn_id = str(req.get("turn_id") or "")
    if not re.fullmatch(r"[a-f0-9]{8,64}", turn_id):
        turn_id = uuid.uuid4().hex
    turn = streaming.Turn(turn_id)
    streaming.register(turn)
    try:
        yield {"type": "started", "turn_id": turn.id}
        try:
            if turn.cancel.is_set():
                yield {"type": "error", "error": "Stopped.", "stopped": True,
                       "context_version": req.get("context_version")}
                return
            try:
                files = attachments.load_many(DATA_DIR, req.get("attachments") or [])
            except ValueError as e:
                yield {"type": "error", "error": str(e), "context_version": req.get("context_version")}
                return
            if files and not str(req.get("message") or "").strip():
                req = dict(req)
                req["message"] = "Please look at the attached files."
            message, notes = _prepare_message(req)
            backend, model = req.get("backend"), (req.get("model") or "").strip()
            problem = preflight(backend)
            if problem:
                yield {"type": "error", "error": problem, "context_version": req.get("context_version")}
                return
            started = time.time()
            gen = iter_backend(backend, message, req.get("session_id"), req.get("mode", "ask"),
                               model, req.get("workdir"), turn, files)
            try:
                while True:
                    kind, text = next(gen)
                    yield {"type": kind, "text": text}
            except StopIteration as stop:
                result = stop.value or {"error": "No reply."}
            result = _finish_result(req, result, notes, started)
        except streaming.Cancelled:
            yield {"type": "error", "error": "Stopped.", "stopped": True,
                   "context_version": req.get("context_version")}
            return
        except streaming.TurnTimeout as e:
            yield {"type": "error", "error": _timeout_message(e, req.get("backend")),
                   "context_version": req.get("context_version")}
            return
        except ollama.Stopped:
            yield {"type": "error", "error": "Stopped.", "stopped": True,
                   "context_version": req.get("context_version")}
            return
        except Exception as e:
            yield {"type": "error", "error": str(e), "context_version": req.get("context_version")}
            return
        if result.get("reply") is not None:
            yield {"type": "done", **{k: v for k, v in result.items()}}
        else:
            yield {"type": "error", "error": result.get("error") or "No reply.",
                   "session_id": result.get("session_id"),
                   "context_version": result.get("context_version")}
    except GeneratorExit:
        turn.kill()
        raise
    finally:
        if (turn.proc and turn.proc.poll() is None) or turn.cancel.is_set():
            turn.kill()
        streaming.finish(turn)


def chat(req):
    """One chat turn, waiting for the final reply. The page uses chat_stream instead."""
    final = {"error": "No reply."}
    for ev in chat_stream(req):
        if ev.get("type") == "done":
            final = {k: v for k, v in ev.items() if k != "type"}
        elif ev.get("type") == "error":
            final = {"error": ev.get("error") or "No reply.", "session_id": ev.get("session_id"),
                     "context_version": ev.get("context_version")}
    return final


# ---- chat history: one JSON file per chat in <data>/chats ----
CHATS_DIR = os.path.join(DATA_DIR, "chats")
CHAT_ID = re.compile(r"^[a-f0-9]{12}$")


def _chat_path(cid):
    if not CHAT_ID.match(cid or ""):
        raise ValueError("Bad chat id")
    return os.path.join(CHATS_DIR, cid + ".json")


def _sync_root():
    return syncmod.root_for(MEMORY)


def _vault_path():
    try:
        vault = MEMORY.vault()
    except Exception:
        return None
    return vault.path if vault else None


def list_chats():
    root = _sync_root()
    if root:
        try:
            return syncmod.list_chats(root, vault_path=_vault_path())
        except Exception:
            return []
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
            out.append({k: c.get(k) for k in ("id", "title", "updated", "backend", "project")})
        except (OSError, ValueError):
            continue
    out.sort(key=lambda c: c.get("updated") or 0, reverse=True)
    return out[:300]


def load_chat(cid):
    root = _sync_root()
    if root:
        try:
            return syncmod.load_chat(root, cid, vault_path=_vault_path())
        except Exception:
            return None
    try:
        with open(_chat_path(cid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_chat(chat):
    if not CHAT_ID.match(chat.get("id") or ""):
        raise ValueError("Bad chat id")
    chat["updated"] = time.time()
    root = _sync_root()
    if root:
        try:
            syncmod.save_chat(root, chat, vault_path=_vault_path())
            return
        except ValueError:
            raise
        except Exception:
            pass  # vault write failed; keep the chat on this computer
    path = _chat_path(chat.get("id"))
    os.makedirs(CHATS_DIR, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(chat, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def delete_chat(cid):
    root = _sync_root()
    if root:
        try:
            syncmod.delete_chat(root, cid, vault_path=_vault_path())
        except Exception:
            return
        return
    try:
        os.remove(_chat_path(cid))
    except FileNotFoundError:
        pass


def settings_payload():
    try:
        v = MEMORY.vault()
    except Exception:
        v = None
    settings = MEMORY.settings()
    where = (os.path.join(v.path, v.memory_note()) if v and settings["memory_in_vault"]
             else MEMORY.memories_path)
    projects = []
    if v:
        try:
            projects = [{"name": p["name"], "description": p.get("description") or ""}
                        for p in v.map().get("projects") or []]
        except Exception:
            projects = []
    try:
        installed = vaultmod.obsidian_installed()
    except Exception:
        installed = False
    notice = vaultmod.connection_notice(settings.get("vault_path") or "", bool(v), installed)
    return {"settings": settings, "memories": MEMORY.memories(), "path": where,
            "vault": v.info() if v else None, "detected": vaultmod.detect_vaults(),
            "sync": syncmod.describe(MEMORY), "projects": projects, "obsidian_app": notice}


def status():
    return {"cursor": bool(find("cursor-agent")), "claude": bool(find("claude")),
            "codex": bool(find("codex")), "chatgpt": bool(find("codex")), "home": HOME}


def _updates_busy():
    if streaming.any_active():
        return True
    service = chatgpt.SERVICE
    job = getattr(service, "job", None) if service else None
    if job is not None and job.state == "running":
        return True
    return any(item.state == "running" for item in ACCOUNTS.jobs.values())


def start_updates():
    """Check for a newer Chatbox in the background. Off unless Settings says so."""
    def settings():
        try:
            return MEMORY.settings()
        except Exception:
            return {"auto_update": True, "auto_install_updates": False}

    def port():
        return CURRENT_PORT

    updatemod.start(DATA_DIR, HERE, settings, _updates_busy, port)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _stream_ndjson(self, events):
        """Write a stream of JSON lines. HTTP/1.0 so the browser reads until we close."""
        iterator = iter(events)
        try:
            first = next(iterator)
        except StopIteration:
            return self._send(200, "", "application/x-ndjson; charset=utf-8")
        except Exception as e:
            return self._send(200, json.dumps({"type": "error", "error": str(e)}) + "\n",
                              "application/x-ndjson; charset=utf-8")
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        def write(ev):
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        try:
            write(first)
            for ev in iterator:
                write(ev)
        except (BrokenPipeError, ConnectionResetError, OSError):
            iterator.close()

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
        ui = ui_file(self.path)
        if ui:
            body, ctype = ui
            return self._send(200, body, ctype, {"Cache-Control": "no-cache"})
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
                                              + [chatgpt.status(), ollama.status(ollama_url())]))
        if self.path == "/api/update":
            return self._send(200, json.dumps(updatemod.snapshot()))
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
                if req.get("service") == "chatgpt":
                    chatgpt.action(req.get("action"))
                else:
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
                        rel = v.save_chat(req.get("messages") or [], req.get("backend", ""),
                                          workdir=req.get("workdir") or "",
                                          project=req.get("project") or "",
                                          locked=bool(req.get("project_lock")))
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
        if self.path == "/api/sync" and req.get("action") == "move":
            root = syncmod.root_for(MEMORY)
            if not root:
                if not MEMORY.vault():
                    return self._send(200, json.dumps({"error": "Connect an Obsidian vault first (Accounts).",
                                                       **settings_payload()}))
                return self._send(200, json.dumps({"error": "Turn on Sync in Settings first.",
                                                   **settings_payload()}))
            try:
                moved = syncmod.backfill_local(MEMORY)
                if not moved.get("copied") and not moved.get("moved"):
                    moved = syncmod.move_existing(DATA_DIR, root)
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e), **settings_payload()}))
            return self._send(200, json.dumps({**moved, **settings_payload()}))
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
        if self.path == "/api/attach":
            try:
                meta = attachments.save(DATA_DIR, req.get("name"), req.get("type"), req.get("data"))
                return self._send(200, json.dumps(meta))
            except ValueError as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path == "/api/cancel":
            return self._send(200, json.dumps({"ok": streaming.cancel(str(req.get("turn_id") or ""))}))
        if self.path == "/api/update":
            action = req.get("action") or "check"
            if action == "apply":
                return self._send(200, json.dumps(updatemod.request_apply()))
            if action == "check":
                return self._send(200, json.dumps(updatemod.consider(auto=False)))
            return self._send(200, json.dumps({"error": "Unknown action"}))
        if self.path == "/api/open":
            try:
                accounts.open_url(req.get("url", ""))
                return self._send(200, '{"ok":true}')
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path != "/api/chat":
            return self._send(404, '{"error":"not found"}')
        self._stream_ndjson(chat_stream(req))


UI_DIR = os.path.join(runtime.source_root(), "ui")
_UI_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def ui_file(url_path):
    """Return (text, content-type) for a ui/ file, or None. Rejects paths outside ui/."""
    if url_path in ("/", "/index.html"):
        rel = "index.html"
    elif url_path.startswith("/ui/"):
        rel = url_path[len("/ui/"):]
    else:
        return None
    rel = os.path.normpath(rel)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    root = os.path.abspath(UI_DIR)
    full = os.path.abspath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    ctype = _UI_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
    with open(full, encoding="utf-8") as f:
        return f.read(), ctype


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    global CURRENT_PORT
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    CURRENT_PORT = srv.server_address[1]
    try:
        syncmod.prepare_vault(MEMORY)
    except Exception:
        pass
    start_updates()
    url = f"http://127.0.0.1:{CURRENT_PORT}/"
    print(f"Chatbox running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

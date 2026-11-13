"""Cheap sign-in checks for each assistant.

The Accounts screen runs the same tools with a long timeout and shows the account
name. This check only answers three questions: is the tool installed, is it signed
in, and what should the user run if not. It finishes in a few seconds, remembers
the answer for about a minute, and never returns command output or tokens.

Cursor is `cursor-agent status`, then `cursor-agent whoami` when status does not
say. Claude is `claude auth status`. Codex is `codex login status`. ChatGPT is the
local account file (the tokens in that file stay there). Ollama is a short request
to its server, http://localhost:11434 unless Settings point somewhere else.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import accounts
import chatgpt
import ollama
import runtime

CACHE_SECONDS = 60
CLI_TIMEOUT = 4
OLLAMA_TIMEOUT = 2
DEFAULT_OLLAMA = ollama.DEFAULT_URL  # http://localhost:11434
OUTPUT_LIMIT = 4096

# Fixed strings only. Command output is parsed and then thrown away.
FIELDS = ("id", "name", "installed", "signed_in", "state", "detail",
          "sign_in_command", "install_hint")
DETAILS = {
    "ready": "Signed in",
    "signed_out": "Not signed in",
    "missing": "Not installed",
    "unknown": "Couldn't check",
}
OLLAMA_DETAILS = {
    "ready": "Running",
    "signed_out": "Not running",
    "missing": "Not running",
    "unknown": "Couldn't check",
}
# The same commands Accounts runs for Sign in, so the row can show them.
SIGN_IN = {
    "cursor": "cursor-agent login",
    "claude": "claude auth login --claudeai",
    "codex": "codex login --device-auth",
    "chatgpt": "",
    "ollama": "",
}
ORDER = ("cursor", "claude", "codex", "chatgpt", "ollama")
NAMES = {"cursor": "Cursor", "claude": "Claude", "codex": "Codex",
         "chatgpt": "ChatGPT", "ollama": "Ollama"}
EXES = {"cursor": "cursor-agent", "claude": "claude", "codex": "codex"}

_NOT_IN = re.compile(r"not\s+(?:logged|signed)\s+in", re.I)
_LOGGED = re.compile(r"\blogged\s+in\b", re.I)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# A last check so a token that reached a label is dropped before it is sent.
_LEAK = re.compile(
    r"sk-[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{8,}|refresh_token|access_token|id_token|authorization\s*:",
    re.I)

_lock = threading.Lock()
_cache = None
_cache_key = None
_cache_at = 0.0


def invalidate():
    """Drop the remembered result. The next request checks again."""
    global _cache, _cache_key, _cache_at
    with _lock:
        _cache = None
        _cache_key = None
        _cache_at = 0.0


def _platform(system):
    if system in (None, ""):
        return "nt" if os.name == "nt" else "posix"
    if system in ("nt", "windows", "win32"):
        return "nt"
    return "posix"


def _public_url(url):
    """Host and port only. User names, passwords, and query strings are dropped."""
    try:
        parts = urllib.parse.urlsplit(ollama.clean_url(url))
    except ValueError:
        return DEFAULT_OLLAMA
    host = parts.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    port = ":" + str(parts.port) if parts.port else ""
    scheme = parts.scheme if parts.scheme in ("http", "https") else "http"
    return scheme + "://" + host + port


def _scrub(text):
    text = str(text or "")
    if _LEAK.search(text):
        return ""
    return text


def install_hint(sid, system=None, ollama_url=None):
    """Short install hint for this operating system. No tokens."""
    posix = _platform(system) != "nt"
    if sid == "cursor":
        if posix:
            return accounts.INSTALL["cursor"]
        return ("Install Cursor from https://cursor.com/download (Windows installer). "
                "Chatbox looks for cursor-agent on PATH.")
    if sid == "claude":
        if posix:
            return accounts.INSTALL["claude"]
        return "In PowerShell: irm https://claude.ai/install.ps1 | iex"
    if sid == "codex":
        if posix:
            return ("Install Codex from Accounts, or download a release from "
                    "https://github.com/openai/codex/releases into ~/.local/bin/codex.")
        return ("Install Codex from https://github.com/openai/codex/releases (Windows build). "
                "Chatbox looks for codex on PATH.")
    if sid == "ollama":
        public = _public_url(ollama_url or DEFAULT_OLLAMA)
        base = "Install Ollama from https://ollama.com/download and start it"
        if public in ("http://localhost:11434", "http://127.0.0.1:11434"):
            return base + " (http://localhost:11434)."
        return _scrub(base + ". Chatbox is checking " + public + ".")
    return ""


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _json_flag(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or "loggedIn" not in data:
        return None
    return bool(data.get("loggedIn"))


def _login_text(text):
    """True or False when the text says so. None when it does not.

    A JSON `loggedIn` flag wins: "not logged in" contains the words "logged in",
    and a status line must not be read as signed in because of that. The text
    itself is never returned.
    """
    cleaned = accounts.ANSI.sub("", text or "")
    flag = _json_flag(cleaned)
    if flag is not None:
        return flag
    if _NOT_IN.search(cleaned):
        return False
    if _LOGGED.search(cleaned):
        return True
    return None


def _safe_account(line):
    """An email or a short user name. Token-shaped lines are not an account."""
    line = (line or "").strip()
    if not line or len(line) > 80 or any(ch in line for ch in " \t/\\=:\"'"):
        return False
    low = line.lower()
    if low.startswith(("sk-", "eyj")) or "token" in low:
        return False
    if _EMAIL.match(line):
        return True
    return bool(_USER.match(line) and line.count(".") <= 1 and "@" not in line)


def _has_safe_account(text):
    cleaned = accounts.ANSI.sub("", text or "")
    return any(_safe_account(line) for line in cleaned.splitlines())


def run_cli(argv, timeout):
    """Run one status command. Returns (code, stdout, stderr).

    code is None when the command cannot be started or does not finish in time.
    On a timeout the output is discarded, so a half-printed token is not kept.
    stdout and stderr are capped and are only for the parser above this function.
    """
    try:
        proc = runtime.spawn(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except OSError:
        return None, "", ""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        runtime.kill_tree(proc)
        try:
            proc.communicate(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None, "", ""
    except OSError:
        return None, "", ""
    text_out = (out or b"")[:OUTPUT_LIMIT].decode("utf-8", "replace")
    text_err = (err or b"")[:OUTPUT_LIMIT].decode("utf-8", "replace")
    return proc.returncode, text_out, text_err


def _cursor_signed_in(exe, run, timeout):
    code, out, err = run([exe, "status"], timeout)
    state = _login_text((out or "") + "\n" + (err or ""))
    if state is not None:
        return state
    # Some builds have whoami and not status, or the other way around.
    code, out, err = run([exe, "whoami"], timeout)
    text = (out or "") + "\n" + (err or "")
    state = _login_text(text)
    if state is not None:
        return state
    if code == 0 and _has_safe_account(text):
        return True
    if code is None:
        return None
    if code != 0:
        return False
    return None


def _claude_signed_in(exe, run, timeout):
    code, out, err = run([exe, "auth", "status"], timeout)
    state = _login_text((out or "") + "\n" + (err or ""))
    if state is not None:
        return state
    if code is None:
        return None
    return False


def _codex_signed_in(exe, run, timeout):
    code, out, err = run([exe, "login", "status"], timeout)
    text = (out or "") + "\n" + (err or "")
    state = _login_text(text)
    if state is not None:
        return state
    if code == 0:
        return True
    if code is None:
        return None
    return False


def chatgpt_signed_in(path):
    """True when the local account file has a usable ChatGPT plan session.

    Reads the file and returns a boolean. Tokens are not copied out.
    A missing file is not an error: ChatGPT is built in, and the user is signed out.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("plan_usage") == "disabled":
        return False
    accounts_map = data.get("accounts")
    acc = None
    if isinstance(accounts_map, dict):
        raw = accounts_map.get(data.get("active") or "")
        if isinstance(raw, dict):
            acc = raw
    if not acc or acc.get("plan_usage") == "disabled":
        return False
    scopes = acc.get("scopes") or []
    if not isinstance(scopes, (list, tuple)) or chatgpt.PLAN_SCOPE not in scopes:
        return False
    if acc.get("refresh_token"):
        return True
    try:
        expires = float(acc.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0
    return bool(acc.get("access_token") and expires > time.time())


def probe_ollama(url, timeout):
    """True when Ollama answers at this address. The body is not kept."""
    target = ollama.clean_url(url) + "/api/tags"
    req = urllib.request.Request(target, method="GET")
    try:
        with ollama._opener.open(req, timeout=timeout) as resp:
            resp.read(1)
        return True
    except urllib.error.HTTPError:
        return True  # the server replied; it is reachable
    except Exception:
        return False


def _row(sid, installed, signed_in, state, system, ollama_url):
    detail = (OLLAMA_DETAILS if sid == "ollama" else DETAILS)[state]
    hint = "" if sid == "chatgpt" else install_hint(sid, system, ollama_url)
    row = {
        "id": sid,
        "name": NAMES[sid],
        "installed": bool(installed),
        "signed_in": bool(signed_in),
        "state": state,
        "detail": detail,
        "sign_in_command": SIGN_IN.get(sid, ""),
        "install_hint": _scrub(hint),
    }
    return {key: row[key] for key in FIELDS}


def _from_signed(exe, signed):
    if not exe:
        return False, False, "missing"
    if signed is True:
        return True, True, "ready"
    if signed is False:
        return True, False, "signed_out"
    return True, False, "unknown"


def check_backends(find, account_path, ollama_url, system=None, run=None, probe=None,
                   cli_timeout=CLI_TIMEOUT, ollama_timeout=OLLAMA_TIMEOUT):
    """Check every assistant once. `run` and `probe` stand in for the real tools in tests."""
    system = _platform(system)
    url = ollama.clean_url(ollama_url)
    runner = run or run_cli
    look = find or (lambda _name: None)
    prober = probe or probe_ollama
    results = {}

    def cli(sid):
        exe_name = EXES[sid]
        exe, signed = None, None
        try:
            exe = look(exe_name)
        except Exception:
            exe = None
        if exe:
            try:
                if sid == "cursor":
                    signed = _cursor_signed_in(exe, runner, cli_timeout)
                elif sid == "claude":
                    signed = _claude_signed_in(exe, runner, cli_timeout)
                else:
                    signed = _codex_signed_in(exe, runner, cli_timeout)
            except Exception:
                signed = None
        installed, signed_in, state = _from_signed(exe, signed)
        results[sid] = _row(sid, installed, signed_in, state, system, url)

    def chat():
        try:
            signed = chatgpt_signed_in(account_path)
        except Exception:
            signed = False
        state = "ready" if signed else "signed_out"
        results["chatgpt"] = _row("chatgpt", True, signed, state, system, url)

    def local():
        try:
            up = bool(prober(url, ollama_timeout))
        except Exception:
            up = False
        state = "ready" if up else "missing"
        results["ollama"] = _row("ollama", up, up, state, system, url)

    threads = [threading.Thread(target=cli, args=(sid,)) for sid in EXES]
    threads.append(threading.Thread(target=chat))
    threads.append(threading.Thread(target=local))
    for thread in threads:
        thread.daemon = True
        thread.start()
    # status then whoami is two waits, plus a moment to stop a slow child.
    limit = (float(cli_timeout) + 5) * 2 + float(ollama_timeout) + 2
    for thread in threads:
        thread.join(limit)
    rows = []
    for sid in ORDER:
        if sid in results:
            rows.append(results[sid])
        elif sid == "chatgpt":
            rows.append(_row(sid, True, False, "unknown", system, url))
        elif sid == "ollama":
            rows.append(_row(sid, False, False, "missing", system, url))
        else:
            rows.append(_row(sid, False, False, "unknown", system, url))
    return {"backends": rows}


def _copy(data):
    return {"backends": [dict(row) for row in data["backends"]]}


def snapshot(fresh=False, find=None, account_path="", ollama_url="", system=None, run=None,
             probe=None, cli_timeout=CLI_TIMEOUT, ollama_timeout=OLLAMA_TIMEOUT):
    """Cached `check_backends`. A new ChatGPT account file or Ollama address checks again.

    `fresh` skips the memory. Custom `run` / `probe` still share it, so tests that
    change the stand-in call `invalidate` first.
    """
    global _cache, _cache_key, _cache_at
    system_name = _platform(system)
    url = ollama.clean_url(ollama_url)
    key = (os.path.abspath(account_path) if account_path else "", _mtime(account_path),
           _public_url(url), system_name)
    now = time.monotonic()
    with _lock:
        if (not fresh and _cache is not None and _cache_key == key
                and now - _cache_at < CACHE_SECONDS):
            return _copy(_cache)
    data = check_backends(
        find, account_path, url, system=system_name, run=run, probe=probe,
        cli_timeout=cli_timeout, ollama_timeout=ollama_timeout)
    with _lock:
        _cache, _cache_key, _cache_at = data, key, time.monotonic()
    return _copy(data)

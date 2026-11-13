"""Ollama backend for Chatbox: chat with local models through Ollama's HTTP API.

Unlike the other assistants, Ollama is a server (default http://localhost:11434), not a
command-line tool, and it does not remember conversations. So Chatbox keeps each Ollama
conversation in <data>/ollama/<session_id>.json and sends the whole history every turn.
To the rest of Chatbox that looks just like a CLI's "--resume <session_id>".
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://localhost:11434"
SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)  # reasoning models (qwen3, deepseek-r1)
SYSTEM = ("You are a helpful assistant in the Chatbox chat window, running locally through Ollama. "
          "You cannot read files or run commands; just answer conversationally. "
          "Format answers in Markdown.")

# Talk to Ollama directly, never through a system HTTP proxy (it is usually on this computer).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def clean_url(url):
    url = (url or "").strip().rstrip("/") or DEFAULT_URL
    if not re.match(r"^https?://", url):
        url = "http://" + url
    return url


def _request(url, path, body=None, timeout=10):
    """GET (body=None) or POST JSON to Ollama and return the decoded JSON reply."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(clean_url(url) + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # Ollama explains errors as {"error": "..."}
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except ValueError:
            msg = str(e)
        raise RuntimeError(f"Ollama: {msg}") from None
    except socket.timeout:  # connected, but the model is still thinking
        raise TimeoutError from None
    except (urllib.error.URLError, OSError) as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            raise TimeoutError from None
        raise ConnectionError(f"Can't reach Ollama at {clean_url(url)} ({getattr(e, 'reason', e)}). "
                              "Is it running? Start the Ollama app, or run `ollama serve`.") from None


def _size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def list_models(url):
    """[[name, label], ...] of the models pulled into Ollama, most recently changed first."""
    try:
        data = _request(url, "/api/tags", timeout=5)
    except (ConnectionError, RuntimeError, TimeoutError):
        return []
    out = []
    for m in data.get("models", []):
        d = m.get("details") or {}
        extra = " · ".join(x for x in (d.get("parameter_size"), _size(m.get("size") or 0)) if x)
        out.append([m["name"], f"{m['name']}  ({extra})" if extra else m["name"]])
    return out


def status(url):
    """Shaped like an Accounts entry, so the page can show Ollama next to the others."""
    info = {"id": "ollama", "name": "Ollama", "plan": "Free · runs models on your computer",
            "installed": False, "logged_in": False, "account": None, "job": None,
            "url": clean_url(url), "models": 0}
    try:
        data = _request(url, "/api/tags", timeout=3)
    except (ConnectionError, RuntimeError, TimeoutError) as e:
        info["error"] = str(e)
        return info
    info["installed"] = True
    info["models"] = len(data.get("models", []))
    info["logged_in"] = info["models"] > 0  # "ready" = running and has at least one model
    info["account"] = f"{info['models']} model{'s' if info['models'] != 1 else ''}"
    return info


class Stopped(Exception):
    """The user cancelled this Ollama request."""


class ThinkFilter:
    """Drop <think>…</think> blocks, including when the tags arrive split across chunks."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.skipping = False

    def feed(self, chunk):
        self.buf += chunk or ""
        out = []
        while self.buf:
            if self.skipping:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-len(self.CLOSE):]
                    break
                self.buf = self.buf[i + len(self.CLOSE):]
                self.skipping = False
                continue
            i = self.buf.find(self.OPEN)
            if i < 0:
                keep = 0
                for k in range(1, len(self.OPEN)):
                    if self.buf.endswith(self.OPEN[:k]):
                        keep = k
                        break
                out.append(self.buf[:-keep] if keep else self.buf)
                self.buf = self.buf[-keep:] if keep else ""
                break
            out.append(self.buf[:i])
            self.buf = self.buf[i + len(self.OPEN):]
            self.skipping = True
        return "".join(out)

    def flush(self):
        if self.skipping:
            self.buf = ""
            return ""
        text, self.buf = self.buf, ""
        return text


class Sessions:
    """Conversation history per Ollama session, one JSON file each."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "ollama")
        self.lock = threading.Lock()

    def _path(self, sid):
        return os.path.join(self.dir, sid + ".json")

    def load(self, sid):
        if not sid or not SESSION_ID.match(sid):
            return []
        try:
            with open(self._path(sid), encoding="utf-8") as f:
                return json.load(f).get("messages", [])
        except (OSError, ValueError):
            return []

    def save(self, sid, messages):
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            path = self._path(sid)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"messages": messages}, f, ensure_ascii=False)
            os.replace(path + ".tmp", path)


def _check_stop(turn):
    if turn is not None and turn.cancel.is_set():
        raise Stopped()


def supports_vision(url, model):
    """True, False, or None when this Ollama server doesn't say."""
    if not model:
        return None
    try:
        data = _request(url, "/api/show", body={"model": model}, timeout=5)
    except (ConnectionError, RuntimeError, TimeoutError):
        return None
    caps = data.get("capabilities")
    if isinstance(caps, list) and caps:
        return "vision" in caps
    family = str((data.get("details") or {}).get("family") or "").lower()
    if any(word in family for word in ("llava", "clip", "moondream", "vision")):
        return True
    return None


def stream_turn(url, sessions, message, session_id, model, turn, timeout, images=None):
    """Yield ('delta', text) chunks. Return {"reply", "session_id", "usage"} or {"error"}."""
    if not model:  # "Auto": the most recently pulled / changed model
        models = list_models(url)
        if not models:
            try:
                _request(url, "/api/tags", timeout=3)
            except (ConnectionError, RuntimeError, TimeoutError) as e:
                return {"error": str(e) or "Ollama did not answer."}
            return {"error": "Ollama has no models yet. Download one in a terminal, for example:\n\n"
                             "ollama pull llama3.2\n\nthen pick it in the Model menu."}
        model = models[0][0]
    history = sessions.load(session_id)
    history.append({"role": "user", "content": message})
    sent = [dict(m) for m in history]
    if images:
        sent[-1]["images"] = list(images)  # this turn only; saved history stays text
    body = {"model": model, "stream": True,
            "messages": [{"role": "system", "content": SYSTEM}] + sent}
    data = json.dumps(body).encode()
    req = urllib.request.Request(clean_url(url) + "/api/chat", data=data,
                                 headers={"Content-Type": "application/json"})
    filt = ThinkFilter()
    parts = []
    usage = {"model": model}
    started = time.monotonic()
    try:
        resp = _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except ValueError:
            msg = str(e)
        return {"error": f"Ollama: {msg}"}
    except socket.timeout:
        return {"error": f"No reply after {timeout} seconds."}
    except (urllib.error.URLError, OSError) as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            return {"error": f"No reply after {timeout} seconds."}
        return {"error": f"Can't reach Ollama at {clean_url(url)} ({getattr(e, 'reason', e)}). "
                         "Is it running? Start the Ollama app, or run `ollama serve`."}
    if turn is not None:
        turn.resp = resp
    try:
        _check_stop(turn)
        while True:
            _check_stop(turn)
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"No reply after {timeout} seconds.")
            line = resp.readline()
            if not line:
                break
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("error"):
                return {"error": f"Ollama: {ev['error']}"}
            piece = filt.feed(((ev.get("message") or {}).get("content")) or "")
            if piece:
                parts.append(piece)
                yield ("delta", piece)
            if ev.get("done"):
                tail = filt.flush()
                if tail:
                    parts.append(tail)
                    yield ("delta", tail)
                usage = {"input": ev.get("prompt_eval_count", 0), "output": ev.get("eval_count", 0),
                         "model": ev.get("model") or model}
                break
    except Stopped:
        raise
    except socket.timeout:
        raise TimeoutError from None
    except (urllib.error.URLError, OSError) as e:
        if turn is not None and turn.cancel.is_set():
            raise Stopped() from None
        if isinstance(getattr(e, "reason", None), socket.timeout):
            raise TimeoutError from None
        raise ConnectionError(f"Can't reach Ollama at {clean_url(url)} ({getattr(e, 'reason', e)}). "
                              "Is it running? Start the Ollama app, or run `ollama serve`.") from None
    finally:
        if turn is not None:
            turn.resp = None
        try:
            resp.close()
        except OSError:
            pass
    reply = "".join(parts).strip()
    history.append({"role": "assistant", "content": reply})
    if len(history) == 2:  # first turn (or the old history was lost): new session
        session_id = uuid.uuid4().hex
    sessions.save(session_id, history)
    return {"reply": reply, "session_id": session_id, "usage": usage}


def run_turn(url, sessions, message, session_id, model, timeout):
    """One chat turn. Returns {"reply", "session_id", "usage"} or {"error", ...}."""
    gen = stream_turn(url, sessions, message, session_id, model, None, timeout)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value or {"error": "No reply."}
    except (ConnectionError, RuntimeError, TimeoutError, Stopped) as e:
        # No session_id: a failed first turn must not count as a started conversation.
        return {"error": str(e) or f"No reply after {timeout} seconds."}

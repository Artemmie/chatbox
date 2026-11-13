"""Streaming turns for Chatbox.

CLI backends speak newline-delimited JSON. This module runs the process, parses
events as they arrive, and yields text deltas. Where a backend can only report
the finished message, the same parsers emit that message once — the caller does
not need a second code path.

A turn can be cancelled (the child is killed) from another thread.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time

TOTAL_TIMEOUT = int(os.environ.get("CHATBOX_TIMEOUT", "600"))
# Silence longer than this, before any output, means the CLI never started talking.
START_TIMEOUT = int(os.environ.get("CHATBOX_START_TIMEOUT", "180"))
# Silence after output has started. Long tool runs can be quiet, so this stays generous.
IDLE_TIMEOUT = int(os.environ.get("CHATBOX_IDLE_TIMEOUT", "300"))


class Cancelled(Exception):
    """The user stopped this turn."""


class TurnTimeout(Exception):
    def __init__(self, kind, seconds):
        self.kind = kind  # "total", "start", or "idle"
        self.seconds = seconds
        super().__init__(kind)


class Turn:
    def __init__(self, tid):
        self.id = tid
        self.cancel = threading.Event()
        self.proc = None
        self.resp = None  # open HTTP response (Ollama / ChatGPT), closed on cancel
        self.stderr = ""
        self.returncode = None

    def kill(self):
        self.cancel.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            _kill_proc(proc)
        resp = self.resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


_turns = {}
_cancelled = set()
_lock = threading.Lock()


def register(turn):
    with _lock:
        _turns[turn.id] = turn
        if turn.id in _cancelled:
            turn.kill()


def cancel(tid):
    """Stop a turn. A cancel that arrives before the turn is registered still stops it."""
    if not tid or not isinstance(tid, str):
        return False
    with _lock:
        _cancelled.add(tid)
        turn = _turns.get(tid)
    if turn:
        turn.kill()
    return True


def finish(turn):
    with _lock:
        _turns.pop(turn.id, None)
        _cancelled.discard(turn.id)


def active(tid):
    with _lock:
        return tid in _turns


def any_active():
    with _lock:
        return bool(_turns)


def _kill_proc(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def read_stdout(cmd, cwd, turn, total=None, start_timeout=None, idle_timeout=None):
    """Yield stdout lines. Raises Cancelled or TurnTimeout. Fills turn.stderr."""
    total = TOTAL_TIMEOUT if total is None else total
    start_timeout = START_TIMEOUT if start_timeout is None else start_timeout
    idle_timeout = IDLE_TIMEOUT if idle_timeout is None else idle_timeout
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True)
    turn.proc = proc
    err_box = []

    def read_err():
        try:
            err_box.append(proc.stderr.read())
        except OSError:
            pass

    q = queue.Queue()

    def read_out():
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                q.put(line)
        except OSError as e:
            q.put(e)
        finally:
            q.put(None)

    err_thread = threading.Thread(target=read_err, daemon=True)
    err_thread.start()
    threading.Thread(target=read_out, daemon=True).start()
    started = time.monotonic()
    last = started
    saw_output = False
    try:
        while True:
            if turn.cancel.is_set():
                turn.kill()
                raise Cancelled()
            now = time.monotonic()
            if now - started > total:
                turn.kill()
                raise TurnTimeout("total", total)
            quiet_limit = idle_timeout if saw_output else start_timeout
            if now - last > quiet_limit:
                turn.kill()
                raise TurnTimeout("idle" if saw_output else "start", quiet_limit)
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item, Exception):
                raise item
            if item is None:
                if turn.cancel.is_set():
                    raise Cancelled()
                break
            saw_output = True
            last = time.monotonic()
            yield item.decode("utf-8", "replace")
        if turn.cancel.is_set():
            raise Cancelled()
    finally:
        if proc.poll() is None:
            _kill_proc(proc)
        else:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        err_thread.join(timeout=2)
        turn.stderr = b"".join(err_box).decode("utf-8", "replace")
        turn.returncode = proc.returncode


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


def _message_text(ev):
    msg = ev.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                parts.append(part.get("text") or "")
        return "".join(parts)
    return ""


def _loads(line):
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def _extend(buf, text):
    """Append text, or only the new suffix when text is a longer snapshot of buf.

    Returns (new_buf, delta).
    """
    if not text:
        return buf, ""
    if not buf:
        return text, text
    if text.startswith(buf):
        return text, text[len(buf):]
    if buf.startswith(text):
        return buf, ""  # older snapshot of what we already showed
    return buf + text, text


class CodexParser:
    """codex exec --json. Agent text usually arrives whole in item.completed.

    item.updated with a growing agent_message is also accepted, so a build that
    streams snapshots still shows text early. The saved reply is the last agent
    message, matching the previous non-streaming parser.
    """

    def __init__(self):
        self.session_id = None
        self.reply = None
        self.error = None
        self.usage = {}
        self.visible = ""
        self.saw_terminal = False

    def feed_line(self, line):
        ev = _loads(line)
        return self.feed(ev) if ev else []

    def feed(self, ev):
        out = []
        t = ev.get("type")
        if t == "thread.started":
            self.session_id = ev.get("thread_id") or self.session_id
        elif t in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item") or {}
            kind = item.get("type")
            if kind == "agent_message" and t != "item.started":
                text = item.get("text") or ""
                if not self.visible or text.startswith(self.visible):
                    extra = text[len(self.visible):]
                    self.visible = text
                    if extra:
                        out.append(("delta", extra))
                elif text != self.visible:
                    # A later agent message replaces commentary; the saved reply is the last one.
                    self.visible = text
                    out.append(("replace", text))
                if t == "item.completed":
                    self.reply = text
            elif kind == "command_execution" and t == "item.started":
                cmd = (item.get("command") or "").strip()
                if cmd:
                    out.append(("status", "Running " + cmd[:160]))
        elif t == "turn.completed":
            self.saw_terminal = True
            u = ev.get("usage") or {}
            self.usage = {"input": u.get("input_tokens", 0), "cache_read": u.get("cached_input_tokens", 0),
                          "output": u.get("output_tokens", 0), "reasoning": u.get("reasoning_output_tokens", 0)}
        elif t in ("error", "turn.failed"):
            self.saw_terminal = True
            self.error = _codex_error(ev)
        return out

    def finish(self):
        if self.reply is not None:
            return {"reply": self.reply, "session_id": self.session_id, "usage": self.usage}
        if self.error:
            return {"error": self.error, "session_id": self.session_id, "usage": self.usage}
        return None


def _codex_error(ev):
    msg = ev.get("message") or (ev.get("error") or {}).get("message", "Error")
    if isinstance(msg, dict):
        msg = msg.get("message") or "Error"
    try:
        msg = json.loads(msg)["error"]["message"]
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return str(msg)


class ClaudeParser:
    """claude -p --output-format stream-json, with or without partial messages.

    text_delta events are real token chunks. Assistant events (the whole block)
    are used only when no token chunks arrived. A final result event is authoritative.
    """

    def __init__(self):
        self.session_id = None
        self.buf = ""
        self.saw_delta = False
        self.result = None
        self.error = None
        self.is_error = False
        self.usage = {}
        self.saw_terminal = False

    def feed_line(self, line):
        ev = _loads(line)
        return self.feed(ev) if ev else []

    def feed(self, ev):
        out = []
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        t = ev.get("type")
        if t == "stream_event":
            event = ev.get("event") or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                if text:
                    self.saw_delta = True
                    self.buf += text
                    out.append(("delta", text))
        elif t == "assistant" and not self.saw_delta:
            self.buf, extra = _extend(self.buf, _message_text(ev))
            if extra:
                out.append(("delta", extra))
        elif t == "result" or (t is None and "result" in ev):
            self.saw_terminal = True
            self.result = ev.get("result") or ""
            self.is_error = bool(ev.get("is_error"))
            self.usage = json_usage(ev)
            if self.is_error:
                self.error = self.result or ev.get("error") or "Error"
            elif not self.saw_delta:
                out.extend(self._adopt_final(self.result))
        elif t == "error":
            self.saw_terminal = True
            err = ev.get("error")
            if isinstance(err, dict):
                self.error = err.get("message") or "Error"
            else:
                self.error = err or ev.get("message") or "Error"
        return out

    def _adopt_final(self, text):
        """Show a finished reply without repeating text already streamed."""
        if not text:
            return []
        if not self.buf or text.startswith(self.buf):
            extra = text[len(self.buf):]
            self.buf = text
            return [("delta", extra)] if extra else []
        if text != self.buf:
            self.buf = text
            return [("replace", text)]
        return []

    def finish(self):
        if self.is_error:
            return {"error": self.error or self.result or "Error", "session_id": self.session_id,
                    "usage": self.usage}
        if self.error and self.result is None and not self.buf:
            return {"error": self.error, "session_id": self.session_id, "usage": self.usage}
        if self.result is None and not self.buf:
            return None
        reply = self.result if isinstance(self.result, str) else self.buf
        return {"reply": reply or "", "session_id": self.session_id, "usage": self.usage}


class CursorParser:
    """cursor-agent --output-format stream-json.

    With --stream-partial-output, keep assistant events that carry timestamp_ms
    and no model_call_id (the others repeat text). Chunks may be deltas or
    growing snapshots; both are handled. Without partial output, each assistant
    event is one segment.
    """

    def __init__(self):
        self.session_id = None
        self.buf = ""
        self.saw_partial = False
        self.result = None
        self.error = None
        self.is_error = False
        self.usage = {}
        self.saw_terminal = False

    def feed_line(self, line):
        ev = _loads(line)
        return self.feed(ev) if ev else []

    def feed(self, ev):
        out = []
        t = ev.get("type")
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        if t == "assistant":
            if "model_call_id" in ev:
                return []
            if self.saw_partial and "timestamp_ms" not in ev:
                return []
            text = _message_text(ev)
            if "timestamp_ms" in ev:
                self.saw_partial = True
            self.buf, extra = _extend(self.buf, text)
            if extra:
                out.append(("delta", extra))
        elif t == "tool_call" and ev.get("subtype") == "started":
            label = _cursor_tool(ev)
            if label:
                out.append(("status", label))
        elif t == "result" or (t is None and "result" in ev and "message" not in ev):
            self.saw_terminal = True
            self.result = ev.get("result") or ""
            self.is_error = bool(ev.get("is_error"))
            self.usage = json_usage(ev)
            if self.is_error:
                self.error = self.result or "Error"
            else:
                out.extend(self._adopt_final(self.result))
        return out

    def _adopt_final(self, text):
        if not text:
            return []
        if not self.buf or text.startswith(self.buf):
            extra = text[len(self.buf):]
            self.buf = text
            return [("delta", extra)] if extra else []
        if text != self.buf:
            self.buf = text
            return [("replace", text)]
        return []

    def finish(self):
        if self.is_error:
            return {"error": self.error or self.result or "Error", "session_id": self.session_id,
                    "usage": self.usage}
        if self.result is None and not self.buf:
            return None
        reply = self.result if isinstance(self.result, str) else self.buf
        return {"reply": reply or "", "session_id": self.session_id, "usage": self.usage}


def _cursor_tool(ev):
    tc = ev.get("tool_call") or {}
    if not isinstance(tc, dict):
        return "Working"
    for key, val in tc.items():
        if isinstance(val, dict):
            cmd = val.get("command") or val.get("cmd")
            if cmd:
                return "Running " + str(cmd)[:160]
        if key not in ("id", "call_id"):
            return "Using " + key.replace("_", " ")
    return "Working"


def unknown_flag(err):
    low = (err or "").lower()
    return any(s in low for s in (
        "unknown option", "unrecognized option", "unexpected argument",
        "unknown flag", "flag provided but not defined", "unknown command",
        "unknown argument"))


PARSERS = {"claude": ClaudeParser, "cursor": CursorParser, "codex": CodexParser, "chatgpt": CodexParser}

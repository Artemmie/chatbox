"""Sync chats and memories through a folder in the user's Obsidian vault.

Off unless Settings has sync enabled and a vault is connected. Markdown in that
folder is the copy both computers share. Chat notes are named with the date and
title, and Chatbox.md links them together for the graph. Sign-in tokens and
attachment bytes stay in the local data directory.
"""
import copy
import hashlib
import json
import os
import re
import socket
import threading
import time

CHAT_ID = re.compile(r"^[a-f0-9]{12}$")
MEM_ID = re.compile(r"^[a-f0-9]{12}$")
SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "code_verifier", "code",
    "token", "password", "api_key", "authorization", "secret", "credentials",
}
HEADINGS = {
    "You": ("you", None),
    "Note": ("note", None),
    "Plan": ("plan", None),
    "Cursor": ("bot", "cursor"),
    "Claude": ("bot", "claude"),
    "Codex": ("bot", "codex"),
    "ChatGPT": ("bot", "chatgpt"),
    "Ollama": ("bot", "ollama"),
    "Assistant": ("bot", None),
}
NAMES = {v: k for k, (_, v) in HEADINGS.items() if v}
RESUME = re.compile(
    r"(?:<!--chatbox-resume\n(.*?)\n-->|%%\s*chatbox-resume\n(.*?)\n%%)\s*\Z",
    re.DOTALL,
)
HEADING = re.compile(
    r"(?m)^#{2,3} (You|Cursor|Claude|Codex|ChatGPT|Ollama|Note|Plan|Assistant)"
    r"(?: · ([^\n]+))?[ \t]*\n"
)
LEGACY_NAME = re.compile(r"^[a-f0-9]{12}\.md$")
LOCAL_STAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2})(\.\d+)?)?$"
)
UNSAFE_TITLE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
SKIP_SUFFIX = (".tmp", ".temp", ".crdownload", ".download", ".partial", ".icloud")
ROOT_NOTE = "Chatbox.md"
ROOT_LINK = "[[Chatbox]]"
PART_OF = "Part of [[Chatbox]]"
INDEX_BEGIN = "%% chatbox:index %%"
INDEX_END = "%% /chatbox:index %%"
_INDEX_BEGIN = re.compile(r"(?:<!--\s*chatbox:index\s*-->|%%\s*chatbox:index\s*%%)")
_INDEX_END = re.compile(r"(?:<!--\s*/chatbox:index\s*-->|%%\s*/chatbox:index\s*%%)")


def machine_name():
    name = socket.gethostname().split(".")[0]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "computer"
    return name[:40]


def clean_rel(folder):
    """A vault-relative folder. Rejects absolute paths and '..'."""
    raw = (folder or "Chatbox").strip()
    if raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("Sync folder must stay inside the vault.")
    folder = raw.replace("\\", "/").strip("/")
    parts = []
    for part in folder.split("/"):
        if not part or part == ".":
            continue
        if part == ".." or part.startswith("/") or ":" in part:
            raise ValueError("Sync folder must stay inside the vault.")
        parts.append(part)
    if not parts:
        raise ValueError("Sync folder must stay inside the vault.")
    return "/".join(parts)


def inside(parent, child):
    parent = os.path.realpath(parent)
    child = os.path.realpath(child)
    if child != parent and not child.startswith(parent + os.sep):
        raise ValueError("Sync folder must stay inside the vault.")
    return child


def root_for(store):
    """Absolute sync folder, or None when sync is off or the vault is missing."""
    try:
        settings = store.settings()
    except Exception:
        return None
    if not settings.get("sync_chats"):
        return None
    vault = store.vault()
    if not vault:
        return None
    try:
        rel = clean_rel(settings.get("sync_folder") or "Chatbox")
        return inside(vault.path, os.path.join(vault.path, *rel.split("/")))
    except ValueError:
        return None


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key) in SECRET_KEYS:
                continue
            out[key] = _scrub(item)
        return out
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _scrub_files(item):
    files = item.get("files")
    if not isinstance(files, list):
        return
    clean = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        kept = {k: v for k, v in entry.items() if k in ("name", "kind")}
        if kept.get("name"):
            clean.append(kept)
    if clean:
        item["files"] = clean
    else:
        item.pop("files", None)


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _skip_name(name):
    if not name or name.startswith("."):
        return True
    low = name.lower()
    return low.endswith(SKIP_SUFFIX)


def _quote(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _parse_local_datetime(text):
    """Seconds since the epoch for a local 'YYYY-MM-DD HH:MM' string, or None."""
    matched = LOCAL_STAMP.match(str(text or "").strip())
    if not matched:
        return None
    day, hm, sec, frac = matched.groups()
    try:
        parsed = time.strptime(f"{day} {hm}:{sec or '00'}", "%Y-%m-%d %H:%M:%S")
        seconds = time.mktime(parsed)
    except (OverflowError, ValueError):
        return None
    if frac:
        seconds += float(frac)
    return seconds


def _as_seconds(value):
    """Unix seconds from ms epoch, seconds epoch, or a local datetime string."""
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1]
        parsed = _parse_local_datetime(text)
        if parsed is not None:
            return parsed
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    # Date.now() is milliseconds (~1e12). time.time() is seconds (~1e9).
    if value >= 1e11:
        return float(value) / 1000.0
    return float(value)


def _format_stamp(value):
    """Local time, e.g. 2026-11-13 15:23:41. Missing values stay the epoch."""
    seconds = _as_seconds(value)
    if seconds is None:
        seconds = 0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


def _parse_front_stamp(raw, milliseconds):
    """Keep an old numeric frontmatter value. Parse a datetime into ms or seconds."""
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    try:
        return float(text)
    except ValueError:
        pass
    seconds = _parse_local_datetime(text)
    if seconds is None:
        return None
    return seconds * 1000.0 if milliseconds else seconds


def _safe_title(title, limit=60):
    text = UNSAFE_TITLE.sub(" ", str(title or ""))
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:
        text = text[:limit].rstrip(" .")
    return text or "Chat"


def _day_prefix(chat):
    seconds = _as_seconds(chat.get("created"))
    if seconds is None:
        seconds = _as_seconds(chat.get("updated"))
    if seconds is None:
        seconds = time.time()
    return time.strftime("%Y-%m-%d", time.localtime(seconds))


def chat_filename(chat, suffix=""):
    """'<YYYY-MM-DD> <title>.md', with a short suffix only when the caller asks."""
    extra = f" ({suffix})" if suffix else ""
    return f"{_day_prefix(chat)} {_safe_title(chat.get('title'))}{extra}.md"


def _heading_name(item):
    """Display name, and a model suffix for assistant turns."""
    role = item.get("r")
    if role == "you":
        return "You", None
    if role == "note":
        return "Note", None
    if role == "plan":
        return "Plan", None
    base = NAMES.get(item.get("b"), "Assistant")
    model = item.get("model")
    if isinstance(model, str):
        model = re.sub(r"\s+", " ", model).strip(" ·")
        if model and model.lower() != base.lower():
            return base, model[:80]
    return base, None


def _line_needs_escape(line):
    return (line.startswith("## ") or line.startswith("### ")
            or line.startswith("\\## ") or line.startswith("\\### ")
            or line.startswith("<!--chatbox") or line.startswith("\\<!--chatbox")
            or line.startswith("%% chatbox") or line.startswith("%%chatbox")
            or line.startswith("\\%% chatbox") or line.startswith("\\%%chatbox"))


def _escape_body(text):
    lines = []
    for line in str(text or "").split("\n"):
        if _line_needs_escape(line):
            lines.append("\\" + line)
        else:
            lines.append(line)
    return "\n".join(lines)


def _unescape_body(text):
    lines = []
    for line in text.split("\n"):
        if (line.startswith("\\## ") or line.startswith("\\### ")
                or line.startswith("\\<!--chatbox")
                or line.startswith("\\%% chatbox") or line.startswith("\\%%chatbox")):
            lines.append(line[1:])
        else:
            lines.append(line)
    return "\n".join(lines)


def _take_meta(section):
    """Hidden per-message metadata, in the current %% form or the old HTML comment."""
    for pattern in (r"^<!--chatbox (\{.*\})-->\n?", r"^%%\s*chatbox (\{.*\})\s*%%\n?"):
        matched = re.match(pattern, section)
        if not matched:
            continue
        try:
            extra = json.loads(matched.group(1))
        except ValueError:
            continue
        if isinstance(extra, dict):
            return extra, section[matched.end():]
    return {}, section


def render_chat(chat):
    """Markdown note a person can read. Session ids stay in a hidden resume block."""
    chat = _scrub(copy.deepcopy(chat))
    lines = ["---",
             "id: " + _quote(chat.get("id") or ""),
             "title: " + _quote(chat.get("title") or "Chat"),
             "backend: " + _quote(chat.get("backend") or ""),
             "created: " + _format_stamp(chat.get("created")),
             "updated: " + _format_stamp(chat.get("updated")),
             "up: " + _quote(ROOT_LINK),
             "---", "",
             PART_OF, ""]
    title = chat.get("title") or "Chat"
    lines.append("# " + title)
    lines.append("")
    for item in chat.get("items") or []:
        if not isinstance(item, dict) or item.get("temp"):
            continue
        _scrub_files(item)
        base, model = _heading_name(item)
        lines.append("### " + (f"{base} · {model}" if model else base))
        lines.append("")
        meta = {}
        for key in ("r", "b", "mode", "model", "u", "group", "path", "memId", "files"):
            if item.get(key) not in (None, "", [], {}):
                meta[key] = item[key]
        default_role, default_b = HEADINGS.get(base, ("bot", None))
        if meta.get("r") == default_role:
            meta.pop("r", None)
        if meta.get("b") == default_b:
            meta.pop("b", None)
        if meta:
            lines.append("%% chatbox " + json.dumps(meta, ensure_ascii=False) + " %%")
            lines.append("")
        body = item.get("t")
        if body is None and item.get("r") == "plan":
            body = item.get("path") or ""
        lines.append(_escape_body(body or "").rstrip())
        lines.append("")
    resume = {}
    for key in ("sessions", "ctx", "sent", "workdir"):
        value = chat.get(key)
        if value not in (None, "", {}, []):
            resume[key] = value
    text = "\n".join(lines).rstrip() + "\n"
    if resume:
        text += "\n%% chatbox-resume\n" + json.dumps(resume, ensure_ascii=False) + "\n%%\n"
    return text


def parse_chat(text):
    """The chat dict, or None if this isn't a finished Chatbox note."""
    if not text:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None  # still syncing, or not our file
    meta = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key, raw = key.strip(), raw.strip()
        if key in ("created", "updated"):
            parsed = _parse_front_stamp(raw, milliseconds=(key == "created"))
            if parsed is not None:
                meta[key] = parsed
                continue
        try:
            meta[key] = json.loads(raw)
        except ValueError:
            meta[key] = raw.strip('"')
    if not CHAT_ID.match(str(meta.get("id") or "")):
        return None
    body = text[end + 5:]
    resume = {}
    found = RESUME.search(body)
    if found:
        raw = found.group(1) if found.group(1) is not None else found.group(2)
        try:
            resume = json.loads(raw)
        except ValueError:
            resume = {}
        body = body[:found.start()]
    items = []
    # Anything before the first message heading is the title and the root link.
    for heading, heading_model, section in _sections(body):
        role, backend = HEADINGS.get(heading, ("bot", None))
        section = section.lstrip("\n")
        extra, section = _take_meta(section)
        text_body = _unescape_body(section).strip()
        item = {"r": extra.get("r") or role, "t": text_body}
        if extra.get("b") or backend:
            item["b"] = extra.get("b") or backend
        for key in ("mode", "model", "u", "group", "path", "memId", "files"):
            if key in extra:
                item[key] = extra[key]
        if "model" not in item and heading_model:
            item["model"] = heading_model.strip()
        if item["r"] == "plan" and not item.get("path"):
            item["path"] = text_body
        items.append(item)
    chat = {"id": meta["id"], "title": meta.get("title") or "Chat",
            "backend": meta.get("backend") or "",
            "created": meta.get("created") or 0, "updated": meta.get("updated") or 0,
            "items": items}
    if isinstance(resume, dict):
        for key in ("sessions", "ctx", "sent", "workdir"):
            if key in resume:
                chat[key] = resume[key]
    return chat


def _sections(body):
    matches = list(HEADING.finditer(body))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        model = match.group(2) or ""
        yield match.group(1), model.strip(), body[start:end]


def render_memories(items):
    lines = ["# Chatbox Memories", "",
             PART_OF, "",
             "Memories shared with every assistant. Each bullet is one memory. "
             "You can edit the words; leave the id at the start alone.", ""]
    for item in items:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        mid = item.get("id") or ""
        if not MEM_ID.match(str(mid)):
            mid = hashlib.sha1(item["text"].encode()).hexdigest()[:12]
        extra = ""
        source = item.get("source") or ""
        created = item.get("created") or ""
        if source or created:
            extra = " <!--" + " ".join(part for part in (source, created) if part) + "-->"
        text = str(item["text"]).replace("\n", " ")
        lines.append(f"- `{mid}` {text}{extra}")
    return "\n".join(lines).rstrip() + "\n"


def parse_memories(text):
    if text is None:
        return None
    items = []
    seen = set()
    for line in text.splitlines():
        matched = re.match(r"^\s*[-*]\s+`([a-f0-9]{12})`\s+(.+?)\s*$", line)
        source, created = "", ""
        if matched:
            mid, rest = matched.group(1), matched.group(2)
        else:
            plain = re.match(r"^\s*[-*]\s+(?!\[[ xX]\])(.+?)\s*$", line)
            if not plain:
                continue
            rest = plain.group(1)
            mid = hashlib.sha1(rest.encode()).hexdigest()[:12]
        comment = re.search(r"<!--([^>]*)-->\s*$", rest)
        if comment:
            bits = comment.group(1).split()
            if bits:
                source = bits[0]
            if len(bits) > 1:
                created = bits[1]
            rest = rest[:comment.start()].strip()
        if not rest or mid in seen:
            continue
        seen.add(mid)
        items.append({"id": mid, "text": rest, "source": source or "obsidian", "created": created})
    return items


def memories_path(root):
    return os.path.join(root, "Memories.md")


def memories_exist(root):
    return os.path.isfile(memories_path(root))


def read_memories(root):
    path = memories_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_memories(handle.read())
    except OSError:
        return []


def _sigs(items):
    return [(_item_sig(item)) for item in items or []]


def _item_sig(item):
    files = item.get("files") if isinstance(item, dict) else None
    names = tuple(entry.get("name") for entry in files if isinstance(entry, dict)) if isinstance(files, list) else ()
    if not isinstance(item, dict):
        return ("", "", "", "", names)
    return (item.get("r"), item.get("b"), item.get("t"), item.get("path"), names)


def _is_prefix(short, long):
    if len(short) > len(long):
        return False
    return _sigs(short) == _sigs(long)[:len(short)]


def merge_items(base, ours, theirs):
    """Return (items, conflict). Append-only divergence merges; other edits do not."""
    if _sigs(ours) == _sigs(theirs):
        return ours, False
    if base is not None and _sigs(theirs) == _sigs(base):
        return ours, False
    if base is not None and _sigs(ours) == _sigs(base):
        return theirs, False
    if _is_prefix(ours, theirs):
        return theirs, False
    if _is_prefix(theirs, ours):
        return ours, False
    shared = 0
    while shared < len(ours) and shared < len(theirs) and _item_sig(ours[shared]) == _item_sig(theirs[shared]):
        shared += 1
    if shared and (base is None or shared >= len(base)):
        tail = theirs[shared:]
        seen = {_item_sig(item) for item in tail}
        extra = [item for item in ours[shared:] if _item_sig(item) not in seen]
        return theirs[:shared] + tail + extra, False
    return None, True


class Library:
    """One sync folder. Remembers what it last read so it won't clobber another computer."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.by_id = {}
        self.base = {}
        self.conflict_hash = {}
        self.memory_hash = None

    def _chats(self):
        return os.path.join(self.root, "Chats")

    def refresh(self):
        folder = self._chats()
        if not os.path.isdir(folder):
            return
        seen = set()
        for name in os.listdir(folder):
            if _skip_name(name) or not name.endswith(".md"):
                continue
            path = os.path.join(folder, name)
            try:
                if not os.path.isfile(path):
                    continue
                inside(self.root, path)
                stat = os.stat(path)
            except (OSError, ValueError):
                continue
            if stat.st_size == 0:
                continue
            legacy = bool(LEGACY_NAME.match(os.path.basename(path)))
            known = next((row for row in self.by_id.values() if row.get("path") == path), None)
            if known and not legacy and known.get("mtime_ns") == stat.st_mtime_ns and known.get("size") == stat.st_size:
                chat = known["chat"]
                row_hash = known.get("hash")
            else:
                try:
                    with open(path, encoding="utf-8") as handle:
                        text = handle.read()
                except OSError:
                    continue
                chat = parse_chat(text)
                if not chat:
                    continue
                row_hash = _hash(text)
                if legacy:
                    try:
                        path, stat, text = self._migrate_legacy(path, chat)
                        row_hash = _hash(text)
                    except OSError:
                        pass
            cid = chat["id"]
            seen.add(cid)
            self.by_id[cid] = {"path": path, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
                               "hash": row_hash, "chat": chat}
        for cid in list(self.by_id):
            if cid not in seen:
                self.by_id.pop(cid, None)
        _update_root_note(self.root)

    def list_chats(self):
        with self.lock:
            self.refresh()
            out = []
            for cid, row in self.by_id.items():
                chat = row["chat"]
                out.append({"id": cid, "title": chat.get("title") or "Chat",
                            "updated": chat.get("updated") or 0, "backend": chat.get("backend") or ""})
            out.sort(key=lambda item: item.get("updated") or 0, reverse=True)
            return out[:300]

    def load(self, cid):
        if not CHAT_ID.match(cid or ""):
            return None
        with self.lock:
            self.refresh()
            row = self.by_id.get(cid)
            return copy.deepcopy(row["chat"]) if row else None

    def save(self, chat):
        cid = chat.get("id")
        if not CHAT_ID.match(cid or ""):
            raise ValueError("Bad chat id")
        chat = _scrub(copy.deepcopy(chat))
        with self.lock:
            self.refresh()
            known = self.by_id.get(cid)
            if not known:
                self._write_new(chat)
                return
            disk = known["chat"]
            if _workdirs_conflict(disk, chat):
                self._write_conflict(chat)
                return
            ours = chat.get("items") or []
            theirs = disk.get("items") or []
            if _is_prefix(ours, theirs) and len(theirs) > len(ours):
                # The other computer added messages. Leave its file alone.
                return
            merged, conflict = merge_items(None, ours, theirs)
            if conflict:
                self._write_conflict(chat)
                return
            chat["items"] = merged
            chat["sessions"] = _merge_maps(disk.get("sessions"), chat.get("sessions"))
            chat["ctx"] = _merge_maps(disk.get("ctx"), chat.get("ctx"))
            chat["sent"] = _merge_maps(disk.get("sent"), chat.get("sent"))
            if not chat.get("workdir"):
                chat["workdir"] = disk.get("workdir") or ""
            self._write_path(known["path"], chat)

    def delete(self, cid):
        if not CHAT_ID.match(cid or ""):
            return
        with self.lock:
            self.refresh()
            known = self.by_id.get(cid)
            if not known:
                return
            path = known["path"]
            try:
                stat = os.stat(path)
            except OSError:
                return
            if stat.st_mtime_ns != known.get("mtime_ns") or stat.st_size != known.get("size"):
                return  # changed since we looked; leave it for the next scan
            try:
                os.remove(path)
            except OSError:
                return
            self.by_id.pop(cid, None)
            self.base.pop(cid, None)
            _update_root_note(self.root)

    def _available(self, path, cid, current):
        if current and os.path.abspath(current) == os.path.abspath(path):
            return True
        if os.path.exists(path):
            return False
        for other, row in self.by_id.items():
            if other != cid and row.get("path") and os.path.abspath(row["path"]) == os.path.abspath(path):
                return False
        return True

    def _dest_path(self, chat, current=None):
        """Date-and-title filename. A short id suffix is added only when that name is taken."""
        folder = self._chats()
        os.makedirs(folder, exist_ok=True)
        cid = chat["id"]
        for suffix in ("", cid[:6], cid):
            path = os.path.join(folder, chat_filename(chat, suffix))
            if self._available(path, cid, current):
                return path
        return os.path.join(folder, chat_filename(chat, cid))

    def _migrate_legacy(self, path, chat):
        """Rewrite an id-named note in place, then rename it. Rename does not leave a copy."""
        dest = self._dest_path(chat, current=path)
        text = render_chat(chat)
        inside(self.root, path)
        if os.path.abspath(dest) != os.path.abspath(path):
            inside(self.root, dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _atomic_write(path, text)
            os.replace(path, dest)
        else:
            _atomic_write(dest, text)
        return dest, os.stat(dest), text

    def _write_new(self, chat):
        self._write_path(None, chat)

    def _write_path(self, path, chat):
        current = path if path and os.path.isfile(path) else None
        dest = self._dest_path(chat, current=current)
        if current and os.path.abspath(current) != os.path.abspath(dest):
            inside(self.root, current)
            inside(self.root, dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(current, dest)
        inside(self.root, dest)
        text = render_chat(chat)
        _atomic_write(dest, text)
        stat = os.stat(dest)
        self.by_id[chat["id"]] = {"path": dest, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
                                  "hash": _hash(text), "chat": copy.deepcopy(chat)}
        self.base[chat["id"]] = copy.deepcopy(chat.get("items") or [])
        _update_root_note(self.root)

    def _write_conflict(self, chat):
        text = render_chat(chat)
        digest = _hash(text)[:8]
        title = _safe_title(chat.get("title"))
        name = f"{title} (conflict {machine_name()}).md"
        path = os.path.join(self._chats(), name)
        previous = self.conflict_hash.get(chat["id"])
        if previous and previous.get("path") == path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    current = _hash(handle.read())
            except OSError:
                current = None
            if current and current != previous.get("hash"):
                path = os.path.join(self._chats(), f"{title} (conflict {machine_name()} {digest}).md")
        conflict = copy.deepcopy(chat)
        if os.path.abspath(path) != os.path.abspath(self.by_id.get(chat["id"], {}).get("path", "")):
            # A separate note so the other computer's file stays put.
            if conflict["id"] == chat["id"] and self.by_id.get(chat["id"], {}).get("path") != path:
                new_id = hashlib.sha1((chat["id"] + digest).encode()).hexdigest()[:12]
                if new_id == chat["id"]:
                    new_id = (new_id[:11] + "a") if not new_id.endswith("a") else new_id[:11] + "b"
                conflict["id"] = new_id
                conflict["title"] = f"{chat.get('title') or 'Chat'} (conflict {machine_name()})"
                text = render_chat(conflict)
        inside(self.root, path)
        _atomic_write(path, text)
        stat = os.stat(path)
        self.conflict_hash[chat["id"]] = {"path": path, "hash": _hash(text)}
        self.by_id[conflict["id"]] = {"path": path, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
                                      "hash": _hash(text), "chat": conflict}
        self.base[conflict["id"]] = copy.deepcopy(conflict.get("items") or [])
        _update_root_note(self.root)


def _workdirs_conflict(left, right):
    a = (left.get("workdir") or "").rstrip("\\/")
    b = (right.get("workdir") or "").rstrip("\\/")
    return bool(a and b and a != b)


def _merge_maps(existing, incoming):
    out = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in SECRET_KEYS:
            continue
        current = out.get(key)
        if current in (None, "", [], {}):
            out[key] = value
        elif isinstance(current, list) and isinstance(value, list):
            merged = list(current)
            for item in value:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        elif isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge_maps(current, value)
    return out


_libraries = {}
_lib_lock = threading.Lock()


def library(root):
    root = os.path.abspath(root)
    with _lib_lock:
        lib = _libraries.get(root)
        if lib is None:
            lib = Library(root)
            _libraries[root] = lib
        return lib


def _note_updated(path):
    """Sort key for the index: frontmatter updated, or the file's mtime."""
    try:
        with open(path, encoding="utf-8") as handle:
            chunk = handle.read(4096)
    except OSError:
        chunk = ""
    if chunk.startswith("---"):
        end = chunk.find("\n---", 3)
        block = chunk[:end] if end > 0 else ""
        for line in block.splitlines():
            if line.startswith("updated:"):
                seconds = _as_seconds(line.split(":", 1)[1])
                if seconds:
                    return seconds
                break
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


def _index_block(root):
    lines = ["- [[Memories]]"]
    folder = os.path.join(root, "Chats")
    entries = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if _skip_name(name) or not name.endswith(".md"):
                continue
            path = os.path.join(folder, name)
            try:
                if not os.path.isfile(path) or os.path.getsize(path) == 0:
                    continue
            except OSError:
                continue
            stem = name[:-3].replace("]", "").replace("|", "").strip()
            if stem:
                entries.append((_note_updated(path), stem))
    entries.sort(key=lambda item: (-item[0], item[1].lower()))
    lines.extend(f"- [[{stem}]]" for _, stem in entries)
    return INDEX_BEGIN + "\n" + "\n".join(lines) + "\n" + INDEX_END + "\n"


def _splice_index(text, block):
    """Put the index between its markers. Text outside the markers stays put."""
    begin = _INDEX_BEGIN.search(text)
    if begin:
        end = _INDEX_END.search(text, begin.end())
        if end:
            tail = end.end()
            if text[tail:tail + 2] == "\r\n":
                tail += 2
            elif text[tail:tail + 1] in ("\n", "\r"):
                tail += 1
            return text[:begin.start()] + block + text[tail:]
        line_start = text.rfind("\n", 0, begin.start()) + 1
        line_end = text.find("\n", begin.end())
        line_end = len(text) if line_end < 0 else line_end + 1
        text = text[:line_start] + text[line_end:]
    if text.strip():
        if text.endswith("\n\n"):
            return text + block
        if text.endswith("\n"):
            return text + "\n" + block
        return text + "\n\n" + block
    return "# Chatbox\n\n" + block


def _update_root_note(root):
    """Maintain <root>/Chatbox.md. Does nothing when Chatbox has not written a note yet."""
    try:
        note = os.path.join(root, ROOT_NOTE)
        chats = os.path.join(root, "Chats")
        memories = os.path.join(root, "Memories.md")
        exists = os.path.isfile(note)
        has_chats = False
        if os.path.isdir(chats):
            for name in os.listdir(chats):
                if _skip_name(name) or not name.endswith(".md"):
                    continue
                path = os.path.join(chats, name)
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    has_chats = True
                    break
        if not exists and not has_chats and not os.path.isfile(memories):
            return
        block = _index_block(root)
        current = ""
        if exists:
            with open(note, encoding="utf-8") as handle:
                current = handle.read()
        updated = _splice_index(current, block)
        if updated != current:
            _atomic_write(note, updated)
    except OSError:
        return


def update_root_note(root):
    """Refresh the graph index. Takes the library lock; sync itself calls _update_root_note."""
    if not root:
        return
    root = os.path.abspath(root)
    with library(root).lock:
        _update_root_note(root)


def list_chats(root):
    return library(root).list_chats()


def load_chat(root, cid):
    return library(root).load(cid)


def save_chat(root, chat):
    library(root).save(chat)


def delete_chat(root, cid):
    library(root).delete(cid)


def write_memories(root, items):
    """Write memories. If the file changed since we last wrote it, append instead of replacing lines."""
    path = memories_path(root)
    lib = library(root)
    items = [item for item in items if isinstance(item, dict) and item.get("text")]
    with lib.lock:
        current = []
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            current = parse_memories(text)
            on_disk = _hash(text)
        except OSError:
            text = ""
            on_disk = None
        if on_disk and on_disk != lib.memory_hash:
            by_id = {item["id"]: item for item in current}
            merged = list(current)
            have = {item["text"].strip().lower() for item in current}
            for item in items:
                old = by_id.get(item.get("id"))
                if old and old["text"] == item["text"]:
                    continue
                if item["text"].strip().lower() in have:
                    continue
                if old and old["text"] != item["text"]:
                    # Their wording won; keep ours as an extra line.
                    copy_item = dict(item)
                    copy_item["id"] = hashlib.sha1((item["id"] + item["text"]).encode()).hexdigest()[:12]
                    merged.append(copy_item)
                    have.add(item["text"].strip().lower())
                else:
                    merged.append(item)
                    have.add(item["text"].strip().lower())
            items = merged
        rendered = render_memories(items)
        _atomic_write(path, rendered)
        lib.memory_hash = _hash(rendered)
        _update_root_note(root)


def move_existing(data_dir, root):
    """Copy local chats and memories into the sync folder. Originals stay put."""
    os.makedirs(os.path.join(root, "Chats"), exist_ok=True)
    copied = 0
    chats_dir = os.path.join(data_dir, "chats")
    lib = library(root)
    try:
        names = os.listdir(chats_dir)
    except OSError:
        names = []
    have = {row["id"] for row in lib.list_chats()}
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(chats_dir, name), encoding="utf-8") as handle:
                chat = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(chat, dict) or not CHAT_ID.match(str(chat.get("id") or "")):
            continue
        if chat["id"] in have:
            continue
        save_chat(root, chat)
        copied += 1
        have.add(chat["id"])
    memory_count = 0
    mem_path = os.path.join(data_dir, "memories.json")
    try:
        with open(mem_path, encoding="utf-8") as handle:
            local = json.load(handle)
    except (OSError, ValueError):
        local = []
    local = [item for item in local if isinstance(item, dict) and item.get("text")]
    if local:
        existing = read_memories(root) if memories_exist(root) else []
        have_text = {item["text"].strip().lower() for item in existing}
        added = [item for item in local if item["text"].strip().lower() not in have_text]
        if added or not memories_exist(root):
            write_memories(root, existing + added)
            memory_count = len(added)
    return {"copied": copied, "memories": memory_count, "path": root}


def describe(store):
    settings = store.settings()
    root = root_for(store)
    return {"enabled": bool(settings.get("sync_chats")), "active": bool(root),
            "folder": settings.get("sync_folder") or "Chatbox", "path": root or "",
            "vault": bool(store.vault())}

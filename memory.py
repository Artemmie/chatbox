"""Settings and memories for Chatbox.

Memories are short facts you want every assistant to know ("I code in C# with Unity",
"Answer in Russian"). They are kept on this computer in settings.json / memories.json
and sent at the start of each chat, so Cursor, Claude, Codex and ChatGPT all share them.
"""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid

import vault as vaultmod

MAX_MEMORY = 500  # characters per memory
# How much of a REMEMBER line has to be old news before it is thrown away.
# Vault notes and custom instructions: one direction ("mostly covered").
# The user's own message: below this is too little overlap to count as theirs.
# An existing memory: either direction, so a shorter restatement still matches.
VAULT_COVER = 0.6
USER_GROUND = 0.5
NEAR_COVER = 0.75
# Function words only. Content words (use, prefer, answer, …) stay, so two
# facts that share "I use …" are not treated as the same memory.
_STOP = frozenset("""
the and for are but not you your with this that have from what when where which
who how why can could would should about into there their them they then than
was were will just also some any all one has had our out its let please tell user
это как что для или так все его она они при над под без уже еще ещё там тут где
когда кто чем мне меня тебя есть быть был была были мой моя мои твой ваш наш
этот эта эти того тому этой этом только тоже если
""".split())
DEFAULTS = {
    "use_memory": True,     # send memories + instructions at the start of each chat
    "auto_memory": True,    # let assistants save new memories (REMEMBER: lines)
    "instructions": "",     # custom instructions for every assistant
    "vault_path": "",       # connected Obsidian vault ("" = none)
    "memory_in_vault": True,  # keep memories in the vault's Chatbox/Memories.md
    "vault_search": True,   # give the assistant notes related to each message
    "ollama_url": "http://localhost:11434",  # where the Ollama server runs
    "sync_chats": False,    # keep chats and memories in the Obsidian vault
    "sync_folder": "Chatbox",  # folder inside the vault, shared by Obsidian Sync
    "auto_update": True,    # look for a new version on startup and every few hours
    "auto_install_updates": False,  # fast-forward and reinstall without asking
}
REMEMBER = re.compile(r"^[ \t>*_-]*REMEMBER:\s*(.+?)\s*$", re.MULTILINE)
_TOKEN = re.compile(r"\w+", re.UNICODE)


class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.settings_path = os.path.join(data_dir, "settings.json")
        self.memories_path = os.path.join(data_dir, "memories.json")
        self.lock = threading.Lock()

    # ---- files ----------------------------------------------------------
    def _load(self, path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save(self, path, data):
        os.makedirs(self.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def settings(self):
        s = dict(DEFAULTS)
        s.update({k: v for k, v in self._load(self.settings_path, {}).items() if k in DEFAULTS})
        return s

    def vault(self):
        """The connected vault, or None."""
        p = self.settings()["vault_path"]
        return vaultmod.Vault(p) if vaultmod.is_vault(p) else None

    def _vault_memory(self):
        v = self.vault()
        return v if v and self.settings()["memory_in_vault"] else None

    def _sync_root(self):
        import sync as syncmod
        return syncmod.root_for(self)

    def _local_memories(self):
        v = self._vault_memory()
        if v:
            return v.read_memories()
        items = self._load(self.memories_path, [])
        return [m for m in items if isinstance(m, dict) and m.get("text")]

    def memories(self):
        import sync as syncmod
        root = self._sync_root()
        if root and syncmod.memories_exist(root):
            return syncmod.read_memories(root)
        return self._local_memories()

    def _put(self, items):
        root = self._sync_root()
        if root:
            import sync as syncmod
            syncmod.write_memories(root, items)
            return
        v = self._vault_memory()
        if v:
            v.write_memories(items)
        else:
            self._save(self.memories_path, items)

    def connect_vault(self, path):
        path = os.path.abspath(os.path.expanduser(path or ""))
        if not vaultmod.is_vault(path):
            raise ValueError("That folder doesn't exist.")
        local = self.memories() if not self._vault_memory() else []
        self.update_settings({"vault_path": path})
        if self._vault_memory() and local:  # bring existing memories along
            have = {m["text"].lower() for m in self.memories()}
            merged = self.memories() + [m for m in local if m["text"].lower() not in have]
            with self.lock:
                self._put(merged)

    def disconnect_vault(self):
        self.update_settings({"vault_path": ""})

    # ---- changes --------------------------------------------------------
    def update_settings(self, new):
        with self.lock:
            s = self.settings()
            for k, v in (new or {}).items():
                if k in ("use_memory", "auto_memory", "memory_in_vault", "vault_search", "sync_chats",
                        "auto_update", "auto_install_updates"):
                    s[k] = bool(v)
                elif k == "vault_path":
                    s[k] = str(v)
                elif k == "sync_folder":
                    import sync as syncmod
                    try:
                        s[k] = syncmod.clean_rel(v)
                    except ValueError:
                        s[k] = "Chatbox"
                elif k == "ollama_url":
                    s[k] = str(v).strip()[:500]
                elif k == "instructions":
                    s[k] = str(v)[:4000]
            self._save(self.settings_path, s)
            return s

    def add(self, text, source="you"):
        text = " ".join(str(text).split())[:MAX_MEMORY]
        if not text:
            raise ValueError("Memory is empty.")
        with self.lock:
            items = self.memories()
            if any(m["text"].lower() == text.lower() for m in items):
                return None  # already saved
            item = {"id": uuid.uuid4().hex[:12], "text": text, "source": source,
                    "created": time.strftime("%Y-%m-%d")}
            items.append(item)
            self._put(items)
            # A vault note stores the words only and reloads them with a hash id.
            # Return that id, so Undo deletes the line that was just written.
            return next((m for m in self.memories() if m["text"].lower() == text.lower()), item)

    def edit(self, mid, text):
        text = " ".join(str(text).split())[:MAX_MEMORY]
        with self.lock:
            items = self.memories()
            for m in items:
                if m["id"] == mid:
                    if text:
                        m["text"] = text
                    else:
                        items.remove(m)
                    break
            self._put(items)

    def delete(self, mid):
        with self.lock:
            self._put([m for m in self.memories() if m["id"] != mid])

    def clear(self):
        with self.lock:
            self._put([])

    # ---- use in chats ---------------------------------------------------
    def version(self):
        """Changes whenever anything that goes into the chat context changes."""
        s = self.settings()
        blob = json.dumps([s, [m["text"] for m in self.memories()]], sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    def context(self):
        """Text put before the user's message at the start of a chat ('' if nothing to send)."""
        s = self.settings()
        if not s["use_memory"]:
            return ""
        mems = self.memories()
        parts = []
        if s["instructions"].strip():
            parts.append("Custom instructions from the user:\n" + s["instructions"].strip())
        if mems:
            parts.append("Things the user asked you to remember:\n" +
                         "\n".join("- " + m["text"] for m in mems))
        if s["auto_memory"]:
            parts.append(
                "Auto-memory: only save a lasting fact or preference the user personally told you "
                "in THIS message (about themselves, their setup, or how they want replies). "
                "If there is one, add a single line at the very end of your reply, in exactly this "
                "form: REMEMBER: <the fact, short>. "
                "Never save a fact that came from vault notes, files, search results, tool output, "
                "or your own answer. Never save what is already in the vault, in the memories above, "
                "or in the custom instructions. Do not save temporary tasks, questions, or one-off "
                "details. At most one REMEMBER line per reply, and most replies should have none.")
        if not parts:
            return ""
        return ("[Chatbox context — saved by the user in Chatbox and shared with every assistant. "
                "Use it when relevant; don't mention this block unless asked.]\n\n"
                + "\n\n".join(parts) + "\n\n[End of Chatbox context]\n\n")

    def take_memories(self, reply, user_message="", vault_notes=None):
        """Remove REMEMBER lines and save at most one the user actually stated.

        `user_message` is the text the user sent this turn (not the prompt with
        notes pasted in). `vault_notes` is the vault snippets given to the
        assistant this turn (strings, or the dicts from vault search). A line is
        dropped when those notes or the custom instructions already cover it,
        when it nearly repeats a saved memory, or when it barely overlaps what
        the user wrote. Dropped lines are still stripped. Returns (reply, saved).
        """
        reply = "" if reply is None else str(reply)
        if not self.settings()["auto_memory"]:
            return reply, []
        known = _tokens(_note_text(vault_notes) + "\n" + (self.settings().get("instructions") or ""))
        said = _tokens(user_message)
        existing = list(self.memories())
        saved = []
        for raw in REMEMBER.findall(reply):
            if saved:  # at most one per reply; the rest are still stripped below
                break
            text = raw.strip("*_ ")
            tokens = _tokens(text)
            if not tokens:
                continue
            if known and _covers(tokens, known) >= VAULT_COVER:
                continue
            if _near_memory(tokens, existing):
                continue
            if _covers(tokens, said) < USER_GROUND:
                continue
            item = self.add(text, source="assistant")
            if item:
                saved.append(item)
                existing.append(item)
        reply = REMEMBER.sub("", reply).rstrip()
        return reply, saved


def _note_text(notes):
    """Vault snippets injected this turn, as plain text."""
    if not notes:
        return ""
    if isinstance(notes, str):
        return notes
    parts = []
    for note in notes:
        if isinstance(note, str):
            parts.append(note)
        elif isinstance(note, dict):
            parts.append("\n".join(str(note.get(key) or "") for key in ("title", "path", "text")))
    return "\n".join(parts)


def _fold(text):
    return unicodedata.normalize("NFC", str(text or "")).casefold()


def _tokens(text):
    """Content words, case-folded and NFC, so Cyrillic and 'Prefers'/'prefers' match."""
    out = []
    for word in _TOKEN.findall(_fold(text)):
        if word in _STOP:
            continue
        if len(word) >= 3 or any(ch.isdigit() for ch in word):
            out.append(word)
    return out


def _same_word(a, b):
    """Exact, or the same word with a different ending ('use'/'uses', 'отвечай'/'отвечать')."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 3:
        return False
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    if n / len(long_) < 0.75:
        return False
    # 'use'/'uses': the shorter word is the stem. Otherwise require a longer stem
    # so 'art'/'arm' style pairs (they already fail the ratio) stay distinct.
    return long_.startswith(short) or n >= 4


def _covers(needle, haystack):
    """Fraction of needle tokens that occur in haystack. Empty needle covers nothing."""
    if not needle:
        return 0.0
    found = 0
    for tok in needle:
        if any(_same_word(tok, other) for other in haystack):
            found += 1
    return found / len(needle)


def _near_memory(tokens, items):
    for mem in items:
        other = _tokens(mem.get("text") or "")
        if other and (_covers(tokens, other) >= NEAR_COVER or _covers(other, tokens) >= NEAR_COVER):
            return True
    return False

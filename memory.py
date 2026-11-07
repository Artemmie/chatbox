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
import uuid

MAX_MEMORY = 500  # characters per memory
DEFAULTS = {
    "use_memory": True,     # send memories + instructions at the start of each chat
    "auto_memory": True,    # let assistants save new memories (REMEMBER: lines)
    "instructions": "",     # custom instructions for every assistant
}
REMEMBER = re.compile(r"^[ \t>*_-]*REMEMBER:\s*(.+?)\s*$", re.MULTILINE)


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

    def memories(self):
        items = self._load(self.memories_path, [])
        return [m for m in items if isinstance(m, dict) and m.get("text")]

    # ---- changes --------------------------------------------------------
    def update_settings(self, new):
        with self.lock:
            s = self.settings()
            for k, v in (new or {}).items():
                if k in ("use_memory", "auto_memory"):
                    s[k] = bool(v)
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
            self._save(self.memories_path, items)
            return item

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
            self._save(self.memories_path, items)

    def delete(self, mid):
        with self.lock:
            self._save(self.memories_path, [m for m in self.memories() if m["id"] != mid])

    def clear(self):
        with self.lock:
            self._save(self.memories_path, [])

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
            parts.append("If the user tells you a NEW lasting fact or preference that would help in "
                         "future chats, add one line at the very end of your reply in exactly this "
                         "form: REMEMBER: <the fact, short>. Never repeat something already listed "
                         "above, and don't save questions or one-off details; most replies should "
                         "not have it.")
        if not parts:
            return ""
        return ("[Chatbox context — saved by the user in Chatbox and shared with every assistant. "
                "Use it when relevant; don't mention this block unless asked.]\n\n"
                + "\n\n".join(parts) + "\n\n[End of Chatbox context]\n\n")

    def take_memories(self, reply):
        """Remove REMEMBER: lines from a reply and save them. Returns (reply, saved items)."""
        if not self.settings()["auto_memory"]:
            return reply, []
        saved = []
        for text in REMEMBER.findall(reply):
            item = self.add(text.strip("*_ "), source="assistant")
            if item:
                saved.append(item)
        reply = REMEMBER.sub("", reply).rstrip()
        return reply, saved

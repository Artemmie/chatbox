"""Obsidian vault as long-term memory for Chatbox.

An Obsidian vault is just a folder of Markdown notes, so "connecting" means picking
that folder. Chatbox can then:
  * keep its memories in one note (Chatbox/Memories.md) that you can edit in Obsidian,
  * find notes related to what you ask and give them to the assistant,
  * save a whole chat as a note (Chatbox/Chats/…).
"""
import datetime
import hashlib
import json
import os
import re

MEMORY_NOTE = "Chatbox/Memories.md"
CHATS_DIR = "Chatbox/Chats"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
OBSIDIAN_CONFIGS = [
    "~/.config/obsidian/obsidian.json",
    "~/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",  # Flatpak
    "~/snap/obsidian/current/.config/obsidian/obsidian.json",          # Snap
    "~/Library/Application Support/obsidian/obsidian.json",            # macOS
]
WORD = re.compile(r"\w{3,}", re.UNICODE)
STOP = set("""the and for are but not you your with this that have from what when where which
who how why can could would should about into there their them they then than was were will just
also like some any all one has had our out get got its it's let use using want need make please tell
это как что для или так все его она они при над под без уже еще ещё там тут где когда кто чем мне
меня тебя есть быть был была были мой моя мои твой ваш наш""".split())

MEMORY_HEADER = """# Chatbox Memories

Part of [[Chatbox]]

Memories saved by Chatbox and shared with every assistant (Cursor, Claude, Codex, ChatGPT).
Each line that starts with "- " is one memory. Edit or delete lines freely.

"""


def _with_root_link(head):
    """Keep the user's lines and make sure the note links to [[Chatbox]]."""
    if "[[Chatbox]]" in head:
        return head if head.endswith("\n\n") else head.rstrip() + "\n\n"
    lines = head.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0].startswith("#"):
        lines[1:1] = ["", "Part of [[Chatbox]]"]
    else:
        lines[0:0] = ["Part of [[Chatbox]]", ""]
    return "\n".join(lines).rstrip() + "\n\n"


def detect_vaults():
    """Vault folders listed in Obsidian's own settings."""
    found = []
    for cfg in OBSIDIAN_CONFIGS:
        try:
            with open(os.path.expanduser(cfg)) as f:
                vaults = json.load(f).get("vaults", {})
        except (OSError, ValueError):
            continue
        for v in sorted(vaults.values(), key=lambda v: not v.get("open")):
            p = v.get("path")
            if p and os.path.isdir(p) and p not in found:
                found.append(p)
    return found


def is_vault(path):
    return bool(path) and os.path.isdir(path)


def mem_id(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


class Vault:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))

    def _abs(self, rel):
        full = os.path.abspath(os.path.join(self.path, rel))
        if not full.startswith(self.path + os.sep):
            raise ValueError("Path is outside the vault.")
        return full

    def info(self):
        return {"path": self.path, "name": os.path.basename(self.path), "notes": len(self._notes())}

    # ---- memories note ---------------------------------------------------
    def read_memories(self):
        try:
            with open(self._abs(MEMORY_NOTE), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        items = []
        for line in lines:
            m = re.match(r"^\s*[-*]\s+(?!\[[ xX]\])(.+?)\s*$", line)
            if m:
                text = m.group(1)
                items.append({"id": mem_id(text), "text": text, "source": "obsidian", "created": ""})
        return items

    def write_memories(self, items):
        full = self._abs(MEMORY_NOTE)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        try:  # keep anything the user wrote above the list
            with open(full, encoding="utf-8") as f:
                old = f.read()
            head = "\n".join(l for l in old.splitlines() if not re.match(r"^\s*[-*]\s+(?!\[[ xX]\])", l))
            head = head.rstrip() + "\n\n"
        except OSError:
            head = MEMORY_HEADER
        head = _with_root_link(head)
        with open(full + ".tmp", "w", encoding="utf-8") as f:
            f.write(head + "".join("- " + m["text"] + "\n" for m in items))
        os.replace(full + ".tmp", full)
        try:
            import sync as syncmod
            syncmod.update_root_note(os.path.dirname(full))
        except OSError:
            pass

    # ---- search ----------------------------------------------------------
    def _notes(self):
        out = []
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if name.endswith(".md"):
                    out.append(os.path.join(root, name))
        return out

    def search(self, query, limit=3, exclude=(), budget=6000):
        """Notes most related to the query: [{path, title, text}], within ~budget characters."""
        terms = {w for w in (t.lower() for t in WORD.findall(query)) if w not in STOP}
        if not terms:
            return []
        scored = []
        for full in self._notes():
            rel = os.path.relpath(full, self.path)
            if rel in exclude or rel == MEMORY_NOTE or rel.startswith(CHATS_DIR + "/"):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read(200_000)
            except OSError:
                continue
            title = os.path.splitext(os.path.basename(full))[0].lower()
            body = text.lower()
            score, hit = 0, 0
            for t in terms:
                c = body.count(t)
                if c or t in title:
                    hit += 1
                score += min(c, 5) + (6 if t in title else 0)
            # need several matching words (or a title match) to count as related
            if score >= 4 and (hit >= 2 or len(terms) == 1 or any(t in title for t in terms)):
                scored.append((score * hit, rel, text))
        scored.sort(reverse=True)
        out, used = [], 0
        for _, rel, text in scored[:limit]:
            room = min(2500, budget - used)
            if room < 300:
                break
            snippet = text.strip()
            if len(snippet) > room:
                snippet = snippet[:room].rsplit("\n", 1)[0] + "\n…"
            out.append({"path": rel, "title": os.path.splitext(os.path.basename(rel))[0], "text": snippet})
            used += len(snippet)
        return out

    # ---- save a chat -----------------------------------------------------
    def save_chat(self, messages, backend=""):
        now = datetime.datetime.now()
        first = next((m["text"] for m in messages if m.get("role") == "you"), "Chat")
        title = re.sub(r"[\\/:*?\"<>|#^\[\]\n\r\t]+", " ", first).strip()[:50].strip() or "Chat"
        rel = f"{CHATS_DIR}/{now:%Y-%m-%d %H%M} {title}.md"
        full = self._abs(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        lines = ["---", 'up: "[[Chatbox]]"', "---", "",
                 "Part of [[Chatbox]]", "",
                 f"# {title}", "",
                 f"Saved from Chatbox on {now:%Y-%m-%d %H:%M}"
                 + (f" · {backend}" if backend else ""), ""]
        for m in messages:
            who = m.get("who") or ("You" if m.get("role") == "you" else "Assistant")
            lines += [f"**{who}:**", "", m.get("text", "").strip(), ""]
        with open(full, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            import sync as syncmod
            syncmod.update_root_note(os.path.dirname(os.path.dirname(full)))
        except OSError:
            pass
        return rel

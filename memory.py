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
# Sent with every Claude and Codex call. Other assistants have no system-prompt
# flag, so Chatbox prepends this on the first message of a chat instead.
# Original wording: not copied from any assistant's own hidden prompt.
DEFAULT_SYSTEM_PROMPT = (
    "You are replying inside Chatbox, a desktop chat app. "
    "A chat here can be handed from one assistant to another, so answer clearly "
    "in Markdown and make each reply stand on its own."
)
DEFAULTS = {
    "use_memory": True,     # send memories + instructions at the start of each chat
    "auto_memory": True,    # let assistants save new memories (REMEMBER: lines)
    "instructions": "",     # custom instructions for every assistant
    "system_prompt": DEFAULT_SYSTEM_PROMPT,  # Chatbox prompt; "" means send nothing
    "vault_path": "",       # connected Obsidian vault ("" = none)
    "memory_in_vault": True,  # keep memories in the vault's Chatbox/Memories.md
    "vault_search": True,   # give the assistant notes related to each message
    "ollama_url": "http://localhost:11434",  # where the Ollama server runs
    "sync_chats": False,    # keep chats and memories in the notes folder
    "sync_folder": "Chatbox",  # folder inside the vault, shared by Obsidian Sync
    "notes_storage": "",    # obsidian | chatbox | off; "" until the first choice
    "auto_update": True,    # look for a new version on startup and every few hours
    "auto_install_updates": False,  # fast-forward and reinstall without asking
}
REMEMBER = re.compile(r"^[ \t>*_-]*REMEMBER:\s*(.+?)\s*$", re.MULTILINE)
NOTES_MODES = ("obsidian", "chatbox", "off")


def _same_folder(left, right):
    """True when two vault paths are the same folder, ignoring a trailing slash."""
    if not (left or "").strip() or not (right or "").strip():
        return False
    try:
        a = os.path.normpath(os.path.abspath(os.path.expanduser(left.strip())))
        b = os.path.normpath(os.path.abspath(os.path.expanduser(right.strip())))
    except (OSError, ValueError):
        return False
    return os.path.normcase(a) == os.path.normcase(b)


def manual_save_visible(sync_chats, notes_mode, has_folder):
    """Header Save button. Hidden while auto-sync is writing notes.

    Shown only when auto-sync is off and a notes folder is in use.
    """
    if sync_chats:
        return False
    return notes_mode in ("obsidian", "chatbox") and bool(has_folder)


def copy_notes(src, dest):
    """Copy Markdown from the Chatbox folder into a vault. Skip ids and paths already there."""
    import sync as syncmod
    src = os.path.abspath(src)
    dest = os.path.abspath(dest)
    if not os.path.isdir(src) or not vaultmod.is_vault(dest):
        return 0
    if os.path.realpath(src) == os.path.realpath(dest):
        return 0
    try:
        have = set(syncmod.find_chat_notes(dest))
    except Exception:
        have = set()
    copied = 0
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in vaultmod.SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue
            source = os.path.join(dirpath, name)
            rel = os.path.relpath(source, src)
            target = os.path.join(dest, rel)
            try:
                cid = syncmod._note_id(source)
            except Exception:
                cid = ""
            if cid and cid in have:
                continue
            if os.path.exists(target):
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(source, "rb") as incoming, open(target, "wb") as outgoing:
                    outgoing.write(incoming.read())
            except OSError:
                continue
            copied += 1
            if cid:
                have.add(cid)
    return copied
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

    def local_vault_dir(self):
        """Plain Markdown notes kept by Chatbox. Open this folder in Obsidian later."""
        return os.path.join(self.dir, "vault")

    def notes_mode(self):
        """obsidian, chatbox, off, or '' when this install has not chosen yet."""
        try:
            settings = self.settings()
        except Exception:
            return ""
        mode = settings.get("notes_storage") or ""
        if mode in ("obsidian", "chatbox", "off"):
            return mode
        if settings.get("vault_path"):
            return "obsidian"
        return ""

    def ensure_notes_choice(self):
        """First launch with no Obsidian app uses the Chatbox folder. A connected vault stays put."""
        try:
            saved = self._load(self.settings_path, {})
        except Exception:
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if saved.get("notes_storage") in ("obsidian", "chatbox", "off"):
            return saved["notes_storage"]
        if saved.get("vault_path"):
            self.update_settings({"notes_storage": "obsidian", "sync_chats": True})
            return "obsidian"
        try:
            installed = vaultmod.obsidian_installed()
        except Exception:
            installed = False
        if not installed:
            os.makedirs(self.local_vault_dir(), exist_ok=True)
            self.update_settings({"notes_storage": "chatbox", "sync_chats": True})
            return "chatbox"
        self.update_settings({"notes_storage": "off"}, _prepare=False)
        return "off"

    def notes_dir(self):
        """Folder notes are written into, or None when notes are off.

        Obsidian mode uses vault_path. Chatbox mode uses the internal folder.
        Obsidian with no usable vault_path falls back to that same internal folder;
        notes_status says so, instead of pretending the missing path is in use.
        """
        try:
            settings = self.settings()
        except Exception:
            return None
        mode = self.notes_mode()
        if mode == "off":
            return None
        if mode == "chatbox":
            return self.local_vault_dir()
        path = os.path.expanduser((settings.get("vault_path") or "").strip())
        if mode == "obsidian":
            if vaultmod.is_vault(path):
                return os.path.abspath(path)
            return self.local_vault_dir()
        if path and vaultmod.is_vault(path):
            return os.path.abspath(path)
        return None

    def vault(self):
        """The notes folder, or None. Off means no notes. Chatbox mode uses its own folder."""
        try:
            settings = self.settings()
        except Exception:
            return None
        path = self.notes_dir()
        if not path:
            return None
        # Only the internal folder is created. A missing Obsidian path is not.
        if not os.path.isdir(path):
            if os.path.abspath(path) != os.path.abspath(self.local_vault_dir()):
                return None
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                return None
        if not vaultmod.is_vault(path):
            return None
        folder = settings.get("sync_folder") or "Chatbox"
        return vaultmod.Vault(path, sync_folder=folder)

    def notes_status(self):
        """One line for Settings. The vault is named by its folder, never by the full path."""
        try:
            mode = self.notes_mode()
            settings = self.settings()
        except Exception:
            return {"line": "", "fallback": False}
        if mode not in ("obsidian", "chatbox"):
            return {"line": "Chats stay on this computer.", "fallback": False}
        path = os.path.expanduser((settings.get("vault_path") or "").strip())
        if mode == "obsidian" and vaultmod.is_vault(path):
            name = os.path.basename(os.path.abspath(path).rstrip("\\/")) or "Vault"
            return {"line": "✓ Saving chats to " + self._notes_trail(name), "fallback": False}
        if mode == "obsidian":
            where = self._notes_trail("Chatbox")
            if path:
                line = "The vault folder doesn't exist. Saving chats to " + where + "."
            else:
                line = "No vault chosen. Saving chats to " + where + "."
            return {"line": line, "fallback": True}
        return {"line": "✓ Saving chats to " + self._notes_trail("Chatbox"), "fallback": False}

    def notes_card(self, vault_info, installed):
        """The one notes card. Chatbox mode is not an Obsidian connection."""
        try:
            mode = self.notes_mode()
            settings = self.settings()
        except Exception:
            mode, settings = "off", {}
        count = int((vault_info or {}).get("notes") or 0)
        noun = "note" if count == 1 else "notes"
        sync_on = bool(settings.get("sync_chats"))
        show_save = manual_save_visible(sync_on, mode, vault_info is not None and mode != "off")
        if mode == "chatbox":
            return {"mode": "chatbox",
                    "title": f"Notes: Inside Chatbox · {count} {noun}",
                    "path": self.local_vault_dir(),
                    "count": count,
                    "open_folder": True,
                    "open_obsidian": bool(installed),
                    "disconnect": False,
                    "show_save": show_save}
        if mode == "obsidian":
            path = os.path.expanduser((settings.get("vault_path") or "").strip())
            connected = bool(path) and vaultmod.is_vault(path)
            name = os.path.basename(os.path.abspath(path).rstrip("\\/")) if connected else ""
            title = f"{name} · {count} {noun}" if connected else "Obsidian"
            return {"mode": "obsidian",
                    "title": title,
                    "path": os.path.abspath(path) if connected else "",
                    "count": count if connected else 0,
                    "connected": connected,
                    "open_folder": False,
                    "open_obsidian": False,
                    "disconnect": True,
                    "show_save": show_save}
        return {"mode": "off", "title": "Notes storage", "path": "", "count": 0,
                "open_folder": False, "open_obsidian": False, "disconnect": False,
                "show_save": False}

    def _notes_trail(self, head):
        try:
            folder = self.settings().get("sync_folder") or "Chatbox"
        except Exception:
            folder = "Chatbox"
        parts = [head]
        for part in str(folder).replace("\\", "/").split("/"):
            if part and part not in (".", ".."):
                parts.append(part)
        return " › ".join(parts)

    def set_notes_storage(self, mode, path="", copy=False):
        """Switch notes storage. Copying from Chatbox into a vault skips notes already there."""
        mode = mode if mode in ("obsidian", "chatbox", "off") else "off"
        previous = self.notes_mode()
        copied = 0
        unchanged = False
        vault_name = ""
        if mode == "obsidian":
            chosen = (path or "").strip()
            if chosen:
                chosen = os.path.abspath(os.path.expanduser(chosen))
                if os.path.lexists(chosen) and not os.path.isdir(chosen):
                    raise ValueError("That path isn't a folder.")
                if not vaultmod.is_vault(chosen):
                    raise ValueError("That folder doesn't exist.")
                saved = (self.settings().get("vault_path") or "").strip()
                unchanged = previous == "obsidian" and _same_folder(saved, chosen)
                if copy and previous == "chatbox":
                    copied = copy_notes(self.local_vault_dir(), chosen)
                self.update_settings({"notes_storage": "obsidian", "vault_path": chosen, "sync_chats": True})
                vault_name = os.path.basename(chosen.rstrip("\\/")) or "Vault"
            else:
                # Remember the choice. A missing vault_path falls back inside notes_dir.
                saved = os.path.expanduser((self.settings().get("vault_path") or "").strip())
                if copy and previous == "chatbox" and vaultmod.is_vault(saved):
                    copied = copy_notes(self.local_vault_dir(), os.path.abspath(saved))
                self.update_settings({"notes_storage": "obsidian", "sync_chats": True})
                if saved and vaultmod.is_vault(saved):
                    vault_name = os.path.basename(os.path.abspath(saved).rstrip("\\/")) or "Vault"
        elif mode == "chatbox":
            os.makedirs(self.local_vault_dir(), exist_ok=True)
            self.update_settings({"notes_storage": "chatbox", "sync_chats": True})
        else:
            self.update_settings({"notes_storage": "off", "sync_chats": False})
        notice = ""
        if mode == "obsidian" and vault_name:
            notice = "Already using this vault" if unchanged else "Saved: notes go to " + vault_name
        return {"copied": copied, "mode": self.notes_mode(), "unchanged": unchanged,
                "vault_name": vault_name, "notice": notice}

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
        self._adopt_projects_folder()
        if self._vault_memory() and local:  # bring existing memories along
            have = {m["text"].lower() for m in self.memories()}
            merged = self.memories() + [m for m in local if m["text"].lower() not in have]
            with self.lock:
                self._put(merged)

    def _adopt_projects_folder(self):
        """Point the default sync folder at Projects/Chatbox when that layout is already there."""
        vault = self.vault()
        if not vault:
            return
        folder = self.settings().get("sync_folder") or "Chatbox"
        if folder not in ("", "Chatbox"):
            return
        try:
            if vaultmod.has_projects_layout(vault.path):
                self.update_settings({"sync_folder": "Projects/Chatbox"}, _prepare=False)
        except Exception:
            return

    def disconnect_vault(self):
        self.update_settings({"vault_path": ""})

    # ---- changes --------------------------------------------------------
    def update_settings(self, new, _prepare=True):
        requested_folder = None
        folder_changed = False
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
                    previous = s.get("sync_folder")
                    try:
                        s[k] = syncmod.clean_rel(v)
                    except ValueError:
                        s[k] = "Chatbox"
                    requested_folder = s[k]
                    folder_changed = s[k] != previous
                elif k == "ollama_url":
                    s[k] = str(v).strip()[:500]
                elif k == "notes_storage":
                    if v in ("obsidian", "chatbox", "off"):
                        s[k] = v
                elif k == "instructions":
                    s[k] = str(v)[:4000]
                elif k == "system_prompt":
                    # None resets to the built-in prompt. "" clears it, so nothing is sent.
                    s[k] = DEFAULT_SYSTEM_PROMPT if v is None else str(v)[:4000]
            self._save(self.settings_path, s)
        if _prepare and s.get("sync_chats"):
            try:
                import sync as syncmod
                syncmod.prepare_vault(self, requested_folder=requested_folder)
                # Changing the folder rehomes chats. Running it again does not copy them twice.
                if folder_changed:
                    syncmod.relocate(self)
            except Exception:
                pass
            s = self.settings()
        return s

    def add(self, text, source="you", chat=""):
        text = " ".join(str(text).split())[:MAX_MEMORY]
        if not text:
            raise ValueError("Memory is empty.")
        with self.lock:
            items = self.memories()
            if any(m["text"].lower() == text.lower() for m in items):
                return None  # already saved
            item = {"id": uuid.uuid4().hex[:12], "text": text, "source": source,
                    "created": time.strftime("%Y-%m-%d"),
                    "project": self._project_for_fact(text)}
            if re.fullmatch(r"[a-f0-9]{12}", str(chat or "")):
                item["chat"] = chat  # so deleting that chat can offer to delete this too
            items.append(item)
            self._put(items)
            # A vault note stores the words only and reloads them with a hash id.
            # Return that id, so Undo deletes the line that was just written.
            return next((m for m in self.memories() if m["text"].lower() == text.lower()), item)

    def for_chat(self, cid):
        """Memories saved from this chat."""
        if not re.fullmatch(r"[a-f0-9]{12}", str(cid or "")):
            return []
        return [m for m in self.memories() if m.get("chat") == cid]

    def delete_many(self, ids):
        ids = set(ids or [])
        if not ids:
            return
        with self.lock:
            items = self.memories()
            keep = [m for m in items if m["id"] not in ids]
            if len(keep) != len(items):
                self._put(keep)

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
    def _project_for_fact(self, text):
        try:
            vault = self.vault()
            return vault.project_for_fact(text) if vault else ""
        except Exception:
            return ""

    def version(self, project=""):
        """Changes whenever anything that goes into the chat context changes."""
        s = self.settings()
        blob = json.dumps([s, [(m["text"], m.get("project") or "") for m in self.memories()],
                           project or ""], sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    def system_prompt(self):
        """The Chatbox prompt, or '' when the user cleared it. Not part of context()."""
        return (self.settings().get("system_prompt") or "").strip()

    def context(self, project=""):
        """Text put before the user's message at the start of a chat ('' if nothing to send).

        Only general memories and the current project's memories are included.
        """
        s = self.settings()
        if not s["use_memory"]:
            return ""
        current = (project or "").strip()
        mems = [m for m in self.memories()
                if not (m.get("project") or "").strip() or (m.get("project") or "").strip() == current]
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

    def take_memories(self, reply, user_message="", vault_notes=None, chat_id=""):
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
            item = self.add(text, source="assistant", chat=chat_id)
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

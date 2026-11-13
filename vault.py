"""Obsidian vault as long-term memory for Chatbox.

An Obsidian vault is just a folder of Markdown notes, so "connecting" means picking
that folder. Chatbox can then:
  * keep its memories in one note (the Chatbox folder's Memories.md) that you can edit,
  * find notes related to what you ask and give them to the assistant,
  * save a chat into the project it belongs to (or the Chatbox folder when it has none).

Nothing here is tied to one person's folder names. Projects are whatever folders live
under a Projects/ index (or another folder that uses the same hub convention).
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

MEMORY_NOTE = "Chatbox/Memories.md"
CHATS_DIR = "Chatbox/Chats"
PROJECTS_DIR = "Projects"
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".chatbox-backup"}
OBSIDIAN_DOWNLOAD = "https://obsidian.md/download"
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
INDEX_ITEM = re.compile(
    r"^\s*[-*]\s+\[\[([^\]|#]+?)(?:\|([^\]]+))?\]\]\s*(?:[—–\-:]\s*(.+))?$"
)
CHATS_BEGIN = "%% chatbox:chats %%"
CHATS_END = "%% /chatbox:chats %%"
_CHATS_BEGIN = re.compile(r"%%\s*chatbox:chats\s*%%")
_CHATS_END = re.compile(r"%%\s*/chatbox:chats\s*%%")
MAX_HUB_CHATS = 20
MAP_BUDGET = 2200
KEY_NOTES = 6
# Added to a note's rank when it sits inside the matched project. High enough that
# a real in-project hit beats a casual one outside, low enough that a much stronger
# outside note can still appear when the project has nothing relevant.
PROJECT_BOOST = 30

MEMORY_HEADER = """# Chatbox Memories

Part of [[Chatbox]]

Memories saved by Chatbox and shared with every assistant (Cursor, Claude, Codex, ChatGPT).
Lines under ## General apply everywhere. Lines under a project heading apply to that project
and should link its hub. Each line that starts with "- " is one memory. Edit or delete lines freely.

"""

# Short enough to leave room for the project list, and the same text for every vault.
CONVENTIONS = """Vault rules for every user:
- Topic first, then tool. Top level: Home.md (one line per area), Inbox.md, Projects/Projects.md, Bots/Bots.md. Bots/<Editor>.md exists only for an editor that has been used (Claude, Cursor, Codex, ChatGPT, Ollama). Ollama models are grouped under Ollama.md.
- A project stays one note, Projects/<Name>.md, until it needs a second note. Then promote it to Projects/<Name>/<Name>.md. The folder and the hub share the name. Use Title Case for names you create. No dates or ids in folder or note names.
- Line 2 is "Back to [[Parent]]". A hub is a short summary plus its children between %% markers. Only Projects.md links every project. Do not wikilink from one project to another.
- Name a chat by its title ("Title (2)" when the title is taken). File it in that project's Chats folder with up: "[[<Name>]]" and "Part of [[<Name>]]". No project: the Chatbox sync folder, up [[Chatbox]]. Decisions are dated rows you append. Logs are dated entries. Files go in <project>/attachments/.
- Search before creating. Append to the note that already exists. Do not rewrite prose outside %% markers. On rename or delete, update every [[link]] so nothing points at a missing note. Never write secrets, contact info, medical details, or chatter.
"""

_MAPS = {}  # vault path -> (signature, map)


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


def _atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def norm(text):
    text = unicodedata.normalize("NFC", text or "").casefold().replace("ё", "е")
    text = re.sub(r"[_\-./\\]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slug(text):
    text = unicodedata.normalize("NFC", text or "").casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def safe_project_name(name):
    """A single path segment. Rejects anything that could leave the projects folder."""
    name = str(name or "").strip().strip("/\\")
    if not name or name in (".", "..") or name.startswith("."):
        return ""
    if "/" in name or "\\" in name or any(ch in name for ch in ':*?"<>|'):
        return ""
    if any(ord(ch) < 32 for ch in name):
        return ""
    return name[:120]


def breadcrumb(rel):
    parts = (rel or "").replace("\\", "/").split("/")
    if parts and parts[-1].lower().endswith(".md"):
        parts[-1] = parts[-1][:-3]
    return " > ".join(p for p in parts if p)


def contains_phrase(text, phrase):
    """True when phrase appears as its own words, not as a piece of a longer word."""
    folded = norm(text)
    wanted = norm(phrase)
    if len(wanted) < 3 or not folded:
        return False
    return re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", folded) is not None


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


def obsidian_installed(system=None, environ=None, home=None, exists=None, which=None):
    """Cheap per-OS check. No process is started. Missing means not installed."""
    system = system or sys.platform
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~") if home is None else home
    exists = exists or os.path.exists
    finder = which if which is not None else shutil.which
    if system == "darwin":
        candidates = [
            "/Applications/Obsidian.app",
            os.path.join(home, "Applications", "Obsidian.app"),
        ]
    elif system in ("win32", "cygwin"):
        local = environ.get("LOCALAPPDATA", "")
        candidates = [os.path.join(local, "Obsidian")] if local else []
    else:
        found = finder("obsidian") if finder else None
        if found:
            return True
        candidates = [
            "/usr/bin/obsidian",
            "/usr/local/bin/obsidian",
            "/opt/Obsidian/obsidian",
            "/opt/obsidian/obsidian",
            "/opt/Obsidian/Obsidian.AppImage",
            "/snap/bin/obsidian",
            "/snap/obsidian",
            "/var/lib/flatpak/app/md.obsidian.Obsidian",
            os.path.join(home, ".local", "bin", "obsidian"),
            os.path.join(home, ".local", "bin", "Obsidian.AppImage"),
            os.path.join(home, "Applications", "Obsidian.AppImage"),
            os.path.join(home, "Obsidian.AppImage"),
            os.path.join(home, ".local", "share", "flatpak", "app", "md.obsidian.Obsidian"),
            os.path.join(home, "snap", "obsidian"),
        ]
    return any(path and exists(path) for path in candidates)


def connection_notice(vault_path, connected, installed):
    """Why sync cannot run, or an empty notice when the vault is connected."""
    path = (vault_path or "").strip()
    if connected:
        notice = ""
    elif path:
        notice = "Vault folder not found."
    elif not installed:
        notice = "Obsidian not found."
    else:
        notice = "No Obsidian vault selected."
    return {"installed": bool(installed), "connected": bool(connected),
            "download": OBSIDIAN_DOWNLOAD, "notice": notice}


def _memory_preamble(text):
    """Prose above the memory list. Stops at the first heading or bullet."""
    lines = []
    for line in (text or "").splitlines():
        if re.match(r"^##\s+", line) or re.match(r"^\s*[-*]\s+", line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _section_project(title):
    title = re.sub(r"^\[\[(.+?)\]\]$", r"\1", (title or "").strip())
    title = title.split("|", 1)[0].strip()
    if not title or title.casefold() == "general":
        return ""
    return title


def _strip_marked(text, begin, end):
    found = begin.search(text or "")
    if not found:
        return text or ""
    close = end.search(text, found.end())
    if not close:
        return text[:found.start()] + text[found.end():]
    tail = close.end()
    if text[tail:tail + 1] == "\n":
        tail += 1
    return text[:found.start()] + text[tail:]


def _splice_marked(text, block, begin, end):
    """Replace the marked region. Text outside the markers stays put."""
    found = begin.search(text or "")
    if found:
        close = end.search(text, found.end())
        if close:
            tail = close.end()
            if text[tail:tail + 2] == "\r\n":
                tail += 2
            elif text[tail:tail + 1] in ("\n", "\r"):
                tail += 1
            return text[:found.start()] + block + text[tail:]
        line_start = text.rfind("\n", 0, found.start()) + 1
        line_end = text.find("\n", found.end())
        line_end = len(text) if line_end < 0 else line_end + 1
        text = text[:line_start] + text[line_end:]
    text = text or ""
    if text.strip():
        if text.endswith("\n\n"):
            return text + block
        if text.endswith("\n"):
            return text + "\n" + block
        return text + "\n\n" + block
    return block


def _link_target(raw):
    return (raw or "").split("|", 1)[0].split("#", 1)[0].strip()


def _note_stamp(path):
    """A number that sorts newest-last. Frontmatter `updated` wins over mtime."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 0
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(4096)
    except OSError:
        return mtime
    if not chunk.startswith("---"):
        return mtime
    end = chunk.find("\n---", 3)
    block = chunk[:end] if end > 0 else ""
    for line in block.splitlines():
        if not line.startswith("updated:"):
            continue
        raw = line.split(":", 1)[1].strip().strip('"')
        matched = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", raw)
        if not matched:
            return mtime
        parts = [int(piece or 0) for piece in matched.groups()]
        return parts[0] * 10**10 + parts[1] * 10**8 + parts[2] * 10**6 + parts[3] * 10**4 + parts[4] * 100 + parts[5]
    return mtime


def _wiki_stem(name):
    return name[:-3].replace("]", "").replace("|", "").strip() if name.endswith(".md") else name


class Vault:
    def __init__(self, path, sync_folder=None):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.sync_folder = (sync_folder or "Chatbox").strip("/") or "Chatbox"

    def _abs(self, rel):
        full = os.path.abspath(os.path.join(self.path, rel))
        if full != self.path and not full.startswith(self.path + os.sep):
            raise ValueError("Path is outside the vault.")
        return full

    def memory_note(self):
        return self.sync_folder + "/Memories.md"

    def chats_dir(self):
        return self.sync_folder + "/Chats"

    def info(self):
        return {"path": self.path, "name": os.path.basename(self.path), "notes": len(self._notes())}

    # ---- memories note ---------------------------------------------------
    def read_memories(self):
        try:
            with open(self._abs(self.memory_note()), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        items = []
        project = ""
        for line in lines:
            heading = re.match(r"^##\s+(.+?)\s*$", line)
            if heading:
                project = _section_project(heading.group(1))
                continue
            matched = re.match(r"^\s*[-*]\s+(?!\[[ xX]\])(.+?)\s*$", line)
            if matched:
                text = matched.group(1)
                items.append({"id": mem_id(text), "text": text, "source": "obsidian",
                              "created": "", "project": project})
        return items

    def write_memories(self, items):
        full = self._abs(self.memory_note())
        os.makedirs(os.path.dirname(full), exist_ok=True)
        try:  # keep anything the user wrote above the list
            with open(full, encoding="utf-8") as f:
                old = f.read()
            head = _memory_preamble(old)
            head = (head.rstrip() + "\n\n") if head.strip() else MEMORY_HEADER
        except OSError:
            head = MEMORY_HEADER
        head = _with_root_link(head)
        _atomic(full, head + _grouped_plain(items))
        try:
            import sync as syncmod
            syncmod.update_root_note(os.path.dirname(full))
        except OSError:
            pass

    def search_hint(self):
        """Tell a file-searching assistant where the vault is and to follow its symlinks."""
        return (f"The user's Obsidian vault is at {self.path}. Some folders in it are symlinks "
                "to other directories. When you search the vault yourself, follow those symlinks "
                "(for example `rg -L`, or `grep -R`).")

    # ---- notes -----------------------------------------------------------
    def _walk(self, start=None):
        """(absolute path, vault-relative posix path) for every markdown file under start."""
        base = start or self.path
        seen = set()
        for root, dirs, files in os.walk(base, followlinks=True):
            real = os.path.realpath(root)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if name.endswith(".md") and not name.startswith("."):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, self.path).replace("\\", "/")
                    yield full, rel

    def _notes(self):
        """Markdown files, including ones reached through a symlinked folder.

        Paths stay as walked from the vault, so a link named telegram-ai/shizik-memory
        is reported that way and not as the real folder it points at. A symlink that
        circles back to a directory already walked is skipped.
        """
        return [full for full, _rel in self._walk()]

    def _is_chat_file(self, rel):
        parts = rel.split("/")
        if len(parts) >= 2 and parts[-2] == "Chats":
            return True
        return rel == self.memory_note()

    # ---- project map -----------------------------------------------------
    def _find_projects_root(self):
        """The folder that holds projects. `Projects` when that directory exists.

        Otherwise a single top-level folder whose own note is named like the folder
        and that contains child folders (the same convention under another name).
        """
        preferred = os.path.join(self.path, PROJECTS_DIR)
        if os.path.isdir(preferred):
            return PROJECTS_DIR
        try:
            names = os.listdir(self.path)
        except OSError:
            return None
        found = []
        for name in names:
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            folder = os.path.join(self.path, name)
            if not os.path.isdir(folder) or not os.path.isfile(os.path.join(folder, name + ".md")):
                continue
            try:
                children = [c for c in os.listdir(folder)
                            if not c.startswith(".") and c not in SKIP_DIRS
                            and os.path.isdir(os.path.join(folder, c))]
            except OSError:
                continue
            if children:
                found.append(name)
        if len(found) == 1:
            return found[0]
        return None

    def projects_root(self):
        return self._find_projects_root() or PROJECTS_DIR

    def _signature(self):
        root = self._find_projects_root()
        if not root:
            return (self.path, "none")
        stamps = [root]
        base = os.path.join(self.path, root)
        seen = set()
        for dirpath, dirs, files in os.walk(base, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            try:
                stat = os.stat(dirpath)
                stamps.append((os.path.relpath(dirpath, self.path), stat.st_mtime_ns))
            except OSError:
                continue
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, self.path).replace("\\", "/")
                stamps.append((rel, stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def map(self):
        """Cached {root, projects}. Rebuilt when a project file's mtime or size changes."""
        sig = self._signature()
        cached = _MAPS.get(self.path)
        if cached and cached[0] == sig:
            return cached[1]
        data = self._build_map()
        _MAPS[self.path] = (sig, data)
        return data

    def _build_map(self):
        root = self._find_projects_root()
        projects = []
        if not root:
            return {"root": PROJECTS_DIR, "projects": projects}
        index_text = ""
        index_path = os.path.join(self.path, root, root + ".md")
        try:
            with open(index_path, encoding="utf-8", errors="replace") as handle:
                index_text = handle.read(200_000)
        except OSError:
            index_text = ""
        described = {}
        for line in index_text.splitlines():
            matched = INDEX_ITEM.match(line)
            if not matched:
                continue
            target = _link_target(matched.group(1))
            label = (matched.group(2) or target).strip()
            blurb = (matched.group(3) or "").strip()
            described[target] = (label, blurb)
            described[label] = (label, blurb)
        base = os.path.join(self.path, root)
        try:
            children = sorted(os.listdir(base), key=lambda item: item.casefold())
        except OSError:
            children = []
        for name in children:
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            folder = os.path.join(base, name)
            if os.path.isfile(folder) and name.endswith(".md") and name.casefold() != (root + ".md").casefold():
                stem = name[:-3]
                hub_text = ""
                try:
                    with open(folder, encoding="utf-8", errors="replace") as handle:
                        hub_text = handle.read(80_000)
                except OSError:
                    hub_text = ""
                label, blurb = described.get(stem, ("", ""))
                projects.append({
                    "name": stem,
                    "folder": f"{root}/{stem}",
                    "hub": f"{root}/{stem}.md",
                    "lone": True,
                    "description": (blurb or _hub_description(hub_text))[:180],
                    "aliases": _alias_list(stem, label, hub_text),
                    "notes": [],
                })
                continue
            if not os.path.isdir(folder):
                continue
            hub_rel = f"{root}/{name}/{name}.md"
            hub_abs = os.path.join(folder, name + ".md")
            hub_text = ""
            if os.path.isfile(hub_abs):
                try:
                    with open(hub_abs, encoding="utf-8", errors="replace") as handle:
                        hub_text = handle.read(80_000)
                except OSError:
                    hub_text = ""
            label, blurb = described.get(name, ("", ""))
            description = blurb or _hub_description(hub_text)
            projects.append({
                "name": name,
                "folder": f"{root}/{name}",
                "hub": hub_rel if hub_text or os.path.isfile(hub_abs) else "",
                "lone": False,
                "description": description[:180],
                "aliases": _alias_list(name, label, hub_text),
                "notes": self._key_notes(folder, hub_rel, hub_text),
            })
        return {"root": root, "projects": projects}

    def _key_notes(self, folder, hub_rel, hub_text):
        linked = set()
        for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", hub_text or ""):
            linked.add(_link_target(match.group(1)).casefold())
        notes = []
        for full, rel in self._walk(folder):
            if rel == hub_rel or self._is_chat_file(rel):
                continue
            title = os.path.splitext(os.path.basename(rel))[0]
            prefix = os.path.relpath(folder, self.path).replace("\\", "/") + "/"
            rel_no = rel[len(prefix):] if rel.startswith(prefix) else title
            if rel_no.lower().endswith(".md"):
                rel_no = rel_no[:-3]
            notes.append({
                "path": rel,
                "title": title,
                "rel": rel_no,
                "breadcrumb": breadcrumb(rel),
                "linked": title.casefold() in linked or rel_no.casefold() in linked,
            })
        notes.sort(key=lambda note: (not note["linked"], note["path"].count("/"), note["path"].casefold()))
        for note in notes:
            note.pop("linked", None)
        return notes[:KEY_NOTES]

    def project_by_name(self, name):
        wanted = safe_project_name(name)
        if not wanted:
            return None
        for project in self.map()["projects"]:
            if project["name"] == wanted:
                return project
        return None

    def project_for_path(self, rel):
        rel = (rel or "").replace("\\", "/")
        root = self.map()["root"]
        prefix = root + "/"
        if not rel.startswith(prefix):
            return ""
        name = rel[len(prefix):].split("/", 1)[0]
        if name.lower().endswith(".md"):
            name = name[:-3]
        return name if self.project_by_name(name) else ""

    def map_text(self, budget=MAP_BUDGET):
        """Conventions plus a compact project list, never longer than budget."""
        data = self.map()
        chunks = [CONVENTIONS.rstrip(), "", "Projects:"]
        if not data["projects"]:
            chunks.append("- (none yet — a new one is created when you file a chat into it)")
        else:
            for project in data["projects"]:
                line = "- " + project["name"]
                if project.get("description"):
                    line += " — " + project["description"]
                trial = chunks + [line]
                if len("\n".join(trial)) + 1 > budget:
                    break
                chunks.append(line)
                for note in project.get("notes") or []:
                    note_line = "  - " + (note.get("rel") or note.get("title") or "")
                    trial = chunks + [note_line]
                    if len("\n".join(trial)) + 1 > budget:
                        break
                    chunks.append(note_line)
        text = "\n".join(chunks).rstrip() + "\n"
        if len(text) <= budget:
            return text
        trimmed = text[:max(0, budget - 1)].rsplit("\n", 1)[0].rstrip()
        return (trimmed + "\n…\n") if trimmed else "…\n"

    def match_project(self, message="", workdir="", ranked=None, search=True):
        """Best project for this message and working folder, or '' when none is clear."""
        projects = self.map()["projects"]
        if not projects:
            return ""
        best_name, best_score = "", 0
        folded_message = message or ""
        names = _workdir_names(workdir)
        work_abs = os.path.abspath(os.path.expanduser(workdir)) if workdir else ""
        for project in projects:
            score = 0
            phrases = [project["name"]] + list(project.get("aliases") or [])
            if any(contains_phrase(folded_message, phrase) for phrase in phrases):
                score = max(score, 100 + len(project["name"]))
            folder_abs = os.path.abspath(os.path.join(self.path, project["folder"]))
            if work_abs and (work_abs == folder_abs or work_abs.startswith(folder_abs + os.sep)):
                score = max(score, 90)
            target = slug(project["name"])
            if len(target) >= 3 and any(slug(name) == target for name in names):
                score = max(score, 70)
            if score > best_score:
                best_name, best_score = project["name"], score
        if best_name:
            return best_name
        if ranked is None and search and folded_message.strip():
            ranked = self.rank(folded_message)
        return _concentrated(ranked or [])

    def project_for_fact(self, text):
        """The one project a memory is clearly about, or '' when it is general."""
        hits = []
        for project in self.map()["projects"]:
            phrases = [project["name"]] + [a for a in project.get("aliases") or [] if norm(a) != norm(project["name"])]
            if any(contains_phrase(text, phrase) for phrase in phrases):
                hits.append(project["name"])
        if len(hits) == 1:
            return hits[0]
        return ""

    def hub_snippet(self, name, limit=1500):
        project = self.project_by_name(name)
        if not project or not project.get("hub"):
            return None
        try:
            with open(self._abs(project["hub"]), encoding="utf-8", errors="replace") as handle:
                text = handle.read(40_000)
        except (OSError, ValueError):
            return None
        text = _strip_marked(text, _CHATS_BEGIN, _CHATS_END).strip()
        if len(text) > limit:
            text = text[:limit].rsplit("\n", 1)[0] + "\n…"
        return {"path": project["hub"], "title": project["name"], "text": text,
                "breadcrumb": breadcrumb(project["hub"])}

    # ---- search ----------------------------------------------------------
    def rank(self, query, exclude=()):
        """Every related note, best first, before the project boost is applied."""
        folded = unicodedata.normalize("NFC", query or "")
        terms = {w for w in (t.lower() for t in WORD.findall(folded)) if w not in STOP}
        if not terms:
            return []
        exclude = set(exclude or ())
        projects = {p["name"] for p in self.map()["projects"]}
        root = self.map()["root"]
        scored = []
        for full, rel in self._walk():
            if rel in exclude or self._is_chat_file(rel):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    text = handle.read(200_000)
            except OSError:
                continue
            title = unicodedata.normalize("NFC", os.path.splitext(os.path.basename(full))[0]).lower()
            body = unicodedata.normalize("NFC", text).lower()
            score, hit = 0, 0
            for term in terms:
                count = body.count(term)
                if count or term in title:
                    hit += 1
                score += min(count, 5) + (6 if term in title else 0)
            if not (score >= 4 and (hit >= 2 or len(terms) == 1 or any(t in title for t in terms))):
                continue
            project = ""
            prefix = root + "/"
            if rel.startswith(prefix):
                name = rel[len(prefix):].split("/", 1)[0]
                if name in projects:
                    project = name
            scored.append({
                "score": score * hit,
                "path": rel,
                "title": os.path.splitext(os.path.basename(rel))[0],
                "text": text,
                "breadcrumb": breadcrumb(rel),
                "project": project,
            })
        scored.sort(key=lambda row: (-row["score"], row["path"]))
        return scored

    def pick(self, ranked, project=None, limit=3, budget=6000):
        """Trim ranked notes. A matched project contributes its hits first."""
        rows = [dict(row) for row in (ranked or [])]
        if project:
            for row in rows:
                if row.get("project") == project:
                    row["score"] = row["score"] + PROJECT_BOOST + 12
            inside = [row for row in rows if row.get("project") == project]
            outside = [row for row in rows if row.get("project") != project]
            inside.sort(key=lambda row: (-row["score"], row["path"]))
            outside.sort(key=lambda row: (-row["score"], row["path"]))
            rows = inside[:limit]
            if len(rows) < limit:
                rows.extend(outside[:limit - len(rows)])
        else:
            rows = rows[:limit]
        out, used = [], 0
        for row in rows:
            room = min(2500, budget - used)
            if room < 300:
                break
            snippet = row["text"].strip()
            if len(snippet) > room:
                snippet = snippet[:room].rsplit("\n", 1)[0] + "\n…"
            out.append({"path": row["path"], "title": row["title"], "text": snippet,
                        "breadcrumb": row["breadcrumb"], "project": row.get("project") or ""})
            used += len(snippet)
        return out

    def search(self, query, limit=3, exclude=(), budget=6000, project=None):
        """Notes most related to the query: [{path, title, text, breadcrumb}], within ~budget."""
        return self.pick(self.rank(query, exclude=exclude), project=project, limit=limit, budget=budget)

    # ---- save a chat -----------------------------------------------------
    def save_chat(self, messages, backend="", workdir="", project=None, locked=False, chat_id=""):
        now = datetime.datetime.now()
        first = next((m["text"] for m in messages if m.get("role") == "you"), "Chat")
        title = re.sub(r"[\\/:*?\"<>|#^\[\]\n\r\t]+", " ", first).strip()[:50].strip() or "Chat"
        blob = "\n".join(m.get("text") or "" for m in messages)
        if locked:
            name = safe_project_name(project or "")
        else:
            name = self.match_project(message=blob, workdir=workdir)
        if name:
            ensure_project(self.path, name)
            promote_project(self.path, name)
            root = self.projects_root()
            rel_dir = f"{root}/{name}/Chats"
            link = f"[[{name}]]"
        else:
            rel_dir = self.chats_dir()
            link = "[[Chatbox]]"
        # Same chat id updates the note already written for it. A second click
        # must not add "Title (2).md".
        existing = _note_for_chat(self.path, chat_id)
        if existing:
            full = existing
            rel = os.path.relpath(full, self.path).replace(os.sep, "/")
        else:
            rel = _free_note(self, rel_dir, title)
            full = self._abs(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        lines = ["---"]
        cid = _chat_id(chat_id)
        if cid:
            lines.append(f'id: "{cid}"')
        lines.append(f'up: "{link}"')
        if name:
            lines.append("project: " + json.dumps(name, ensure_ascii=False))
        lines += ["---", "", f"# {title}", f"Back to {link}", "", f"Part of {link}", "",
                  f"Saved from Chatbox on {now:%Y-%m-%d %H:%M}" + (f" · {backend}" if backend else ""), ""]

        def who_of(message):
            who = message.get("who")
            if isinstance(who, str) and who.strip():
                return who.strip()
            if message.get("role") == "you":
                return "You"
            return {"cursor": "Cursor", "claude": "Claude", "codex": "Codex",
                    "chatgpt": "ChatGPT", "ollama": "Ollama"}.get(message.get("b") or "", "Assistant")

        for message in messages:
            lines += [f"**{who_of(message)}:**", "", message.get("text", "").strip(), ""]
        _atomic(full, "\n".join(lines))
        try:
            import sync as syncmod
            if name and rel_dir != self.chats_dir():
                refresh_project_hub(self.path, name)
            else:
                syncmod.update_root_note(os.path.dirname(os.path.dirname(full)))
        except OSError:
            pass
        return rel


def _grouped_plain(items):
    """## General and ## <Project> sections of plain '- text' memories."""
    general = []
    order = []
    grouped = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        project = (item.get("project") or "").strip()
        if not safe_project_name(project):
            project = ""
        if not project:
            general.append(item)
            continue
        if project not in grouped:
            order.append(project)
            grouped[project] = []
        grouped[project].append(item)
    lines = ["## General", ""]
    lines.extend("- " + item["text"] + "\n" for item in general)
    if general:
        lines.append("")
    for project in order:
        lines.append(f"## {project}")
        lines.append("")
        lines.append(f"[[{project}]]")
        lines.append("")
        for item in grouped[project]:
            lines.append("- " + item["text"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _alias_list(name, label, hub_text):
    aliases = []
    for candidate in (name, label, _heading(hub_text)):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    aliases.extend(a for a in _front_aliases(hub_text) if a not in aliases)
    return aliases


def _heading(text):
    matched = re.search(r"(?m)^#\s+(.+?)\s*$", text or "")
    return matched.group(1).strip() if matched else ""


def _front_aliases(text):
    if not (text or "").startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end < 0:
        return []
    for line in text[3:end].splitlines():
        if not line.lower().startswith("aliases:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
        return [part.strip().strip('"') for part in raw.split(",") if part.strip()]
    return []


def _hub_description(text):
    text = _strip_marked(text or "", _CHATS_BEGIN, _CHATS_END)
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end > 0 else text
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("%%"):
            continue
        if stripped.startswith("Back to [[") or stripped.startswith("|") or stripped.startswith("<!--"):
            continue
        if stripped.startswith("- [["):
            continue
        return stripped[:180]
    return ""


def _workdir_names(workdir):
    if not workdir:
        return []
    path = os.path.abspath(os.path.expanduser(workdir))
    names = []
    base = os.path.basename(path.rstrip("\\/"))
    if base:
        names.append(base)
    config = os.path.join(path, ".git", "config")
    try:
        with open(config, encoding="utf-8", errors="replace") as handle:
            raw = handle.read(8000)
    except OSError:
        return names
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("url =") and not stripped.startswith("url="):
            continue
        url = stripped.split("=", 1)[1].strip()
        leaf = url.rstrip("/").split("/")[-1].split(":")[-1]
        if leaf.endswith(".git"):
            leaf = leaf[:-4]
        if leaf and leaf not in names:
            names.append(leaf)
    return names


def _concentrated(ranked):
    """The project that owns the top search hits, when one project clearly leads."""
    counts = {}
    considered = 0
    for row in (ranked or [])[:5]:
        name = row.get("project") or ""
        if not name:
            continue
        considered += 1
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    winner = max(counts, key=lambda name: (counts[name], name))
    if counts[winner] >= 2 and counts[winner] > considered - counts[winner]:
        return winner
    return ""


def has_projects_layout(vault_path):
    if not vault_path or not os.path.isdir(vault_path):
        return False
    return Vault(vault_path)._find_projects_root() is not None


def ensure_layout(vault_path):
    """Create Projects/Projects.md and a Home.md link. Leaves existing prose alone.

    When the vault already uses this convention under another folder name, that
    folder stays the project root and an empty Projects/ is not added beside it.
    Returns False when there is no vault folder to write into.
    """
    if not vault_path or not os.path.isdir(vault_path):
        return False
    root = Vault(vault_path)._find_projects_root() or PROJECTS_DIR
    folder = os.path.join(vault_path, root)
    os.makedirs(folder, exist_ok=True)
    index = os.path.join(folder, root + ".md")
    if not os.path.isfile(index):
        title = root
        _atomic(index, f"# {title}\n\nBack to [[Home]]\n")
    else:
        _ensure_substring(index, "[[Home]]", "\nBack to [[Home]]\n")
    ensure_bots_hub(vault_path)
    return True


def _ensure_substring(path, needle, addition):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return
    if needle in text:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic(path, text + addition)


def ensure_project(vault_path, name, description=None):
    """Create a missing project note and add it to the index.

    One note stays a lone file. A folder hub that already exists is not rewritten.
    The index link is added only after the note exists, and only when it is missing.
    """
    name = safe_project_name(name)
    if not name or not vault_path or not os.path.isdir(vault_path):
        return None
    ensure_layout(vault_path)
    vault = Vault(vault_path)
    root = vault.projects_root()
    folder = os.path.join(vault_path, root, name)
    hub = os.path.join(folder, name + ".md")
    lone = os.path.join(vault_path, root, name + ".md")
    blurb = description if description else f"Notes and chats for {name}."
    if os.path.isdir(folder):
        if not os.path.isfile(hub):
            _atomic(hub, _hub_template(name, blurb, root))
        target = hub
    elif os.path.isfile(lone):
        target = lone
    elif name == "Chatbox":
        os.makedirs(folder, exist_ok=True)
        if not os.path.isfile(hub):
            _atomic(hub, _hub_template(name, blurb, root))
        target = hub
    else:
        _atomic(lone, _lone_template(name, blurb, root))
        target = lone
    if os.path.isfile(target):
        _ensure_index_line(os.path.join(vault_path, root, root + ".md"), name, blurb)
    _MAPS.pop(os.path.abspath(vault_path), None)
    return target


def _hub_template(name, description, root):
    # The Chatbox hub uses the sync index (Memories + its own chats) instead of a second list.
    # Chat links are added later, and only for notes that already exist.
    if name == "Chatbox":
        return f"# {name}\nBack to [[{root}]]\n\n{description}\n"
    block = f"{CHATS_BEGIN}\n## Chats\n\n{CHATS_END}\n"
    return f"# {name}\nBack to [[{root}]]\n\n{description}\n\n{block}"


def _lone_template(name, description, root):
    return f"# {name}\nBack to [[{root}]]\n\n{description}\n"


def _ensure_index_line(index_path, name, description):
    try:
        with open(index_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = f"# Projects\n\nBack to [[Home]]\n"
    if re.search(r"\[\[" + re.escape(name) + r"(\||\]\])", text):
        return
    line = f"- [[{name}]] — {description}\n" if description else f"- [[{name}]]\n"
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic(index_path, text + line)


def project_chats_dir(vault_path, name):
    name = safe_project_name(name)
    if not name or not vault_path:
        return None
    promote_project(vault_path, name)
    vault = Vault(vault_path)
    project = vault.project_by_name(name)
    root = project["folder"].rsplit("/", 1)[0] if project else vault.projects_root()
    folder_rel = project["folder"] if project and not project.get("lone") else f"{root}/{name}"
    return os.path.join(vault_path, folder_rel.replace("/", os.sep), "Chats")


def refresh_project_hub(vault_path, name):
    """Rewrite only the marked chat list on a project hub. Other text stays."""
    name = safe_project_name(name)
    if not name or not vault_path:
        return
    vault = Vault(vault_path)
    project = vault.project_by_name(name)
    root = project["folder"].rsplit("/", 1)[0] if project else vault.projects_root()
    folder_rel = project["folder"] if project else f"{root}/{name}"
    hub = os.path.join(vault_path, folder_rel.replace("/", os.sep), name + ".md")
    if not os.path.isfile(hub):
        return
    chats = os.path.join(vault_path, folder_rel.replace("/", os.sep), "Chats")
    entries = []
    if os.path.isdir(chats):
        for filename in os.listdir(chats):
            if filename.startswith(".") or not filename.endswith(".md"):
                continue
            path = os.path.join(chats, filename)
            try:
                if not os.path.isfile(path) or os.path.getsize(path) == 0:
                    continue
            except OSError:
                continue
            stem = _wiki_stem(filename)
            if stem:
                entries.append((_note_stamp(path), stem))
    entries.sort(key=lambda item: (item[0], item[1].casefold()), reverse=True)
    stems = [stem for _stamp, stem in entries]
    lines = ["## Chats", ""]
    for stem in stems[:MAX_HUB_CHATS]:
        lines.append(f"- [[{stem}]]")
    if len(stems) > MAX_HUB_CHATS:
        lines.append("")
        lines.append(f"More chats are in {folder_rel}/Chats.")
    block = CHATS_BEGIN + "\n" + "\n".join(lines) + "\n" + CHATS_END + "\n"
    try:
        with open(hub, encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        return
    updated = _splice_marked(current, block, _CHATS_BEGIN, _CHATS_END)
    if updated != current:
        _atomic(hub, updated)


def _chat_id(value):
    text = str(value or "").strip().strip('"')
    return text if re.fullmatch(r"[a-f0-9]{12}", text) else ""


def _note_for_chat(vault_path, chat_id):
    """The note already saved for this chat, or '' when this id has none."""
    cid = _chat_id(chat_id)
    if not cid or not vault_path:
        return ""
    try:
        import sync as syncmod
        found = syncmod.find_chat_notes(vault_path).get(cid) or []
    except Exception:
        return ""
    return sorted(found)[0] if found else ""


def _free_note(vault, directory, title):
    """A note path whose name is not already taken. The second one is 'Title (2).md'."""
    for number in range(0, 30):
        suffix = "" if number == 0 else f" ({number + 1})"
        rel = f"{directory}/{title}{suffix}.md"
        try:
            if not os.path.exists(vault._abs(rel)):
                return rel
        except ValueError:
            break
    return f"{directory}/{title}.md"


def backup_file(vault_path, path):
    """Copy a note under .chatbox-backup before it is edited or moved."""
    if not vault_path or not path or not os.path.isfile(path):
        return ""
    rel = os.path.relpath(path, vault_path)
    if rel.startswith(".."):
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(vault_path, ".chatbox-backup", stamp, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.copy2(path, dest)
    return dest


def _note_paths(vault_path):
    paths = []
    if not vault_path or not os.path.isdir(vault_path):
        return paths
    for dirpath, dirs, files in os.walk(vault_path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".md") and not name.startswith("."):
                paths.append(os.path.join(dirpath, name))
    return paths


def _swap_wikilink(text, old, new):
    pattern = re.compile(
        r"\[\[((?:[^\]|#]+/)*)" + re.escape(old) + r"((?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]"
    )
    return pattern.sub(lambda match: "[[" + match.group(1) + new + match.group(2) + "]]", text)


def retarget_links(vault_path, old_names, new_name):
    """Point [[old]] at [[new]] without changing any other text."""
    new_name = str(new_name or "").strip()
    if not new_name:
        return 0
    changed = 0
    for path in _note_paths(vault_path):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        updated = text
        for old in old_names or []:
            if old and old != new_name:
                updated = _swap_wikilink(updated, old, new_name)
        if updated != text:
            backup_file(vault_path, path)
            _atomic(path, updated)
            changed += 1
    return changed


def remove_wikilinks(vault_path, names):
    """Drop links to notes that are gone, so the graph has no ghost nodes."""
    names = [name for name in (names or []) if name]
    if not names:
        return 0
    changed = 0
    for path in _note_paths(vault_path):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        kept = []
        dirty = False
        for line in text.splitlines():
            stripped = line.strip()
            if any(re.fullmatch(r"[-*]?\s*\[\[" + re.escape(name) + r"(?:\|[^\]]+)?\]\]", stripped)
                   for name in names):
                dirty = True
                continue
            new_line = line
            for name in names:
                new_line = re.sub(
                    r"\[\[" + re.escape(name) + r"(?:\|([^\]]+))?\]\]",
                    lambda match, name=name: match.group(1) or name,
                    new_line,
                )
            if new_line != line:
                dirty = True
            kept.append(new_line)
        if dirty:
            backup_file(vault_path, path)
            body = "\n".join(kept)
            if text.endswith("\n"):
                body += "\n"
            _atomic(path, body)
            changed += 1
    return changed


def promote_project(vault_path, name):
    """Turn Projects/<Name>.md into Projects/<Name>/<Name>.md when a second note is needed."""
    name = safe_project_name(name)
    if not name or not vault_path or not os.path.isdir(vault_path):
        return False
    root = Vault(vault_path).projects_root()
    lone = os.path.join(vault_path, root, name + ".md")
    folder = os.path.join(vault_path, root, name)
    hub = os.path.join(folder, name + ".md")
    if os.path.isdir(folder) or not os.path.isfile(lone):
        return False
    backup_file(vault_path, lone)
    os.makedirs(folder, exist_ok=True)
    os.replace(lone, hub)
    retarget_links(vault_path, [f"{root}/{name}"], f"{root}/{name}/{name}")
    _MAPS.pop(os.path.abspath(vault_path), None)
    return True


EDITORS = (
    ("claude", "Claude", "Chats sent to Claude from Chatbox."),
    ("cursor", "Cursor", "Chats sent to Cursor from Chatbox."),
    ("codex", "Codex", "Chats sent to Codex from Chatbox."),
    ("chatgpt", "ChatGPT", "Chats sent to ChatGPT from Chatbox."),
    ("ollama", "Ollama", "Local Ollama chats from Chatbox. Model names are grouped on this note."),
)
_CHILDREN_BEGIN = re.compile(r"%%\s*chatbox:children\s*%%")
_CHILDREN_END = re.compile(r"%%\s*/chatbox:children\s*%%")


def ensure_bots_hub(vault_path):
    """Bots/Bots.md for every vault, plus Inbox and a Home link. No editor notes yet."""
    if not vault_path or not os.path.isdir(vault_path):
        return
    os.makedirs(os.path.join(vault_path, "Bots"), exist_ok=True)
    hub = os.path.join(vault_path, "Bots", "Bots.md")
    if not os.path.isfile(hub):
        _atomic(hub, "# Bots\nBack to [[Home]]\n\nOne note per editor Chatbox has actually used.\n")
    else:
        _ensure_substring(hub, "Back to [[Home]]", "\nBack to [[Home]]\n")
    inbox = os.path.join(vault_path, "Inbox.md")
    if not os.path.isfile(inbox):
        _atomic(inbox, "# Inbox\nBack to [[Home]]\n\nNotes that are not part of a project yet.\n")
    home = os.path.join(vault_path, "Home.md")
    if not os.path.isfile(home):
        _atomic(home, "# Home\n\n- [[Inbox]]\n- [[Projects]]\n- [[Bots]]\n")
    else:
        for link in ("[[Inbox]]", "[[Projects]]", "[[Bots]]"):
            _ensure_substring(home, link, f"\n- {link}\n")


def refresh_bots(vault_path, chats):
    """Editor notes only for backends that appear in real chats, with links that resolve."""
    if not vault_path or not os.path.isdir(vault_path):
        return
    ensure_bots_hub(vault_path)
    grouped = {key: {"models": [], "titles": []} for key, _label, _blurb in EDITORS}
    for chat in chats or []:
        if chat.get("exists") is False:
            continue
        key = str(chat.get("backend") or "").lower()
        if key not in grouped:
            continue
        title = str(chat.get("title") or "").strip()
        if not title:
            continue
        bucket = grouped[key]
        for model in chat.get("models") or []:
            if model and model not in bucket["models"]:
                bucket["models"].append(model)
        bucket["titles"].append((chat.get("updated") or 0, title))
    present = []
    for key, label, blurb in EDITORS:
        bucket = grouped[key]
        if not bucket["titles"]:
            continue
        path = os.path.join(vault_path, "Bots", label + ".md")
        bucket["titles"].sort(key=lambda item: item[0], reverse=True)
        stems, seen = [], set()
        for _updated, title in bucket["titles"]:
            if title in seen:
                continue
            seen.add(title)
            stems.append(title)
        lines = ["## Recent chats", ""]
        if bucket["models"]:
            lines.append("Models: " + ", ".join(bucket["models"][:12]))
            lines.append("")
        lines.extend(f"- [[{stem}]]" for stem in stems[:MAX_HUB_CHATS])
        block = CHATS_BEGIN + "\n" + "\n".join(lines) + "\n" + CHATS_END + "\n"
        if not os.path.isfile(path):
            _atomic(path, f"# {label}\nBack to [[Bots]]\n\n{blurb}\n\n{block}")
        else:
            try:
                with open(path, encoding="utf-8") as handle:
                    current = handle.read()
            except OSError:
                continue
            updated = _splice_marked(current, block, _CHATS_BEGIN, _CHATS_END)
            if updated != current:
                backup_file(vault_path, path)
                _atomic(path, updated)
        if os.path.isfile(path):
            present.append(label)
    hub = os.path.join(vault_path, "Bots", "Bots.md")
    if not os.path.isfile(hub):
        return
    child_lines = [f"- [[{label}]]" for label in present
                   if os.path.isfile(os.path.join(vault_path, "Bots", label + ".md"))]
    block = "%% chatbox:children %%\n" + ("\n".join(child_lines) + "\n" if child_lines else "") + "%% /chatbox:children %%\n"
    try:
        with open(hub, encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        return
    updated = _splice_marked(current, block, _CHILDREN_BEGIN, _CHILDREN_END)
    if updated != current:
        backup_file(vault_path, hub)
        _atomic(hub, updated)


def migrate_legacy_chatbox(vault_path):
    """Move a top-level Chatbox folder into Projects/Chatbox without dropping files.

    When the destination already has a file, the incoming one is kept beside it
    under a new name. A short note is left at the old folder so links still explain
    where the notes went. [[Chatbox]] keeps resolving to the hub by its name.
    """
    if not vault_path or not os.path.isdir(vault_path):
        return {"moved": False}
    src = os.path.join(vault_path, "Chatbox")
    dest = os.path.join(vault_path, PROJECTS_DIR, "Chatbox")
    if not os.path.isdir(src):
        return {"moved": False}
    if os.path.realpath(src) == os.path.realpath(dest):
        return {"moved": False}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        os.replace(src, dest)
    else:
        _merge_tree(src, dest)
        _remove_empty(src)
    stub_dir = os.path.join(vault_path, "Chatbox")
    stub = os.path.join(stub_dir, "Moved.md")
    if not os.path.isfile(stub):
        os.makedirs(stub_dir, exist_ok=True)
        target = "[[Chatbox]]" if os.path.isfile(os.path.join(dest, "Chatbox.md")) else "Projects/Chatbox"
        _atomic(stub, f"# Moved\n\nChatbox notes now live in {target}.\n")
    _MAPS.pop(os.path.abspath(vault_path), None)
    return {"moved": True}


def _merge_tree(src, dest):
    try:
        names = list(os.listdir(src))
    except OSError:
        return
    os.makedirs(dest, exist_ok=True)
    for name in names:
        if name in (".", ".."):
            continue
        source = os.path.join(src, name)
        target = os.path.join(dest, name)
        if os.path.isdir(source) and not os.path.islink(source):
            if os.path.isdir(target) and not os.path.islink(target):
                _merge_tree(source, target)
                _remove_empty(source)
                continue
            if not os.path.exists(target):
                os.replace(source, target)
                continue
        if os.path.exists(target):
            if os.path.isfile(source) and os.path.isfile(target) and _same_bytes(source, target):
                try:
                    os.remove(source)
                except OSError:
                    pass
                continue
            target = _spare_name(target)
        try:
            os.replace(source, target)
        except OSError:
            continue


def _same_bytes(left, right):
    try:
        if os.path.getsize(left) != os.path.getsize(right):
            return False
        with open(left, "rb") as src, open(right, "rb") as dest:
            return src.read() == dest.read()
    except OSError:
        return False


def _spare_name(path):
    base, ext = os.path.splitext(path)
    for number in range(1, 50):
        candidate = f"{base} (kept {number}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return path + ".kept"


def _remove_empty(path):
    if not os.path.isdir(path):
        return
    for dirpath, dirs, files in os.walk(path, topdown=False):
        if not dirs and not files:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

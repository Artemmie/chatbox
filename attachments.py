"""Files and images attached to a Chatbox message.

Uploads stay on this computer (under the Chatbox data folder). Each backend is
told about them in the way that backend can actually accept.
"""
import base64
import os
import re
import uuid

MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 8
MAX_TOTAL = 20 * 1024 * 1024
MAX_TEXT = 100_000  # characters pasted into a chat that has no filesystem
ID = re.compile(r"^[a-f0-9]{16}$")
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".py", ".js", ".ts", ".tsx",
            ".jsx", ".css", ".html", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
            ".rs", ".go", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
            ".sql", ".log", ".env", ".gitignore", ".svg"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}


def _dir(data_dir):
    return os.path.join(data_dir, "uploads")


def kind_of(name, mime, raw):
    ext = os.path.splitext(name or "")[1].lower()
    mime = (mime or "").lower()
    if mime.startswith("image/") or ext in IMAGE_EXT:
        return "image"
    if mime == "application/pdf" or ext == ".pdf":
        return "pdf"
    if mime.startswith("text/") or ext in TEXT_EXT:
        return "text"
    # A file with no useful type that is valid UTF-8 is treated as text.
    if b"\x00" not in raw[:4096]:
        try:
            raw[:4096].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "other"


def save(data_dir, name, mime, b64):
    """Store one upload. Returns a small descriptor or raises ValueError."""
    name = os.path.basename(str(name or "file").replace("\\", "/")).strip() or "file"
    if len(name) > 180:
        name = name[-180:]
    try:
        raw = base64.b64decode(b64 or "", validate=False)
    except (ValueError, TypeError):
        raise ValueError("That file could not be read.") from None
    if not raw:
        raise ValueError(f"{name} is empty.")
    if len(raw) > MAX_FILE:
        raise ValueError(f"{name} is over 8 MB.")
    folder = _dir(data_dir)
    os.makedirs(folder, exist_ok=True)
    total = 0
    try:
        for entry in os.listdir(folder):
            if entry.endswith(".bin"):
                total += os.path.getsize(os.path.join(folder, entry))
    except OSError:
        total = 0
    if total + len(raw) > MAX_TOTAL:
        raise ValueError("Attachments are over 20 MB in total. Remove one and try again.")
    fid = uuid.uuid4().hex[:16]
    kind = kind_of(name, mime, raw)
    meta = {"id": fid, "name": name, "type": mime or "", "kind": kind, "size": len(raw)}
    with open(os.path.join(folder, fid + ".bin"), "wb") as f:
        f.write(raw)
    with open(os.path.join(folder, fid + ".json"), "w", encoding="utf-8") as f:
        import json
        json.dump(meta, f)
    return meta


def load(data_dir, fid):
    if not ID.match(fid or ""):
        raise ValueError("Unknown attachment.")
    folder = _dir(data_dir)
    try:
        import json
        with open(os.path.join(folder, fid + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(folder, fid + ".bin"), "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        raise ValueError("That attachment is no longer available.") from None
    meta["raw"] = raw
    return meta


def load_many(data_dir, ids):
    ids = ids or []
    if len(ids) > MAX_FILES:
        raise ValueError(f"You can attach up to {MAX_FILES} files.")
    return [load(data_dir, fid) for fid in ids]


def _text(item):
    try:
        return item["raw"].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{item['name']} is not a text file this assistant can read inline.") from None


def _safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return cleaned[:80]


def stage_for_cli(workdir, items):
    """Copy attachments into the working folder and return a note for the prompt."""
    folder = os.path.join(os.path.abspath(workdir), ".chatbox-attach")
    os.makedirs(folder, exist_ok=True)
    lines = []
    for item in items:
        dest = os.path.join(folder, item["id"][:8] + "-" + _safe_name(item["name"]))
        with open(dest, "wb") as f:
            f.write(item["raw"])
        lines.append(f"- {dest} ({item['kind']}, {item['name']})")
    return ("The user attached these files. Read each one before you answer "
            "(images included, if you can view them):\n" + "\n".join(lines))


def for_chatgpt(items):
    """Parts the Responses API accepts. Raises ValueError if one of them is not allowed."""
    bad = [i["name"] for i in items if i["kind"] not in ("text", "image", "pdf")]
    if bad:
        raise ValueError("ChatGPT can't take " + ", ".join(bad) + ". It can take text, images, and PDFs.")
    out = []
    for item in items:
        if item["kind"] == "text":
            text = _text(item)
            if len(text) > MAX_TEXT:
                raise ValueError(f"{item['name']} is too long to paste into ChatGPT (limit 100,000 characters).")
            out.append({"kind": "text", "name": item["name"], "text": text})
        elif item["kind"] == "image":
            mime = item.get("type") or IMAGE_EXT.get(os.path.splitext(item["name"])[1].lower()) or "image/png"
            if not mime.startswith("image/"):
                mime = "image/png"
            out.append({"kind": "image", "name": item["name"],
                        "data_url": "data:" + mime + ";base64," + base64.b64encode(item["raw"]).decode()})
        else:
            out.append({"kind": "pdf", "name": item["name"],
                        "file_data": "data:application/pdf;base64," + base64.b64encode(item["raw"]).decode()})
    return out


def for_ollama(items, vision):
    """(extra text, [base64 images]). vision is True, False, or None (unknown)."""
    images, texts = [], []
    for item in items:
        if item["kind"] == "text":
            text = _text(item)
            if len(text) > MAX_TEXT:
                raise ValueError(f"{item['name']} is too long to paste into Ollama (limit 100,000 characters).")
            texts.append(f"Attached file {item['name']}:\n\n{text}")
        elif item["kind"] == "image":
            if vision is False:
                raise ValueError(f"Ollama can't view {item['name']} with this model. Pick a vision model, or remove the image.")
            images.append(base64.b64encode(item["raw"]).decode())
        else:
            raise ValueError(f"Ollama can't take {item['name']}. It can take text, and images when the model supports them.")
    extra = ("\n\n".join(texts)) if texts else ""
    note = ""
    if images and vision is None:
        note = "\n\n(The image is attached. If this model cannot view images, say so.)"
    return extra + note, images


def ollama_note(items, vision):
    if vision is False and any(i["kind"] == "image" for i in items):
        return "This Ollama model can't view images."
    if any(i["kind"] not in ("text", "image") for i in items):
        return "Ollama can take text, and images only when the model supports them."
    return ""

"""Compact transcript so a fresh assistant session can join a chat mid-conversation.

Cursor, Claude and Codex remember a chat only inside the CLI session they resume.
ChatGPT and Ollama remember only the history stored for that session id. A handoff
starts a new session, so the earlier turns have to travel in the prompt: who spoke,
which assistant answered, trimmed to a fixed budget.
"""
import re

# About three thousand tokens. Leaves room for memories, vault notes and the new question.
BUDGET = 12000
# Turns kept closer to full size. Older ones become a sentence, then a one-line digest.
RECENT = 6
NEWEST_CAP = 8000
OTHER_CAP = 1600
SUMMARY = 180
DIGEST = 70
MAX_TURNS = 400

NAMES = {
    "cursor": "Cursor",
    "claude": "Claude",
    "codex": "Codex",
    "chatgpt": "ChatGPT",
    "ollama": "Ollama",
}

HEADER = (
    "[Earlier conversation in this chat. Each line names who spoke and which "
    "assistant answered. You are joining mid-conversation and do not already "
    "have these turns. Use them as context and do not quote this block back.]\n\n"
)
FOOTER = "\n\n[End of earlier conversation]\n\n"


def _flat(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clip(text, limit):
    """Keep the start of text. A cut always ends with an ellipsis so it is visibly trimmed."""
    text = _flat(text)
    limit = int(limit)
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    if limit < 2:
        return text[:limit]
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    if len(cut) < max(1, (limit - 1) // 2):
        cut = text[:limit - 1].rstrip()
    return cut + "…"


def _summary(text, limit):
    """First sentence when it fits, otherwise the clipped start. This is the summary of an old turn."""
    text = _flat(text)
    if len(text) <= limit:
        return text
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if sentence and len(sentence) <= limit:
        if sentence != text and not sentence.endswith((".", "!", "?", "…")):
            return sentence + "…"
        return sentence
    return _clip(text, limit)


def _speaker(item):
    role = item.get("r") or item.get("role")
    if role == "you":
        return "You"
    if role not in ("bot", "err", "assistant"):
        return ""
    name = NAMES.get(item.get("b") or item.get("backend") or "", "Assistant")
    model = item.get("model")
    if isinstance(model, str):
        model = re.sub(r"\s+", " ", model).strip(" ·")
        if model and model.lower() != name.lower():
            name = f"{name} ({model[:80]})"
    if role == "err":
        return name + " (error)"
    return name


def _turn_text(item):
    text = item.get("t")
    if text is None:
        text = item.get("text") or ""
    text = str(text).strip()
    names = []
    files = item.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = re.sub(r"\s+", " ", str(entry.get("name") or "")).strip()
            if name:
                names.append(name[:120])
    if names:
        suffix = "[attached: " + ", ".join(names) + "]"
        text = (text + "\n" + suffix).strip() if text else suffix
    return text


def _turns(items):
    if not isinstance(items, list):
        return []
    if len(items) > MAX_TURNS:
        items = items[-MAX_TURNS:]
    turns = []
    for item in items:
        if not isinstance(item, dict) or item.get("temp"):
            continue
        who = _speaker(item)
        if not who:
            continue
        text = _turn_text(item)
        if text:
            turns.append((who, text))
    return turns


def _pack(lines, notice, budget):
    body = ""
    if notice:
        body += notice.rstrip() + "\n"
    body += "\n".join(lines)
    text = HEADER + body.rstrip() + FOOTER
    if len(text) <= budget:
        return text
    return None


def compact_transcript(items, budget=BUDGET):
    """Transcript of prior turns, or '' when there is nothing to carry.

    Recent turns stay readable. Older turns are shortened to a sentence, and if
    that still exceeds the budget the oldest ones collapse into one digest line.
    The result is never longer than `budget` characters.
    """
    turns = _turns(items)
    if not turns:
        return ""
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        budget = BUDGET
    if budget < 400:
        budget = 400

    count = len(turns)
    full = []
    for index, (who, text) in enumerate(turns):
        cap = budget if count == 1 else (NEWEST_CAP if index == count - 1 else OTHER_CAP)
        full.append(f"{who}: {_clip(text, cap)}")
    packed = _pack(full, "", budget)
    if packed:
        return packed

    split = max(0, count - RECENT)
    mixed = []
    for index, (who, text) in enumerate(turns):
        if index < split:
            mixed.append(f"{who}: {_summary(text, SUMMARY)}")
        elif index == count - 1:
            mixed.append(f"{who}: {_clip(text, NEWEST_CAP)}")
        else:
            mixed.append(f"{who}: {_clip(text, OTHER_CAP)}")
    notice = "[Older turns were summarized to fit.]" if split else ""
    packed = _pack(mixed, notice, budget)
    if packed:
        return packed

    for drop in range(1, len(mixed)):
        kept = mixed[drop:]
        bits = [f"{who}: {_summary(text, DIGEST)}" for who, text in turns[:drop][:8]]
        extra = f" and {drop - 8} more" if drop > 8 else ""
        notice = (f"[Earlier {drop} turn{'s' if drop != 1 else ''} summarized away to fit: "
                  + "; ".join(bits) + extra + ".]")
        packed = _pack(kept, notice, budget)
        if packed:
            return packed
        notice = f"[Earlier {drop} turn{'s' if drop != 1 else ''} summarized away to fit.]"
        packed = _pack(kept, notice, budget)
        if packed:
            return packed

    who, text = turns[-1]
    room = budget - len(HEADER) - len(FOOTER) - len(who) - 2
    line = f"{who}: {_clip(text, max(20, room))}"
    packed = _pack([line], "", budget)
    if packed:
        return packed
    text = (HEADER + line + FOOTER)[:budget - 1].rstrip()
    return text + "…"

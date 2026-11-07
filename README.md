# Chatbox

One chat window for **Cursor**, **Claude**, **Codex** and **ChatGPT** on Linux — using the subscriptions you already pay for. No API keys.

Pick an assistant with a button, chat, switch to another one mid-conversation. Each keeps its own conversation.

## How it works

Chatbox doesn't talk to any AI service itself. Each message is handed to the official command-line tool for the assistant you picked, and that tool uses **your own sign-in**:

```
You type in Chatbox
      │
      ▼
chatbox.py (local server on 127.0.0.1)
      ├── Cursor  → cursor-agent  → your Cursor account
      ├── Claude  → claude        → your Claude Pro/Max subscription
      ├── Codex   → codex         → your ChatGPT account
      └── ChatGPT → codex, as a plain chat (no files, no commands)
```

Usage counts toward each service's plan, exactly as if you used its own app.

## Features

- **Four assistants**, one window. Switching keeps each conversation.
- **Ask / Agent mode** – Ask is read-only. Agent lets Cursor, Claude or Codex edit files and run commands in the folder you choose.
- **Model picker** – *Auto*, or any model your plan offers (Cursor's list is loaded live).
- **Shared memory** – save facts once ("I use Unity 6 and C#", "answer in Russian") and every assistant gets them at the start of each chat. Add them in **Settings**, type `/remember …`, click **Remember** under any message, or let the assistant save new facts by itself (with **Undo**).
- **Custom instructions** – one place to tell all four assistants how to reply.
- **Accounts screen** – see what's connected and **Install / Sign in / Sign out** each service without a terminal. Sign-in happens in your browser; Chatbox never sees your password.
- **Desktop app** – menu entry, icon and its own window.

## Requirements

- Linux with a desktop (tested on KDE Plasma / Wayland)
- Python 3.10+
- A subscription for each assistant you want: Cursor account, Claude Pro or Max, ChatGPT (Plus/Pro, or free with limits)

## Install

```bash
git clone https://github.com/Artemmie/chatbox.git
cd chatbox
./install.sh
```

Then open **Chatbox** from your app menu. On first start the **Accounts** screen opens: click **Install** and **Sign in** for the services you use.

Options:

| Command | What it does |
|---|---|
| `./install.sh` | Own window. Downloads PyQt6 + Qt WebEngine (~150 MB) into a private venv. |
| `./install.sh --browser` | No extra downloads. Opens Chatbox in your web browser. |
| `./install.sh --distrobox NAME` | Menu entry runs Chatbox inside a distrobox container. |

Quick try without installing:

```bash
python3 chatbox.py        # opens http://127.0.0.1:8765 in your browser
```

Uninstall with `./uninstall.sh` (your sign-ins and the tools stay).

## The tools it uses

| Assistant | Tool | Installed by the Accounts screen from |
|---|---|---|
| Cursor | `cursor-agent` | `https://cursor.com/install` |
| Claude | `claude` (Claude Code) | `https://claude.ai/install.sh` |
| Codex & ChatGPT | `codex` | OpenAI's GitHub releases → `~/.local/bin/codex` |

If you have the ChatGPT desktop app, Chatbox can also use the `codex` bundled inside it (set `CHATBOX_CODEX=/path/to/codex` if it isn't found). That copy may be older and accept fewer models.

## Privacy & security

- The server listens on `127.0.0.1` only and rejects requests from other websites.
- Chatbox stores no passwords, tokens or API keys. Each tool keeps its own sign-in in its usual place.
- In **Agent** mode the assistant can change files in the chosen folder — use it on folders you trust.
- ChatGPT mode always runs read-only from an empty folder.
- Memories and custom instructions are stored locally in `~/.local/share/chatbox/memories.json` and `settings.json`. They are sent only to the assistant you are chatting with, at the start of a chat (or when they change). Turn this off in **Settings**.

## Files

| File | Purpose |
|---|---|
| `chatbox.py` | Local server and the chat page |
| `accounts.py` | Status, install, sign-in and sign-out for each tool |
| `memory.py` | Settings, memories and custom instructions |
| `app.py` | Desktop window (PyQt6 WebEngine) |
| `install.sh` / `uninstall.sh` | Per-user install, no root needed |

## License

MIT

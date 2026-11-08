# Chatbox

One chat window for **Cursor**, **Claude**, **Codex** and **ChatGPT** on Linux and macOS — using the subscriptions you already pay for. No API keys.

A clean, modern chat window: sidebar with your saved chats, a centered conversation with proper formatting (headings, lists, tables, code with a Copy button), and one composer where you pick the assistant, mode, model and folder. Switch assistants mid-conversation — each keeps its own part of the chat.

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

- **Four assistants**, one window. Assistants that aren't connected are greyed out until you sign in.
- **Chat history** – every chat is saved and listed in the sidebar (Today, Yesterday, …). Reopen one and keep going: each assistant resumes its own session, in the chat's own folder.
- **Ask / Plan / Agent** – *Ask* answers and may read files. *Plan* writes an implementation plan and saves it as a Markdown file (`<folder>/plans/…md`) for you to review — nothing is changed yet. Click **Implement plan** to hand it to *Agent*, which edits files and runs commands and ticks off the plan's checklist as it goes.
- **Model picker** – *Auto*, or any model your plan offers (Cursor's list is loaded live).
- **Shared memory** – save facts once ("I use Unity 6 and C#", "answer in Russian") and every assistant gets them at the start of each chat. Add them in **Settings**, type `/remember …`, click **Remember** under any message, or let the assistant save new facts by itself (with **Undo**).
- **Custom instructions** – one place to tell all four assistants how to reply.
- **Obsidian as long-term memory** – connect your vault (Accounts → Obsidian; vaults are detected automatically). Memories then live in `Chatbox/Memories.md` in your vault, shared by every assistant and editable in Obsidian. Notes related to your message are found and given to the assistant, and **Save to Obsidian** stores a whole chat in `Chatbox/Chats/`.
- **Usage** – messages, tokens, reply time and (for Claude) cost per assistant and model, with a daily chart and a 7 / 30 day / all-time view. Each reply also shows its tokens, time and cost. Subscription limits aren't visible to the command-line tools, so they aren't shown.
- **Accounts screen** – see what's connected and **Install / Sign in / Sign out** each service without a terminal. Sign-in happens in your browser; Chatbox never sees your password.
- **Desktop app** – its own window and icon: a menu entry on Linux, `Chatbox.app` on macOS.

## Requirements

- Linux with a desktop (tested on KDE Plasma / Wayland), or macOS (Apple silicon or Intel; new, not yet tested on a real Mac)
- Python 3.9+ (on macOS: `xcode-select --install`, or python.org / Homebrew)
- A subscription for each assistant you want: Cursor account, Claude Pro or Max, ChatGPT (Plus/Pro, or free with limits)

## Install

```bash
git clone https://github.com/Artemmie/chatbox.git
cd chatbox
./install.sh
```

Then open **Chatbox** from your app menu (Linux) or from Applications / Launchpad / Spotlight (macOS). On first start the **Accounts** screen opens: click **Install** and **Sign in** for the services you use.

Options:

| Command | What it does |
|---|---|
| `./install.sh` | Own window. Downloads PyQt6 + Qt WebEngine (~150 MB) into a private venv. |
| `./install.sh --browser` | No extra downloads. Opens Chatbox in your web browser. |
| `./install.sh --distrobox NAME` | Linux only: menu entry runs Chatbox inside a distrobox container. |

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
- With Obsidian connected, notes that match your message are sent to the assistant you are chatting with (turn off in **Settings**). Chatbox only writes inside `Chatbox/` in your vault, plus `plans/` in the project folder you choose.
- Usage statistics are stored locally in `~/.local/share/chatbox/usage.jsonl` (no message text, only counts). Clear them in **Usage**.
- Chats are saved locally in `~/.local/share/chatbox/chats/` (one JSON file each). Delete them from the sidebar.
- Memories and custom instructions are stored locally in `~/.local/share/chatbox/memories.json` and `settings.json`. They are sent only to the assistant you are chatting with, at the start of a chat (or when they change). Turn this off in **Settings**.

## Files

| File | Purpose |
|---|---|
| `chatbox.py` | Local server: chat, history, settings and account APIs |
| `ui.html` | The interface (single page, no external files) |
| `accounts.py` | Status, install, sign-in and sign-out for each tool |
| `memory.py` | Settings, memories and custom instructions |
| `usage.py` | Usage log and summaries for the Usage screen |
| `vault.py` | Obsidian vault: detection, memory note, note search, saving chats |
| `app.py` | Desktop window (PyQt6 WebEngine) |
| `install.sh` / `uninstall.sh` | Per-user install, no root needed |

## License

MIT

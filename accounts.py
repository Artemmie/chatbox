"""Accounts for Chatbox: check sign-in status, and run install / sign-in / sign-out
for each service's command-line tool.

Sign-in runs the tool's own login command in a pseudo-terminal (some tools need one),
so the tool opens the browser itself. The page shows its link and, for Claude, a box
to paste the code the browser shows.
"""
import json
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\r")
URL = re.compile(r"https://[^\s'\"<>]+")
JOB_LIMIT = 15 * 60  # seconds before an unfinished sign-in is stopped

INSTALL = {
    "cursor": "curl https://cursor.com/install -fsS | bash",
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    # Standalone Codex from OpenAI's GitHub releases, into ~/.local/bin/codex.
    "codex": ('set -e; a=$(uname -m); case $a in aarch64|arm64) a=aarch64;; *) a=x86_64;; esac; '
              'n=codex-$a-unknown-linux-musl; t=$(mktemp -d); '
              'echo "Downloading $n…"; '
              'curl -fsSL -o "$t/c.tgz" "https://github.com/openai/codex/releases/latest/download/$n.tar.gz"; '
              'tar xzf "$t/c.tgz" -C "$t"; mkdir -p "$HOME/.local/bin"; '
              'install -m755 "$t/$n" "$HOME/.local/bin/codex"; rm -rf "$t"; '
              '"$HOME/.local/bin/codex" --version; echo "Installed to ~/.local/bin/codex"'),
}

SERVICES = {
    "cursor": {"name": "Cursor", "plan": "Cursor account (free or paid plan)", "exe": "cursor-agent"},
    "claude": {"name": "Claude", "plan": "Claude Pro or Max subscription", "exe": "claude"},
    "codex": {"name": "Codex & ChatGPT", "plan": "ChatGPT account (Plus/Pro, or free with limits)",
              "exe": "codex"},
}


class Job:
    """One running install / login command, read through a pseudo-terminal."""

    def __init__(self, service, kind, argv, on_done):
        self.service, self.kind = service, kind
        self.output, self.url, self.state, self.code = "", None, "running", None
        self.started = time.time()
        self._on_done = on_done
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")
            os.environ["TERM"] = "dumb"
            try:
                os.chdir(os.path.expanduser("~"))
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            if time.time() - self.started > JOB_LIMIT:
                self.cancel()
            try:
                ready, _, _ = select.select([self.fd], [], [], 1)
            except (OSError, ValueError):
                break
            if not ready:
                if self._exited():
                    break
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output = (self.output + ANSI.sub("", chunk.decode(errors="replace")))[-20000:]
            if not self.url:
                m = URL.search(self.output)
                if m:
                    self.url = m.group(0).rstrip(".,)")
        self._exited(wait=True)
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.state == "running":
            self.state = "done" if self.code == 0 else "failed"
        self._on_done(self)

    def _exited(self, wait=False):
        if self.code is not None:
            return True
        try:
            pid, status = os.waitpid(self.pid, 0 if wait else os.WNOHANG)
        except ChildProcessError:
            self.code = self.code if self.code is not None else 1
            return True
        if pid:
            self.code = os.waitstatus_to_exitcode(status)
            return True
        return False

    def send(self, text):
        os.write(self.fd, (text.strip() + "\r").encode())

    def cancel(self):
        self.state = "cancelled"
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def view(self):
        tail = "\n".join(l for l in self.output.strip().splitlines() if l.strip())[-1500:]
        asks_code = self.service == "claude" and self.kind == "login" and "paste code" in self.output.lower()
        # Last message without a link, e.g. "Invalid code…" or "Login successful".
        last = next((l.strip() for l in reversed(tail.splitlines())
                     if l.strip() and "https://" not in l), "")
        last = last.replace("Paste code here if prompted >", "").strip()
        return {"kind": self.kind, "state": self.state, "url": self.url, "output": tail,
                "last": last[:300], "asks_code": asks_code and self.state == "running"}


class Accounts:
    def __init__(self, find):
        self.find = find  # find(exe_name) -> path or None
        self.jobs = {}
        self._cache, self._cache_time = None, 0
        self.lock = threading.Lock()

    # ---- status -------------------------------------------------------
    def _check(self, sid):
        exe = self.find(SERVICES[sid]["exe"])
        info = {"installed": bool(exe), "logged_in": False, "account": None, "source": None}
        if not exe:
            return info
        if sid == "codex" and exe.endswith("resources/codex"):
            info["source"] = "built into the ChatGPT app"
        argv = {"cursor": [exe, "status"], "claude": [exe, "auth", "status"],
                "codex": [exe, "login", "status"]}[sid]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            out = ANSI.sub("", p.stdout + p.stderr)
        except (subprocess.TimeoutExpired, OSError) as e:
            info["error"] = str(e)
            return info
        if sid == "claude":
            try:
                data = json.loads(out[out.index("{"):])
                info["logged_in"] = bool(data.get("loggedIn"))
                info["account"] = data.get("email") or data.get("emailAddress") or data.get("account")
                if data.get("authMethod") not in (None, "none", "claude.ai", "oauth"):
                    info["account"] = (info["account"] or "") + f" ({data.get('authMethod')})"
            except ValueError:
                pass
        elif sid == "cursor":
            m = re.search(r"Logged in as (\S+)", out)
            info["logged_in"], info["account"] = bool(m), m and m.group(1)
        else:
            info["logged_in"] = p.returncode == 0 and "not logged in" not in out.lower()
            m = re.search(r"Logged in using (.+)", out)
            info["account"] = m and m.group(1).strip()
        return info

    def status(self, fresh=False):
        with self.lock:
            if fresh or not self._cache or time.time() - self._cache_time > 20:
                results = {}
                threads = [threading.Thread(target=lambda s=s: results.__setitem__(s, self._check(s)))
                           for s in SERVICES]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self._cache, self._cache_time = results, time.time()
            out = []
            for sid, meta in SERVICES.items():
                job = self.jobs.get(sid)
                out.append({"id": sid, "name": meta["name"], "plan": meta["plan"],
                            **self._cache[sid], "job": job.view() if job else None})
            return out

    def _job_done(self, job):
        self._cache_time = 0  # re-check status on next request

    # ---- actions ------------------------------------------------------
    def action(self, sid, action, text=""):
        if sid not in SERVICES:
            raise ValueError("Unknown service")
        job = self.jobs.get(sid)
        if action == "cancel":
            if job and job.state == "running":
                job.cancel()
            return
        if action == "dismiss":
            if job and job.state != "running":
                self.jobs.pop(sid, None)
            return
        if action == "input":
            if not job or job.state != "running":
                raise ValueError("Nothing is waiting for input.")
            job.send(text)
            return
        if job and job.state == "running":
            raise ValueError("Already running — finish or cancel it first.")
        exe = self.find(SERVICES[sid]["exe"])
        if action == "install":
            argv = ["bash", "-c", INSTALL[sid]]
        elif not exe:
            raise ValueError(f"{SERVICES[sid]['name']} is not installed yet.")
        elif action == "login":
            argv = {"cursor": [exe, "login"], "claude": [exe, "auth", "login", "--claudeai"],
                    "codex": [exe, "login"]}[sid]
        elif action == "logout":
            argv = {"cursor": [exe, "logout"], "claude": [exe, "auth", "logout"],
                    "codex": [exe, "logout"]}[sid]
        else:
            raise ValueError("Unknown action")
        self.jobs[sid] = Job(sid, action, argv, self._job_done)


def open_url(url):
    """Open an https link in the user's browser (from inside a distrobox, too)."""
    if not url.startswith("https://"):
        raise ValueError("Only https links can be opened.")
    for argv in (["xdg-open", url], ["distrobox-host-exec", "xdg-open", url]):
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError:
            continue
    import webbrowser
    webbrowser.open(url)

"""Paths, tool lookup, and process control that differ on Windows.

Linux and macOS keep the same directories and process groups as before.
Windows uses %LOCALAPPDATA%\\Chatbox, .cmd/.exe shims, and taskkill for a process tree.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def source_root():
    """Folder that holds ui/ and version.txt. Inside a PyInstaller build this is the bundle."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir(system=None, environ=None, home=None):
    """Where chats, accounts and settings live. CHATBOX_DATA always wins."""
    system = system or os.name
    environ = os.environ if environ is None else environ
    override = environ.get("CHATBOX_DATA")
    if override:
        return str(Path(override))
    if home is None:
        home = str(Path.home())
    if system == "nt":
        base = environ.get("LOCALAPPDATA") or str(Path(home) / "AppData" / "Local")
        return str(Path(base) / "Chatbox")
    # Always slash-separated, including when this branch is unit-tested on Windows.
    return str(home).replace("\\", "/").rstrip("/") + "/.local/share/chatbox"


def windows_tool_dirs(environ=None, home=None):
    """Folders that hold npm shims and per-user installs on Windows."""
    environ = os.environ if environ is None else environ
    home = Path(home) if home else Path.home()
    found = []
    appdata = environ.get("APPDATA")
    local = environ.get("LOCALAPPDATA")
    if appdata:
        found.append(str(Path(appdata) / "npm"))
    if local:
        found.append(str(Path(local) / "npm"))
    found.append(str(home / ".local" / "bin"))
    return found


def find_in_dirs(name, directories, suffixes):
    for folder in directories:
        if not folder:
            continue
        for suffix in suffixes:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate):
                return candidate
    return None


def codex_app_candidates(home, environ=None):
    """Extra Codex binaries shipped inside the ChatGPT app. POSIX list is unchanged."""
    environ = os.environ if environ is None else environ
    candidates = [
        environ.get("CHATBOX_CODEX", ""),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        os.path.join(home, "Applications/ChatGPT.app/Contents/Resources/codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
        os.path.join(home, "Applications/Codex.app/Contents/Resources/codex"),
        os.path.join(home, "Applications/chatgpt/usr/lib/chatgpt/resources/codex"),
        os.path.join(home, "Applications/ChatGPT/resources/codex"),
        "/opt/ChatGPT/resources/codex",
        "/opt/chatgpt/resources/codex",
        "/usr/lib/chatgpt/resources/codex",
    ]
    if os.name == "nt":
        local = environ.get("LOCALAPPDATA") or ""
        candidates.extend([
            os.path.join(local, "Programs", "ChatGPT", "resources", "codex.exe"),
            os.path.join(local, "Programs", "Codex", "resources", "codex.exe"),
        ])
    return candidates


def executable(path):
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


_SHIM = re.compile(r'"%~dp0\\([^"\r\n]+)"\s+"%~dp0\\([^"\r\n]+)"')


def _inside(parent, child):
    parent = os.path.normcase(os.path.abspath(parent))
    child = os.path.normcase(os.path.abspath(child))
    return child == parent or child.startswith(parent + os.sep)


def unwrap_windows_shim(path):
    """An npm `.cmd` that only forwards to node. Returns [node, script], or None.

    cmd.exe cannot pass a prompt that contains newlines, so the real program is started directly.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(4000)
    except OSError:
        return None
    match = _SHIM.search(text)
    if not match:
        return None
    folder = os.path.dirname(os.path.abspath(path))
    exe = os.path.normpath(os.path.join(folder, match.group(1)))
    script = os.path.normpath(os.path.join(folder, match.group(2)))
    if not (_inside(folder, exe) and _inside(folder, script)):
        return None
    if os.path.isfile(exe) and os.path.isfile(script):
        return [exe, script]
    return None


def prepare_argv(argv, system=None):
    """How to start this command. POSIX is unchanged. Windows avoids cmd.exe when it can."""
    system = system or os.name
    argv = list(argv)
    if system != "nt" or not argv:
        return argv
    path = str(argv[0])
    lower = path.lower()
    if lower.endswith(".py"):
        return [sys.executable, *argv]
    if lower.endswith((".cmd", ".bat")):
        unwrapped = unwrap_windows_shim(path)
        if unwrapped:
            return unwrapped + argv[1:]
        return ["cmd.exe", "/c", *argv]
    return argv


def spawn(argv, system=None, **kwargs):
    """Start a child in its own process group. On Windows that is a new process group, not setsid."""
    system = system or os.name
    argv = prepare_argv(argv, system)
    if system == "nt":
        flags = int(kwargs.pop("creationflags", 0))
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = flags
        kwargs.pop("start_new_session", None)
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(argv, **kwargs)


def kill_tree(proc, system=None, killpg=None, runner=None):
    """Stop a child and anything it started. POSIX keeps the process-group signals."""
    system = system or os.name
    if proc is None:
        return
    if system == "nt":
        run = runner or subprocess.run
        run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return
    group_kill = killpg or os.killpg
    try:
        group_kill(proc.pid, 15)  # SIGTERM
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        group_kill(proc.pid, 9)  # SIGKILL
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def restrict_private(path):
    """Mode 0600 on POSIX. Windows ignores the Unix mode bits; do not fail the write."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def windows_install_message(name):
    return (f"On Windows, install {name} with its own Windows installer, then open Accounts again. "
            "Chatbox looks for it on PATH and in the npm global folder.")


def bash_exe():
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                          r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.isfile(candidate):
                return candidate
    return "bash"

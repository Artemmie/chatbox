"""Paths, tool lookup, and process control that differ on Windows.

Linux and macOS keep the same directories and process groups as before.
Windows uses %LOCALAPPDATA%\\Chatbox, .cmd/.exe shims, and taskkill for a process tree.
"""
import os
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
    return str(Path(home) / ".local" / "share" / "chatbox")


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


def spawn(argv, system=None, **kwargs):
    """Start a child in its own process group. On Windows that is a new process group, not setsid."""
    system = system or os.name
    if system == "nt":
        if argv and str(argv[0]).lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/c", *list(argv)]
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

"""Open a native folder dialog on this computer and return the chosen path.

The Settings page calls this through POST /api/pick-folder. The HTTP handler
runs on a worker thread, so a PyQt window registers a bridge and shows
QFileDialog on the GUI thread. Without that bridge, each OS gets its own dialog.
A dialog that never returns is stopped after PICK_TIMEOUT seconds.
"""
import os
import shutil
import subprocess
import sys

PICK_TIMEOUT = 120
_bridge = None

_OSA = r'''
on run argv
  set promptText to "Choose vault folder"
  try
    if (count of argv) > 0 and (item 1 of argv) is not "" then
      set chosen to choose folder with prompt promptText default location (POSIX file (item 1 of argv))
    else
      set chosen to choose folder with prompt promptText
    end if
  on error number -128
    return ""
  end try
  return POSIX path of chosen
end run
'''

_TK = r'''
import os, sys
initial = os.environ.get("CHATBOX_PICK_INITIAL", "")
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    sys.stderr.write(str(exc))
    sys.exit(2)
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
try:
    path = filedialog.askdirectory(title="Choose vault folder", initialdir=initial or None)
except Exception as exc:
    sys.stderr.write(str(exc))
    sys.exit(2)
sys.stdout.write(path or "")
try:
    root.destroy()
except Exception:
    pass
'''

_PS = r'''
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = "Choose vault folder"
$d.ShowNewFolderButton = $true
$start = $env:CHATBOX_PICK_INITIAL
if ($start -and (Test-Path -LiteralPath $start)) { $d.SelectedPath = $start }
$result = $d.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
'''


def set_bridge(fn):
    """Prefer this callable over the OS dialog. The PyQt window passes QFileDialog."""
    global _bridge
    _bridge = fn


def pick(initial="", timeout=None):
    """Return {path, cancelled} or {path:'', cancelled:False, error}."""
    limit = PICK_TIMEOUT if timeout is None else timeout
    bridge = _bridge
    if bridge is not None:
        try:
            return _normalize(bridge(initial or "", limit))
        except Exception as exc:
            return {"path": "", "cancelled": False, "error": str(exc)}
    system = sys.platform
    if system == "darwin":
        return _darwin(initial, limit)
    if system in ("win32", "cygwin"):
        return _windows(initial, limit)
    return _linux(initial, limit)


def _normalize(result):
    if isinstance(result, str):
        result = {"path": result}
    if not isinstance(result, dict):
        return {"path": "", "cancelled": True}
    error = str(result.get("error") or "").strip()
    if error:
        return {"path": "", "cancelled": False, "error": error}
    path = _clean(result.get("path") or "")
    if result.get("cancelled") or not path:
        return {"path": "", "cancelled": True}
    return {"path": path, "cancelled": False}


def _clean(path):
    path = str(path or "").strip().strip("\r")
    if len(path) > 1:
        path = path.rstrip("/\\")
    return path


def _existing_start(initial):
    raw = os.path.expanduser((initial or "").strip())
    if raw and os.path.isdir(raw):
        return os.path.abspath(raw)
    return ""


def _from_argv(argv, timeout, run=None, env=None):
    run = run or subprocess.run
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": False, "error": "The folder picker timed out."}
    except OSError as exc:
        return {"path": "", "cancelled": False, "error": str(exc)}
    path = _clean(getattr(proc, "stdout", "") or "")
    code = getattr(proc, "returncode", 1)
    if path and code == 0:
        return {"path": path, "cancelled": False}
    # failed: the tool exited with an error, which is not the user pressing Cancel.
    # macOS uses that to retry without a default location that osascript rejected.
    if code != 0:
        return {"path": "", "cancelled": True, "failed": True}
    return {"path": "", "cancelled": True}


def _public(result):
    if not isinstance(result, dict):
        return {"path": "", "cancelled": True}
    result.pop("failed", None)
    return result


def _darwin(initial, timeout, run=None):
    start = _existing_start(initial)
    argv = ["osascript", "-e", _OSA]
    if start:
        argv.append(start)
    result = _from_argv(argv, timeout, run)
    # A stale default location makes osascript fail before the user can pick.
    # Cancel is exit 0 and an empty path (the script catches error -128), so it is not retried.
    if start and result.get("failed"):
        result = _from_argv(["osascript", "-e", _OSA], timeout, run)
    return _public(result)


def _linux(initial, timeout, which=None, run=None):
    which = shutil.which if which is None else which
    start = _existing_start(initial) or os.path.expanduser("~")
    if which("zenity"):
        argv = ["zenity", "--file-selection", "--directory",
                "--title=Choose vault folder", "--filename=" + start + os.sep]
        return _public(_from_argv(argv, timeout, run))
    if which("kdialog"):
        argv = ["kdialog", "--getexistingdirectory", start, "--title", "Choose vault folder"]
        return _public(_from_argv(argv, timeout, run))
    tk = _tk(initial, timeout, run)
    if tk is not None:
        return _public(tk)
    # browse: the page can show its own folder list instead (see browse()).
    return {"path": "", "cancelled": False, "browse": True,
            "error": "Couldn't open a folder picker. Type the folder path and click Save."}


def _windows(initial, timeout, run=None):
    tk = _tk(initial, timeout, run)
    if tk is not None:
        return _public(tk)
    return _powershell(initial, timeout, run)


def _child_env(initial):
    env = dict(os.environ)
    start = _existing_start(initial)
    if start:
        env["CHATBOX_PICK_INITIAL"] = start
    else:
        env.pop("CHATBOX_PICK_INITIAL", None)
    return env


def _tk(initial, timeout, run=None):
    """None when tkinter cannot draw, so the caller can try another dialog."""
    run = run or subprocess.run
    try:
        proc = run([sys.executable, "-c", _TK], capture_output=True, text=True,
                   timeout=timeout, env=_child_env(initial))
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": False, "error": "The folder picker timed out."}
    except OSError as exc:
        return {"path": "", "cancelled": False, "error": str(exc)}
    if getattr(proc, "returncode", 1) == 2:
        return None
    err = (getattr(proc, "stderr", "") or "").lower()
    if getattr(proc, "returncode", 1) != 0 and ("display" in err or "tkinter" in err):
        return None
    path = _clean(getattr(proc, "stdout", "") or "")
    if getattr(proc, "returncode", 1) != 0 or not path:
        return {"path": "", "cancelled": True}
    return {"path": path, "cancelled": False}


def _powershell(initial, timeout, run=None):
    argv = ["powershell", "-NoProfile", "-STA", "-Command", _PS]
    result = _from_argv(argv, timeout, run, env=_child_env(initial))
    if result.get("error"):
        return {"path": "", "cancelled": False,
                "error": "Couldn't open a folder picker. Type the folder path and click Save."}
    return _public(result)


BROWSE_LIMIT = 500


def browse(path=""):
    """List the folders inside path, for the page's own folder list.

    Used when no native dialog can be opened (a browser tab on a system without
    zenity, kdialog or tkinter). Hidden folders are left out. A folder that holds
    an .obsidian folder is marked as a vault.
    """
    raw = os.path.expanduser(str(path or "").strip()) or os.path.expanduser("~")
    here = os.path.abspath(raw)
    while here and not os.path.isdir(here):
        up = os.path.dirname(here)
        if up == here:
            break
        here = up
    if not os.path.isdir(here):
        here = os.path.expanduser("~")
    folders, error = [], ""
    try:
        with os.scandir(here) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                folders.append({"name": entry.name,
                                "vault": os.path.isdir(os.path.join(entry.path, ".obsidian"))})
    except OSError:
        error = "Chatbox can't open this folder."
    folders.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(here)
    result = {"path": here, "parent": parent if parent != here else "",
              "home": os.path.expanduser("~"), "sep": os.sep,
              "vault": os.path.isdir(os.path.join(here, ".obsidian")),
              "folders": folders[:BROWSE_LIMIT], "more": max(0, len(folders) - BROWSE_LIMIT)}
    if error:
        result["error"] = error
    return result

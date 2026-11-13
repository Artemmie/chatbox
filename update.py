"""Look for a newer Chatbox and apply it from the user's own clone.

A git install is compared with the tracked branch on origin (`git fetch`, then
fast-forward only). Anything else is compared with the public GitHub tags of
Artemmie/chatbox. Chatbox never downloads and runs a release by itself, never
force-pushes or merges, and leaves chats, accounts and settings where they are.
"""
import json
import os
import platform
import sys
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import runtime

REPO = "Artemmie/chatbox"
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases"
CHECK_EVERY = 6 * 60 * 60
_BOX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_lock = threading.Lock()
_run_lock = threading.Lock()
_started = False
_no_hooks = None
_ctx = {}
STATE = {"enabled": True, "available": False, "can_apply": False, "checking": False,
         "current": "", "latest": "", "message": "", "url": "", "kind": "", "download": False,
         "checked_at": 0}


def asset_name(system=None, machine=None):
    """The release file for this operating system. Opening it downloads the update."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Windows":
        return "Chatbox-Windows-x64-setup.exe"
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "Chatbox-macOS-arm64.dmg"
        return "Chatbox-macOS-arm64.dmg"
    return "Chatbox-Linux-x86_64.AppImage"


def release_asset_url(tag, filename):
    tag = tag if str(tag).startswith("v") else "v" + str(tag)
    return f"https://github.com/{REPO}/releases/download/{tag}/{filename}"


def version_parts(text):
    text = str(text or "").strip().lstrip("vV")
    if not text:
        return None
    nums = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "" and not nums:
            return None
        nums.append(int(digits or 0))
    return tuple(nums) if nums else None


def version_is_newer(remote, local):
    left, right = version_parts(remote), version_parts(local)
    if left is None or right is None:
        return bool(remote) and str(remote).lstrip("vV") != str(local).lstrip("vV")
    return left > right


def bundled_version():
    """The version baked into a PyInstaller build. A source checkout does not use version.txt."""
    if not getattr(sys, "frozen", False):
        return ""
    path = os.path.join(runtime.source_root(), "version.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


class UpdateError(Exception):
    pass


def _hooks_dir():
    """An empty directory so git does not run hooks from the clone during an update."""
    global _no_hooks
    if not _no_hooks or not os.path.isdir(_no_hooks):
        _no_hooks = tempfile.mkdtemp(prefix="chatbox-hooks-")
    return _no_hooks


def git(source, *args, check=True):
    cmd = ["git", "-C", source, "-c", "core.hooksPath=" + _hooks_dir(), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git failed").strip()
        raise UpdateError(detail.splitlines()[-1][:300] if detail else "git failed")
    return proc


def is_git_repo(path):
    if not path or not os.path.isdir(path):
        return False
    try:
        proc = git(path, "rev-parse", "--is-inside-work-tree", check=False)
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def read_install(data_dir):
    path = os.path.join(data_dir or "", "install.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    info = {}
    source = data.get("source")
    if isinstance(source, str) and source:
        info["source"] = source
    mode = data.get("mode")
    info["mode"] = "browser" if mode == "browser" else "window"
    box = data.get("distrobox") if isinstance(data.get("distrobox"), str) else ""
    info["distrobox"] = box if _BOX_NAME.match(box or "") else ""
    commit = data.get("commit") if isinstance(data.get("commit"), str) else ""
    info["commit"] = commit if re.fullmatch(r"[0-9a-fA-F]{7,64}", commit or "") else ""
    return info


def write_install(data_dir, source, mode, distrobox=""):
    """Record where this install was copied from. Called by install.sh."""
    info = {"source": os.path.realpath(source),
            "mode": "browser" if mode == "browser" else "window",
            "distrobox": distrobox if _BOX_NAME.match(distrobox or "") else "",
            "commit": ""}
    if is_git_repo(source):
        proc = git(source, "rev-parse", "HEAD", check=False)
        if proc.returncode == 0:
            info["commit"] = proc.stdout.strip()
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "install.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return info


def source_of(data_dir, here):
    """The git clone to update, or None when this install has no clone."""
    info = read_install(data_dir)
    recorded = info.get("source") or ""
    if recorded and is_git_repo(recorded):
        return os.path.realpath(recorded), info
    if here and is_git_repo(here):
        return os.path.realpath(here), info
    return None, info


def _branch(source):
    proc = git(source, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if proc.returncode != 0:
        return ""
    name = proc.stdout.strip()
    return "" if name == "HEAD" else name


def upstream_of(source):
    """origin/branch for the tracked branch, or origin/<current> when no upstream is set."""
    proc = git(source, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if proc.returncode == 0:
        name = proc.stdout.strip()
        if "/" in name:
            return name
    branch = _branch(source)
    if not branch:
        return ""
    proc = git(source, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False)
    if proc.returncode == 0:
        return f"origin/{branch}"
    return ""


def dirty(source):
    proc = git(source, "status", "--porcelain", check=False)
    return bool((proc.stdout or "").strip())


def _counts(source, upstream):
    proc = git(source, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    left, right = proc.stdout.split()
    return int(left), int(right)


def fetch_upstream(source, upstream):
    remote = upstream.split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote or ""):
        raise UpdateError("The tracked remote name is not usable.")
    # Fetch that one remote. No --force, so a rejected non-fast-forward ref is left as it is.
    git(source, "fetch", "--prune", remote)


def check_git(source, fetch=True):
    head = git(source, "rev-parse", "HEAD").stdout.strip()
    upstream = upstream_of(source)
    if not upstream:
        return {"kind": "git", "available": False, "can_apply": False, "current": head[:12],
                "latest": head[:12], "message": "This clone has no tracked branch on origin.", "url": ""}
    if fetch:
        fetch_upstream(source, upstream)
    left, right = _counts(source, upstream)
    latest = git(source, "rev-parse", upstream).stdout.strip()
    result = {"kind": "git", "current": head[:12], "latest": latest[:12], "url": "",
              "available": False, "can_apply": False, "message": ""}
    if left and right:
        result["available"] = True
        result["message"] = ("The update is not a straight fast-forward, so it was not applied. "
                             "The clone and origin have both changed.")
        return result
    if right and dirty(source):
        result["available"] = True
        result["message"] = ("This copy has local changes, so the update was not applied. "
                             "Commit or stash them in the Chatbox folder, then try again.")
        return result
    if right:
        result["available"] = True
        result["can_apply"] = True
    return result


def _fetch_json(url, opener):
    if opener is not None:
        raw = opener(url)
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "chatbox"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def check_github(local_commit, opener=None):
    """Compare a recorded commit with the newest public tag. Never downloads the tag."""
    result = {"kind": "github", "available": False, "can_apply": False, "current": (local_commit or "")[:12],
              "latest": "", "message": "", "url": RELEASE_PAGE}
    try:
        tags = _fetch_json(TAGS_URL, opener)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError):
        tags = None
    sha = ""
    name = ""
    if isinstance(tags, list) and tags:
        first = tags[0] if isinstance(tags[0], dict) else {}
        commit = first.get("commit") if isinstance(first.get("commit"), dict) else {}
        sha = commit.get("sha") if isinstance(commit.get("sha"), str) else ""
        name = first.get("name") if isinstance(first.get("name"), str) else ""
    if not sha:
        try:
            release = _fetch_json(RELEASES_URL, opener)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError):
            result["message"] = "Could not check GitHub for a new version."
            return result
        if isinstance(release, dict):
            name = release.get("tag_name") if isinstance(release.get("tag_name"), str) else name
            page = release.get("html_url")
            if isinstance(page, str) and page.startswith("https://github.com/"):
                result["url"] = page
    if name:
        result["latest"] = name
    if not local_commit or not sha:
        if not local_commit:
            result["message"] = "This install has no recorded revision, so Chatbox will not guess that a tag is newer."
        elif not sha:
            result["message"] = "Could not check GitHub for a new version."
        return result
    if sha != local_commit:
        result["available"] = True
        result["latest"] = (name or sha[:12])
        result["message"] = "A newer tag is on GitHub. Chatbox will not download and run it on its own."
        result["url"] = RELEASE_PAGE + (f"/tag/{name}" if name else "")
    return result


def check_release(local_version, opener=None, system=None, machine=None):
    """A packaged build compares its version with the latest GitHub release and links the matching file."""
    result = {"kind": "github", "available": False, "can_apply": False, "download": False,
              "current": local_version or "", "latest": "", "message": "", "url": RELEASE_PAGE}
    try:
        release = _fetch_json(RELEASES_URL, opener)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError):
        result["message"] = "Could not check GitHub for a new version."
        return result
    if not isinstance(release, dict):
        result["message"] = "Could not check GitHub for a new version."
        return result
    tag = release.get("tag_name") if isinstance(release.get("tag_name"), str) else ""
    page = release.get("html_url")
    if isinstance(page, str) and page.startswith("https://github.com/"):
        result["url"] = page
    if not tag or not version_is_newer(tag, local_version):
        result["latest"] = tag
        return result
    filename = asset_name(system, machine)
    result["available"] = True
    result["download"] = True
    result["can_apply"] = False
    result["latest"] = tag
    result["url"] = release_asset_url(tag, filename)
    result["message"] = ("A newer Chatbox is published. Download it and replace this copy yourself. "
                         "Chats and sign-ins stay on this computer.")
    return result


def check(data_dir, here, fetch=True, opener=None):
    source, info = source_of(data_dir, here)
    if source:
        try:
            found = check_git(source, fetch=fetch)
        except UpdateError as exc:
            return {"kind": "git", "available": False, "can_apply": False, "current": "", "latest": "",
                    "message": f"Could not check the clone for an update. {exc}", "url": ""}
        return found
    version = bundled_version()
    if getattr(sys, "frozen", False) or version:
        return check_release(version, opener=opener)
    return check_github(info.get("commit") or "", opener=opener)


def _public(result):
    """Status for the browser. The clone path stays on this computer."""
    keys = ("enabled", "available", "can_apply", "checking", "current", "latest", "message", "url",
            "kind", "download")
    out = {key: result.get(key, STATE.get(key)) for key in keys}
    out["checked_at"] = result.get("checked_at", 0)
    return out


def _install_script(source):
    root = os.path.realpath(source)
    script = os.path.realpath(os.path.join(root, "install.sh"))
    if os.path.normcase(script) != os.path.normcase(os.path.join(root, "install.sh")) or not os.path.isfile(script):
        raise UpdateError("install.sh is not in the Chatbox folder.")
    return script


def run_install(source, info, data_dir):
    """Re-run the user's own install.sh. Does not delete the data directory."""
    script = _install_script(source)
    mode = (info or {}).get("mode") or "browser"
    box = (info or {}).get("distrobox") or ""
    if box and not _BOX_NAME.match(box):
        raise UpdateError("The distrobox name recorded at install is not usable.")
    cmd = [runtime.bash_exe(), script]
    if mode == "browser":
        cmd.append("--browser")
    if box:
        cmd.extend(["--distrobox", box])
    env = os.environ.copy()
    env["CHATBOX_DATA"] = str(data_dir).replace("\\", "/")
    for key in list(env):
        if "TOKEN" in key.upper() or "SECRET" in key.upper() or key.upper().endswith("_KEY"):
            env.pop(key, None)
    proc = subprocess.run(cmd, cwd=os.path.realpath(source), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "install failed").strip().splitlines()
        raise UpdateError(detail[-1][:300] if detail else "install failed")


def restart_command(data_dir, here, info, port=None):
    """How to start the same mode again after the files have been copied."""
    info = info or {}
    mode = info.get("mode") or ""
    here_real = os.path.realpath(here) if here else ""
    source = os.path.realpath(info.get("source") or here_real or data_dir)
    installed = os.path.realpath(data_dir)
    running_clone = here_real and here_real == source and here_real != installed
    root = here_real if running_clone else (installed if os.path.isfile(os.path.join(installed, "chatbox.py")) else here_real)
    window = mode == "window"
    if window and os.path.isfile(os.path.join(root, "app.py")):
        venv_py = os.path.join(installed, "venv", "bin", "python")
        py = venv_py if os.path.isfile(venv_py) else shutil.which("python3") or "python3"
        return [py, os.path.join(root, "app.py")]
    py = shutil.which("python3") or "python3"
    cmd = [py, os.path.join(root, "chatbox.py")]
    if port:
        cmd.extend(["--port", str(int(port))])
    cmd.append("--no-browser")
    return cmd


def apply(data_dir, here, port=None, restarter=None, fetch=True):
    """Fast-forward the clone, re-run install.sh, then restart. Aborts on local edits."""
    source, info = source_of(data_dir, here)
    if not source:
        found = check_github(info.get("commit") or "")
        return {"ok": False, "restarting": False,
                "error": found.get("message") or "This install is not a git clone, so Chatbox will not download an update.",
                "url": found.get("url") or RELEASE_PAGE}
    if not _branch(source):
        return {"ok": False, "restarting": False,
                "error": "This clone is not on a branch, so the update was not applied."}
    if dirty(source):
        return {"ok": False, "restarting": False,
                "error": "This copy has local changes, so the update was not applied. "
                         "Commit or stash them in the Chatbox folder, then try again."}
    try:
        found = check_git(source, fetch=fetch)
    except UpdateError as exc:
        return {"ok": False, "restarting": False, "error": str(exc)}
    if found.get("message") and not found.get("can_apply"):
        return {"ok": False, "restarting": False, "error": found["message"]}
    upstream = upstream_of(source)
    if found.get("can_apply"):
        # Fast-forward only. A merge commit or a reset is never used.
        try:
            git(source, "merge", "--ff-only", upstream)
        except UpdateError as exc:
            return {"ok": False, "restarting": False, "error": str(exc)}
    try:
        run_install(source, info, data_dir)
    except UpdateError as exc:
        return {"ok": False, "restarting": False, "error": str(exc)}
    cmd = restart_command(data_dir, here, {**info, "source": source}, port=port)
    if restarter is not None:
        restarter(cmd)
        return {"ok": True, "restarting": True}
    _schedule(cmd)
    return {"ok": True, "restarting": True}


def _schedule(cmd):
    def go():
        time.sleep(0.4)
        os.execv(cmd[0], cmd)
    threading.Thread(target=go, daemon=False).start()


def snapshot():
    with _lock:
        data = _public(STATE)
        due = not STATE.get("checking") and (time.time() - float(STATE.get("checked_at") or 0) >= CHECK_EVERY)
        idle = STATE.get("checked_at", 0) == 0
    if _ctx and (due or idle) and not data.get("checking"):
        threading.Thread(target=lambda: consider(auto=False), daemon=True).start()
        data["checking"] = True
    return data


def consider(auto=False):
    """One check. When auto-install is on and the clone can fast-forward, apply it."""
    if not _ctx:
        return snapshot()
    if not _run_lock.acquire(blocking=False):
        with _lock:
            return _public(STATE)
    try:
        return _consider(auto)
    finally:
        _run_lock.release()


def _consider(auto):
    try:
        settings = _ctx["settings"]() or {}
    except Exception:
        settings = {"auto_update": True}
    with _lock:
        if not settings.get("auto_update", True):
            STATE.update(enabled=False, available=False, can_apply=False, checking=False, message="")
            return _public(STATE)
        STATE["checking"] = True
        STATE["enabled"] = True
    try:
        found = check(_ctx["data_dir"], _ctx["here"], fetch=True)
    except Exception:
        found = {"available": False, "can_apply": False, "message": "Could not check for an update.", "kind": ""}
    with _lock:
        for key in ("available", "can_apply", "current", "latest", "message", "url", "kind"):
            if key in found:
                STATE[key] = found[key]
        STATE["checking"] = False
        STATE["checked_at"] = time.time()
        STATE["enabled"] = True
        view = _public(STATE)
    if auto and settings.get("auto_install_updates") and found.get("can_apply") and not _ctx["busy"]():
        port = _ctx["port"]()
        apply(_ctx["data_dir"], _ctx["here"], port=port)
    return view


def request_apply():
    if not _ctx:
        return {"ok": False, "error": "Updates are not running."}
    if _ctx["busy"]():
        return {"ok": False, "error": "A reply is still running. Stop it, then update."}
    settings = _ctx["settings"]() or {}
    if not settings.get("auto_update", True):
        return {"ok": False, "error": "Update checks are turned off in Settings."}
    return apply(_ctx["data_dir"], _ctx["here"], port=_ctx["port"]())


def start(data_dir, here, settings, busy, port):
    """Check once now, then about every six hours. Safe to call once from startup."""
    global _started
    with _lock:
        _ctx["data_dir"] = data_dir
        _ctx["here"] = here
        _ctx["settings"] = settings
        _ctx["busy"] = busy
        _ctx["port"] = port
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                consider(auto=True)
            except Exception:
                pass
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, name="chatbox-update", daemon=True).start()

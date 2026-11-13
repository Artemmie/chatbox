#!/usr/bin/env python3
"""Build the downloadable Chatbox app. Used by the release workflow.

    python packaging/build.py pyinstaller
    python packaging/build.py package
"""
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUT = DIST / "artifacts"


def version():
    return os.environ.get("CHATBOX_VERSION", "0.0.0").strip() or "0.0.0"


def write_version():
    text = version() + "\n"
    (ROOT / "version.txt").write_text(text, encoding="utf-8")


def pyinstaller():
    write_version()
    os.chdir(ROOT)
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed", "--name", "Chatbox",
        "--add-data", f"ui{sep}ui",
        "--add-data", f"chatbox.svg{sep}.",
        "--add-data", f"version.txt{sep}.",
        "--collect-all", "PyQt6",
        "--specpath", str(DIST),
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--osx-bundle-identifier", "local.chatbox",
        str(ROOT / "launcher.py"),
    ]
    if sys.platform != "darwin":
        cmd = [part for part in cmd if part != "--osx-bundle-identifier" and part != "local.chatbox"]
    subprocess.check_call(cmd)


def _zip_dir(folder, dest):
    folder = Path(folder)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in folder.rglob("*"):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))


def _tar_dir(folder, dest):
    folder = Path(folder)
    with tarfile.open(dest, "w:gz") as archive:
        archive.add(folder, arcname=folder.name)


def package_windows():
    OUT.mkdir(parents=True, exist_ok=True)
    app = DIST / "Chatbox"
    if not app.is_dir():
        raise SystemExit(f"Missing {app}. Run pyinstaller first.")
    _zip_dir(app, OUT / "Chatbox-Windows-x64.zip")
    iscc = shutil.which("ISCC") or shutil.which("iscc")
    if not iscc:
        candidate = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")
        if candidate.is_file():
            iscc = str(candidate)
    if not iscc:
        print("Inno Setup not found. The zip is built; the installer was skipped.")
        return
    subprocess.check_call([iscc, f"/DAppVersion={version().lstrip('v')}", str(ROOT / "packaging" / "Chatbox.iss")])


def package_linux():
    OUT.mkdir(parents=True, exist_ok=True)
    app = DIST / "Chatbox"
    if not app.is_dir():
        raise SystemExit(f"Missing {app}. Run pyinstaller first.")
    _tar_dir(app, OUT / "Chatbox-Linux-x86_64.tar.gz")
    appdir = DIST / "Chatbox.AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    bindir = appdir / "usr" / "bin"
    bindir.mkdir(parents=True)
    shutil.copytree(app, bindir, dirs_exist_ok=True)
    # PyInstaller puts the binary at dist/Chatbox/Chatbox. The AppDir expects usr/bin/Chatbox.
    binary = bindir / "Chatbox"
    if not binary.is_file():
        nested = bindir / "Chatbox" / "Chatbox"
        # copytree of the folder into usr/bin created usr/bin/Chatbox/... 
        # AppRun points at usr/bin/Chatbox as a file. Point it at the nested binary instead.
        pass
    shutil.copy(ROOT / "packaging" / "AppRun", appdir / "AppRun")
    os.chmod(appdir / "AppRun", 0o755)
    # If copytree nested the folder, AppRun must exec the real binary.
    apprun = appdir / "AppRun"
    if (appdir / "usr" / "bin" / "Chatbox" / "Chatbox").is_file():
        apprun.write_text("#!/bin/sh\nHERE=\"$(dirname \"$(readlink -f \"$0\")\")\"\n"
                          "exec \"$HERE/usr/bin/Chatbox/Chatbox\" \"$@\"\n", encoding="utf-8")
        os.chmod(apprun, 0o755)
    desktop = (ROOT / "packaging" / "chatbox.desktop").read_text(encoding="utf-8")
    (appdir / "chatbox.desktop").write_text(desktop, encoding="utf-8")
    shutil.copy(ROOT / "chatbox.svg", appdir / "chatbox.svg")
    tool = os.environ.get("APPIMAGETOOL", "appimagetool")
    dest = OUT / "Chatbox-Linux-x86_64.AppImage"
    env = os.environ.copy()
    env["ARCH"] = "x86_64"
    subprocess.check_call([tool, str(appdir), str(dest)], env=env)
    os.chmod(dest, 0o755)


def package_macos():
    OUT.mkdir(parents=True, exist_ok=True)
    app = DIST / "Chatbox.app"
    if not app.is_dir():
        raise SystemExit(f"Missing {app}. Run pyinstaller first.")
    zip_path = OUT / "Chatbox-macOS-arm64.zip"
    dmg_path = OUT / "Chatbox-macOS-arm64.dmg"
    if zip_path.exists():
        zip_path.unlink()
    subprocess.check_call(["ditto", "-c", "-k", "--keepParent", str(app), str(zip_path)])
    if dmg_path.exists():
        dmg_path.unlink()
    subprocess.check_call(["hdiutil", "create", "-volname", "Chatbox", "-srcfolder", str(app),
                           "-ov", "-format", "UDZO", str(dmg_path)])


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("pyinstaller", "package"):
        raise SystemExit("usage: packaging/build.py pyinstaller|package")
    if sys.argv[1] == "pyinstaller":
        pyinstaller()
        return
    if sys.platform == "darwin":
        package_macos()
    elif os.name == "nt":
        package_windows()
    else:
        package_linux()


if __name__ == "__main__":
    main()

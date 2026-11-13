#!/usr/bin/env python3
"""Chatbox desktop app: runs the Chatbox server and shows it in its own window.

Closing the window stops everything. Chat settings (folder, model, last backend)
are kept in ./profile between launches.
"""
import os
import sys
import threading
from http.server import ThreadingHTTPServer

import runtime
HERE = runtime.source_root()
sys.path.insert(0, HERE)
import chatbox  # noqa: E402
import folderpick  # noqa: E402

from PyQt6.QtCore import QObject, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

PORT = 8766  # fixed so saved settings survive restarts; any free port if taken


class Window(QWebEngineView):
    """The Chatbox window. Asks before closing while an install / sign-in is still running,
    because closing stops it (that's how an unfinished sign-in gets lost)."""

    def closeEvent(self, event):
        busy = [chatbox.accounts.SERVICES[s]["name"] for s, j in chatbox.ACCOUNTS.jobs.items()
                if j.state == "running"]
        if busy:
            what = {"login": "sign-in", "install": "install", "logout": "sign-out"}
            kinds = {what.get(chatbox.ACCOUNTS.jobs[s].kind, "task") for s in chatbox.ACCOUNTS.jobs
                     if chatbox.ACCOUNTS.jobs[s].state == "running"}
            answer = QMessageBox.question(
                self, "Close Chatbox?",
                f"{' and '.join(busy)} {'/'.join(sorted(kinds))} is still in progress.\n\n"
                "Closing Chatbox stops it. Close anyway?",
                QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Close:
                event.ignore()
                return
        event.accept()


class FolderBridge(QObject):
    """Show QFileDialog on the GUI thread when the page asks to pick a folder.

    The HTTP handler runs on a worker thread, so it emits `requested` and waits.
    Cancel leaves the path empty. A dialog that never closes hits the timeout.
    """

    requested = pyqtSignal(int, str)

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._event = threading.Event()
        self._result = {"path": "", "cancelled": True}
        self._lock = threading.Lock()
        self._token = 0
        self.requested.connect(self._show)

    def _show(self, token, initial):
        start = initial if initial and os.path.isdir(os.path.expanduser(initial)) else ""
        chosen = QFileDialog.getExistingDirectory(self._window, "Choose vault folder", start)
        # A timed-out request has a newer token. Ignore this dialog so it cannot
        # answer the next one.
        if token != self._token:
            return
        if chosen:
            self._result = {"path": chosen, "cancelled": False}
        else:
            self._result = {"path": "", "cancelled": True}
        self._event.set()

    def pick(self, initial="", timeout=120):
        with self._lock:
            self._token += 1
            token = self._token
            self._event.clear()
            self.requested.emit(token, initial or "")
            if not self._event.wait(timeout or 120):
                return {"path": "", "cancelled": False, "error": "The folder picker timed out."}
            return dict(self._result)


def start_server():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), chatbox.Handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), chatbox.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    srv = start_server()
    chatbox.CURRENT_PORT = srv.server_address[1]
    chatbox.start_updates()
    app = QApplication(sys.argv)
    app.setApplicationName("Chatbox")
    app.setDesktopFileName("chatbox")  # links the window to its menu entry and icon
    icon_path = os.path.join(HERE, "chatbox.svg")
    app.setWindowIcon(QIcon(icon_path))

    profile = QWebEngineProfile("chatbox", app)
    profile.setPersistentStoragePath(os.path.join(chatbox.DATA_DIR, "profile"))
    profile.setCachePath(os.path.join(chatbox.DATA_DIR, "profile", "cache"))

    view = Window()
    view.setPage(QWebEnginePage(profile, view))
    view.setWindowTitle(chatbox.updatemod.window_title())
    view.resize(1000, 760)
    view.load(QUrl(f"http://127.0.0.1:{srv.server_address[1]}/"))
    view.show()
    folderpick.set_bridge(FolderBridge(view).pick)

    code = app.exec()
    srv.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()

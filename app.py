#!/usr/bin/env python3
"""Chatbox desktop app: runs the Chatbox server and shows it in its own window.

Closing the window stops everything. Chat settings (folder, model, last backend)
are kept in ./profile between launches.
"""
import os
import sys
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatbox  # noqa: E402

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

PORT = 8766  # fixed so saved settings survive restarts; any free port if taken


def start_server():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), chatbox.Handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), chatbox.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    srv = start_server()
    app = QApplication(sys.argv)
    app.setApplicationName("Chatbox")
    app.setDesktopFileName("chatbox")  # links the window to its menu entry and icon
    icon_path = os.path.join(HERE, "chatbox.svg")
    app.setWindowIcon(QIcon(icon_path))

    profile = QWebEngineProfile("chatbox", app)
    profile.setPersistentStoragePath(os.path.join(chatbox.DATA_DIR, "profile"))
    profile.setCachePath(os.path.join(chatbox.DATA_DIR, "profile", "cache"))

    view = QWebEngineView()
    view.setPage(QWebEnginePage(profile, view))
    view.setWindowTitle("Chatbox")
    view.resize(1000, 760)
    view.load(QUrl(f"http://127.0.0.1:{srv.server_address[1]}/"))
    view.show()

    code = app.exec()
    srv.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()

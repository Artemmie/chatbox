"""HTTP routing, streaming, cancel, timeouts, and attachments. CLIs are fakes."""
import json
import os
import stat
import sys
import textwrap
import time
import unittest
from pathlib import Path

import streaming

from tests.support import isolate, request, serve


def write_cli(folder, name, body):
    folder = Path(folder)
    script = textwrap.dedent(body)
    if os.name == "nt":
        # A .py file is started by python.exe, so a multiline prompt is not cut by cmd.exe.
        py = folder / (name + ".py")
        py.write_text(script, encoding="utf-8")
        return str(py)
    path = folder / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        self.cli = write_cli(self.tmp, "claude", """\
            #!/usr/bin/env python3
            import json, os, sys, time
            if "--include-partial-messages" in sys.argv:
                sys.stderr.write("error: unknown option '--include-partial-messages'\\n")
                sys.exit(2)
            mode = os.environ.get("FAKE_MODE", "")
            if mode == "silent":
                time.sleep(30)
                sys.exit(0)
            if mode == "auth":
                sys.stderr.write("Error: not logged in. Run claude auth login\\n")
                sys.exit(1)
            if mode == "hang":
                open(os.environ["FAKE_PID"], "w").write(str(os.getpid()))
                print(json.dumps({"type": "stream_event", "session_id": "s",
                    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Partial"}}}), flush=True)
                time.sleep(30)
                sys.exit(0)
            msg = " ".join(sys.argv)
            text = "saw-file" if ".chatbox-attach" in msg else "Hello from Claude"
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess1"}), flush=True)
            print(json.dumps({"type": "stream_event", "session_id": "sess1",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}), flush=True)
            print(json.dumps({"type": "result", "is_error": False, "result": text,
                "session_id": "sess1", "usage": {"input_tokens": 2, "output_tokens": 3}}), flush=True)
            """)
        self.box.find = lambda cmd: self.cli if cmd == "claude" else None
        self.srv = serve(self.box)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("FAKE_MODE", None)

    def events(self, body):
        code, raw = request(self.srv, "/api/chat", body)
        self.assertEqual(code, 200)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_pages_and_forbidden_host(self):
        code, raw = request(self.srv, "/")
        self.assertEqual(code, 200)
        self.assertIn(b"/ui/js/app.js", raw)
        self.assertIn(b'id="sidever"', raw)
        self.assertIn(b'id="aboutver"', raw)
        self.assertIn(b'id="authrow"', raw)
        self.assertIn(b">About<", raw)
        code, raw = request(self.srv, "/ui/style.css")
        self.assertEqual(code, 200)
        self.assertIn(b"--bg", raw)
        self.assertIn(b".sdot.ready", raw)
        self.assertIn(b".sdot.signed_out", raw)
        self.assertIn(b".sdot.missing", raw)
        code, raw = request(self.srv, "/ui/js/app.js")
        self.assertEqual(code, 200)
        self.assertIn(b"/api/auth-status", raw)
        self.assertIn(b"data-signin", raw)
        self.assertIsNone(self.box.ui_file("/ui/../../chatbox.py"))
        code, _ = request(self.srv, "/api/status", host="evil.example")
        self.assertEqual(code, 403)

    def test_sidebar_projects_replace_the_header_dropdown(self):
        code, raw = request(self.srv, "/")
        self.assertEqual(code, 200)
        self.assertIn(b'id="chatsearch"', raw)
        self.assertIn(b'id="newproject"', raw)
        self.assertIn(b">New project<", raw)
        self.assertIn(b'id="p-project"', raw)  # the project is chosen in the message box
        self.assertNotIn(b'id="projectcrumb"', raw)
        self.assertNotIn(b'id="projectpick"', raw)
        self.assertNotIn(b"<select", raw)
        code, js = request(self.srv, "/ui/js/app.js")
        self.assertEqual(code, 200)
        self.assertIn(b"Move to project", js)
        self.assertIn(b"proj-collapsed", js)
        self.assertIn(b"dragstart", js)
        self.assertIn(b"No project", js)
        self.assertIn(b"Notes in the vault are not deleted.", js)
        code, css = request(self.srv, "/ui/style.css")
        self.assertEqual(code, 200)
        self.assertIn(b"#p-project", css)
        self.assertIn(b".proj-row", css)

    def test_version_is_on_status_and_the_version_endpoint(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "VERSION"), encoding="utf-8") as handle:
            expected = "v" + handle.read().strip().lstrip("vV")
        code, raw = request(self.srv, "/api/version")
        self.assertEqual(code, 200)
        info = json.loads(raw)
        self.assertEqual(info["version"], expected)
        self.assertTrue(info["url"].endswith("/releases/tag/" + expected))
        code, raw = request(self.srv, "/api/status")
        self.assertEqual(code, 200)
        body = json.loads(raw)
        self.assertEqual(body["version"], info["version"])
        self.assertEqual(body["version_url"], info["url"])

    def test_streams_and_falls_back_when_a_flag_is_unknown(self):
        evs = self.events({"backend": "claude", "message": "hi", "mode": "ask", "workdir": self.tmp})
        self.assertEqual("".join(e.get("text", "") for e in evs if e["type"] == "delta"), "Hello from Claude")
        done = evs[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["session_id"], "sess1")
        self.assertEqual(done["usage"]["output"], 3)

    def test_missing_cli_and_signed_out_and_auth_stderr(self):
        evs = self.events({"backend": "cursor", "message": "hi", "mode": "ask", "workdir": self.tmp})
        self.assertIn("Cursor is not installed", evs[-1]["error"])
        self.box.ACCOUNTS._cache = {"claude": {"installed": True, "logged_in": False}}
        self.box.ACCOUNTS._cache_time = time.time()
        evs = self.events({"backend": "claude", "message": "hi", "mode": "ask", "workdir": self.tmp})
        self.assertIn("not signed in", evs[-1]["error"].lower())
        self.box.ACCOUNTS._cache_time = 0
        os.environ["FAKE_MODE"] = "auth"
        evs = self.events({"backend": "claude", "message": "hi", "mode": "ask", "workdir": self.tmp})
        self.assertTrue(evs[-1]["error"].startswith("Claude is not signed in"))
        os.environ["FAKE_MODE"] = ""

    def test_cancel_kills_the_child(self):
        pidfile = os.path.join(self.tmp, "pid")
        os.environ["FAKE_MODE"] = "hang"
        os.environ["FAKE_PID"] = pidfile
        import threading
        box = {}

        def reader():
            box["evs"] = self.events({"backend": "claude", "message": "hi", "mode": "ask",
                                     "workdir": self.tmp, "turn_id": "ab" * 8})

        thread = threading.Thread(target=reader)
        thread.start()
        for _ in range(50):
            if os.path.exists(pidfile) and open(pidfile).read().strip():
                break
            time.sleep(0.05)
        else:
            self.fail("fake CLI did not start")
        pid = int(open(pidfile).read())
        self.assertTrue(process_alive(pid))
        code, raw = request(self.srv, "/api/cancel", {"turn_id": "ab" * 8})
        self.assertEqual(code, 200)
        self.assertIn(b"true", raw.lower())
        thread.join(10)
        self.assertTrue(any(e.get("stopped") for e in box["evs"]))
        self.assertIn("Partial", "".join(e.get("text", "") for e in box["evs"]))
        time.sleep(0.2)
        self.assertFalse(process_alive(pid))
        os.environ["FAKE_MODE"] = ""

    def test_start_timeout(self):
        os.environ["FAKE_MODE"] = "silent"
        old = streaming.START_TIMEOUT
        streaming.START_TIMEOUT = 1
        try:
            evs = self.events({"backend": "claude", "message": "hi", "mode": "ask", "workdir": self.tmp})
        finally:
            streaming.START_TIMEOUT = old
            os.environ["FAKE_MODE"] = ""
        self.assertIn("no output", evs[-1]["error"].lower())

    def test_attachment_is_given_to_the_cli(self):
        import base64
        code, raw = request(self.srv, "/api/attach", {
            "name": "notes.md", "type": "text/markdown",
            "data": base64.b64encode(b"hello file").decode()})
        meta = json.loads(raw)
        self.assertEqual(meta["kind"], "text")
        evs = self.events({"backend": "claude", "message": "look", "mode": "ask",
                          "workdir": self.tmp, "attachments": [meta["id"]]})
        self.assertEqual(evs[-1]["reply"], "saw-file")

    def test_bad_folder(self):
        evs = self.events({"backend": "claude", "message": "hi", "mode": "ask",
                          "workdir": os.path.join(self.tmp, "nope")})
        self.assertIn("Folder not found", evs[-1]["error"])


if __name__ == "__main__":
    unittest.main()


class OneWindowTests(unittest.TestCase):
    """A second launch hands over to the first, and an open page notices new files."""

    def setUp(self):
        self.box, self.tmp = isolate()
        self.srv = serve(self.box)
        self.port = self.srv.server_address[1]
        self._hook = self.box.FOCUS_HOOK

    def tearDown(self):
        self.box.FOCUS_HOOK = self._hook
        self.srv.shutdown()
        self.srv.server_close()

    def test_second_launch_focuses_the_window_that_is_open(self):
        import runtime
        # browser mode has no window to bring forward, so a window may still start
        self.box.FOCUS_HOOK = None
        self.assertFalse(runtime.already_running(self.port))
        code, raw = request(self.srv, "/api/focus", {})
        self.assertEqual(json.loads(raw), {"ok": False})
        seen = []
        self.box.FOCUS_HOOK = lambda: seen.append(1)
        code, raw = request(self.srv, "/api/status")
        info = json.loads(raw)
        self.assertEqual(info["app"], "chatbox")
        self.assertTrue(info["window"])
        self.assertTrue(runtime.already_running(self.port))
        self.assertEqual(seen, [1])

    def test_nothing_on_the_port_or_another_program_starts_normally(self):
        import runtime
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import socket
        import threading
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        self.assertFalse(runtime.already_running(port, timeout=1))

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"hello": "world"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertFalse(runtime.already_running(other.server_address[1], timeout=2))
        finally:
            other.shutdown()
            other.server_close()

    def test_build_stamp_changes_when_the_page_files_change(self):
        ui = os.path.join(self.tmp, "ui")
        os.makedirs(os.path.join(ui, "js"))
        with open(os.path.join(ui, "index.html"), "w", encoding="utf-8") as handle:
            handle.write("<p>one</p>")
        original = self.box.UI_DIR
        self.box.UI_DIR = ui
        try:
            first = self.box.ui_build()
            self.assertEqual(first, self.box.ui_build())
            code, raw = request(self.srv, "/api/update")
            self.assertEqual(json.loads(raw)["build"], first)
            code, raw = request(self.srv, "/api/status")
            self.assertEqual(json.loads(raw)["build"], first)
            path = os.path.join(ui, "js", "app.js")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("// new")
            self.assertNotEqual(self.box.ui_build(), first)
        finally:
            self.box.UI_DIR = original
        code, js = request(self.srv, "/ui/js/app.js")
        self.assertIn(b"noticeNewBuild", js)
        self.assertIn(b"location.reload()", js)
        with open(os.path.join(os.path.dirname(__file__), "..", "app.py"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn("runtime.already_running(PORT)", app)
        self.assertIn("chatbox.FOCUS_HOOK", app)

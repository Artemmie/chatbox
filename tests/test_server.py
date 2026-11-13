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
        self.assertIn(b">About<", raw)
        code, raw = request(self.srv, "/ui/style.css")
        self.assertEqual(code, 200)
        self.assertIn(b"--bg", raw)
        self.assertIsNone(self.box.ui_file("/ui/../../chatbox.py"))
        code, _ = request(self.srv, "/api/status", host="evil.example")
        self.assertEqual(code, 403)

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

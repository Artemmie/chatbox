"""Cheap /api/auth-status checks. CLIs are fakes; nothing here prints a token."""
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authstatus
import chatgpt

from tests.support import isolate, request, serve
from tests.test_server import process_alive, write_cli

SECRET = "sk-LIVESECRET"
REFRESH = "fixture-refresh-value"


def _rows(data):
    return {row["id"]: row for row in data["backends"]}


class AuthStatusTests(unittest.TestCase):
    def setUp(self):
        self._find = authstatus_find()
        self.box, self.tmp = isolate()
        self.account = os.path.join(self.tmp, "chatgpt", "account.json")
        authstatus.invalidate()

    def tearDown(self):
        self.box.find = self._find
        for key in ("AUTH_MODE", "AUTH_LOG", "AUTH_PID"):
            os.environ.pop(key, None)
        authstatus.invalidate()

    def check(self, find=None, run=None, system=None, **kw):
        return authstatus.check_backends(
            find=find or (lambda _cmd: None),
            account_path=self.account,
            ollama_url=kw.pop("ollama_url", "http://localhost:11434"),
            system=system,
            run=run,
            probe=kw.pop("probe", lambda url, timeout: False),
            cli_timeout=kw.pop("cli_timeout", 1),
            ollama_timeout=kw.pop("ollama_timeout", 0.4),
        )

    def write_account(self, **fields):
        os.makedirs(os.path.dirname(self.account), exist_ok=True)
        body = {"active": "cid", "accounts": {"cid": {
            "client_id": "cid", "email": "ada@example.com",
            "scopes": [chatgpt.PLAN_SCOPE], **fields,
        }}}
        with open(self.account, "w", encoding="utf-8") as handle:
            json.dump(body, handle)

    def assert_clean(self, data):
        raw = json.dumps(data)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(REFRESH, raw)
        self.assertNotIn("access_token", raw)
        self.assertNotIn("refresh_token", raw)
        self.assertNotIn("id_token", raw)
        self.assertEqual([row["id"] for row in data["backends"]],
                         ["cursor", "claude", "codex", "chatgpt", "ollama"])
        for row in data["backends"]:
            self.assertEqual(set(row), set(authstatus.FIELDS))
            self.assertIn(row["state"], ("ready", "signed_out", "missing", "unknown"))

    def test_limits_stay_short_and_commands_match_sign_in(self):
        self.assertLessEqual(authstatus.CLI_TIMEOUT, 5)
        self.assertLessEqual(authstatus.OLLAMA_TIMEOUT, 3)
        self.assertGreaterEqual(authstatus.CACHE_SECONDS, 45)
        self.assertLessEqual(authstatus.CACHE_SECONDS, 90)
        self.assertEqual(authstatus.DEFAULT_OLLAMA, "http://localhost:11434")
        self.assertEqual(authstatus.SIGN_IN["cursor"], "cursor-agent login")
        self.assertEqual(authstatus.SIGN_IN["claude"], "claude auth login --claudeai")
        self.assertEqual(authstatus.SIGN_IN["codex"], "codex login --device-auth")
        self.assertEqual(authstatus.SIGN_IN["chatgpt"], "")
        self.assertEqual(authstatus.SIGN_IN["ollama"], "")

    def test_install_hints_differ_on_windows(self):
        import accounts
        self.assertEqual(authstatus.install_hint("cursor", "posix"), accounts.INSTALL["cursor"])
        self.assertEqual(authstatus.install_hint("claude", "darwin"), accounts.INSTALL["claude"])
        self.assertNotIn("bash", authstatus.install_hint("cursor", "nt").lower())
        self.assertIn("cursor.com/download", authstatus.install_hint("cursor", "nt"))
        self.assertIn("install.ps1", authstatus.install_hint("claude", "nt"))
        self.assertNotIn("install.sh", authstatus.install_hint("claude", "nt"))
        self.assertIn("codex/releases", authstatus.install_hint("codex", "posix"))
        self.assertNotIn("mktemp", authstatus.install_hint("codex", "posix"))
        self.assertIn("localhost:11434", authstatus.install_hint("ollama", "nt"))
        self.assertIn("ollama.com/download", authstatus.install_hint("ollama", "posix"))
        leaked = authstatus.install_hint(
            "ollama", "posix",
            "http://user:" + SECRET + "@10.0.0.8:11434/v1?access_token=" + SECRET)
        self.assertNotIn(SECRET, leaked)
        self.assertNotIn("access_token", leaked)
        self.assertIn("10.0.0.8:11434", leaked)

    def test_missing_tools_are_not_launched(self):
        def run(argv, timeout):
            raise AssertionError("launched " + " ".join(argv))

        data = self.check(run=run, system="nt")
        self.assert_clean(data)
        rows = _rows(data)
        for sid in ("cursor", "claude", "codex"):
            self.assertEqual(rows[sid]["state"], "missing")
            self.assertFalse(rows[sid]["installed"])
            self.assertFalse(rows[sid]["signed_in"])
            self.assertTrue(rows[sid]["install_hint"])
            self.assertNotIn("bash", rows[sid]["install_hint"].lower())
        self.assertEqual(rows["chatgpt"]["state"], "signed_out")
        self.assertTrue(rows["chatgpt"]["installed"])
        self.assertEqual(rows["ollama"]["state"], "missing")
        self.assertIn("ollama.com/download", rows["ollama"]["install_hint"])

    def test_not_logged_in_is_not_treated_as_logged_in(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv[1:])
            return 1, "You are not logged in. access_token=" + SECRET, "refresh_token=" + REFRESH

        def find(cmd):
            return "/bin/" + cmd

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        for sid in ("cursor", "claude", "codex"):
            row = _rows(data)[sid]
            self.assertEqual(row["state"], "signed_out")
            self.assertTrue(row["installed"])
            self.assertFalse(row["signed_in"])
            self.assertEqual(row["detail"], "Not signed in")
        self.assertNotIn(["whoami"], calls)
        self.assertEqual(_rows(data)["cursor"]["sign_in_command"], "cursor-agent login")
        self.assertEqual(_rows(data)["claude"]["sign_in_command"], "claude auth login --claudeai")
        self.assertEqual(_rows(data)["codex"]["sign_in_command"], "codex login --device-auth")

    def test_cursor_uses_whoami_when_status_does_not_say(self):
        calls = []

        def run(argv, timeout):
            calls.append(tuple(argv[1:]))
            if argv[1:] == ["status"]:
                return 2, "unknown command: status", ""
            if argv[1:] == ["whoami"]:
                return 0, "ada@example.com\n" + SECRET + "\n", ""
            return 1, "not logged in", ""

        def find(cmd):
            return "/bin/cursor-agent" if cmd == "cursor-agent" else None

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["cursor"]["state"], "ready")
        self.assertTrue(_rows(data)["cursor"]["signed_in"])
        self.assertEqual(calls, [("status",), ("whoami",)])

    def test_whoami_token_alone_is_not_a_sign_in(self):
        def run(argv, timeout):
            if argv[1:] == ["status"]:
                return None, "", ""
            return 0, SECRET, ""

        def find(cmd):
            return "/bin/cursor-agent" if cmd == "cursor-agent" else None

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["cursor"]["state"], "unknown")
        self.assertFalse(_rows(data)["cursor"]["signed_in"])
        self.assertEqual(_rows(data)["cursor"]["detail"], "Couldn't check")

    def test_logged_in_phrase_does_not_copy_the_account_token(self):
        def run(argv, timeout):
            if argv[1:] == ["status"]:
                return 0, "Logged in as " + SECRET, ""
            return 1, "not logged in", ""

        def find(cmd):
            return "/bin/cursor-agent" if cmd == "cursor-agent" else None

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["cursor"]["state"], "ready")
        self.assertEqual(_rows(data)["cursor"]["detail"], "Signed in")

    def test_claude_json_flag_wins_and_drops_token_fields(self):
        def run(argv, timeout):
            self.assertEqual(argv[1:], ["auth", "status"])
            if os.environ.get("AUTH_MODE") == "out":
                return 0, json.dumps({"loggedIn": False, "access_token": SECRET}), ""
            return 1, json.dumps({
                "loggedIn": True, "email": "ada@example.com",
                "accessToken": SECRET, "refresh_token": REFRESH,
            }), ""

        def find(cmd):
            return "/bin/claude" if cmd == "claude" else None

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["claude"]["state"], "ready")
        os.environ["AUTH_MODE"] = "out"
        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["claude"]["state"], "signed_out")

    def test_codex_login_status(self):
        def run(argv, timeout):
            self.assertEqual(argv[1:], ["login", "status"])
            if os.environ.get("AUTH_MODE") == "out":
                return 1, "Not logged in", ""
            return 0, "Logged in using ChatGPT\nrefresh_token=" + REFRESH, ""

        def find(cmd):
            return "/bin/codex" if cmd == "codex" else None

        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["codex"]["state"], "ready")
        os.environ["AUTH_MODE"] = "out"
        data = self.check(find=find, run=run)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["codex"]["state"], "signed_out")

    def test_chatgpt_account_file_without_returning_tokens(self):
        self.write_account(access_token=SECRET, refresh_token=REFRESH,
                           expires_at=time.time() + 3600, id_token="header.payload.sig")
        data = self.check()
        self.assert_clean(data)
        self.assertEqual(_rows(data)["chatgpt"]["state"], "ready")
        self.assertTrue(_rows(data)["chatgpt"]["signed_in"])

        self.write_account(access_token=SECRET, expires_at=time.time() - 30)
        data = self.check()
        self.assert_clean(data)
        self.assertEqual(_rows(data)["chatgpt"]["state"], "signed_out")

        self.write_account(access_token=SECRET, refresh_token=REFRESH,
                           expires_at=time.time() + 3600, plan_usage="disabled")
        data = self.check()
        self.assert_clean(data)
        self.assertEqual(_rows(data)["chatgpt"]["state"], "signed_out")

    def test_ollama_probe_uses_the_configured_address(self):
        seen = {}

        def probe(url, timeout):
            seen["url"] = url
            seen["timeout"] = timeout
            return True

        data = self.check(probe=probe, ollama_url="http://localhost:11434/", ollama_timeout=1.25)
        self.assertEqual(seen["url"], "http://localhost:11434")
        self.assertEqual(seen["timeout"], 1.25)
        row = _rows(data)["ollama"]
        self.assertEqual(row["state"], "ready")
        self.assertTrue(row["signed_in"])
        self.assertEqual(row["detail"], "Running")

    def test_probe_reaches_a_local_server_and_ignores_its_body(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(SECRET.encode())

            def log_message(self, fmt, *args):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        self.assertIs(authstatus.probe_ollama("http://127.0.0.1:%d" % port, 2), True)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        self.assertIs(authstatus.probe_ollama("http://127.0.0.1:%d" % closed_port, 1), False)

    def test_runner_times_out_without_keeping_output_or_the_child(self):
        script = write_cli(self.tmp, "cursor-agent", """\
            #!/usr/bin/env python3
            import os, time
            with open(os.environ["AUTH_PID"], "a", encoding="utf-8") as handle:
                handle.write(str(os.getpid()) + "\\n")
            print(%r, flush=True)
            time.sleep(30)
            """ % SECRET)
        pidfile = os.path.join(self.tmp, "pid")
        os.environ["AUTH_PID"] = pidfile

        def find(cmd):
            return script if cmd == "cursor-agent" else None

        started = time.monotonic()
        data = self.check(find=find, cli_timeout=0.8, probe=lambda url, timeout: False)
        self.assertLess(time.monotonic() - started, 12)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["cursor"]["state"], "unknown")
        self.assertTrue(_rows(data)["cursor"]["installed"])
        with open(pidfile, encoding="utf-8") as handle:
            pids = [int(line) for line in handle if line.strip()]
        self.assertGreaterEqual(len(pids), 1)
        time.sleep(0.3)
        for pid in pids:
            self.assertFalse(process_alive(pid))

    def test_cache_and_fresh_and_a_new_account_file(self):
        calls = {"n": 0}

        def run(argv, timeout):
            calls["n"] += 1
            return 0, "Logged in as ada@example.com", ""

        def find(cmd):
            return "/bin/" + cmd

        kw = dict(find=find, run=run, account_path=self.account,
                  ollama_url="http://127.0.0.1:9", probe=lambda url, timeout: False,
                  cli_timeout=1, ollama_timeout=0.2)
        authstatus.invalidate()
        authstatus.snapshot(**kw)
        first = calls["n"]
        self.assertGreater(first, 0)
        authstatus.snapshot(**kw)
        self.assertEqual(calls["n"], first)
        authstatus.snapshot(fresh=True, **kw)
        self.assertGreater(calls["n"], first)
        authstatus._cache_at = time.monotonic() - authstatus.CACHE_SECONDS - 1
        stalled = calls["n"]
        authstatus.snapshot(**kw)
        self.assertGreater(calls["n"], stalled)
        stamped = calls["n"]
        self.write_account(refresh_token=REFRESH, access_token=SECRET, expires_at=time.time() + 60)
        data = authstatus.snapshot(**kw)
        self.assertGreater(calls["n"], stamped)
        self.assert_clean(data)
        self.assertTrue(_rows(data)["chatgpt"]["signed_in"])

    def test_payload_probes_localhost_11434_by_default(self):
        seen = {}

        def probe(url, timeout):
            seen["url"] = url
            seen["timeout"] = timeout
            return False

        self.box.find = lambda cmd: None
        original = authstatus.probe_ollama
        authstatus.probe_ollama = probe
        try:
            authstatus.invalidate()
            data = self.box.auth_status_payload(True)
        finally:
            authstatus.probe_ollama = original
        self.assertEqual(seen["url"], "http://localhost:11434")
        self.assertLessEqual(seen["timeout"], 3)
        self.assert_clean(data)
        self.assertEqual(_rows(data)["ollama"]["state"], "missing")

    def test_http_endpoint_with_mocked_clis_never_returns_tokens(self):
        body = """\
            #!/usr/bin/env python3
            import json, os, sys
            name = os.path.basename(sys.argv[0]).lower()
            log = os.environ.get("AUTH_LOG")
            if log:
                line = (name + " " + " ".join(sys.argv[1:]) + "\\n").encode()
                fd = os.open(log, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            secret = "sk-LIVESECRET"
            refresh = "fixture-refresh-value"
            if "cursor" in name:
                if sys.argv[1:2] == ["whoami"]:
                    print("not logged in")
                    sys.exit(1)
                print("Logged in as ada@example.com")
                print("access_token=" + secret)
                sys.exit(0)
            if "claude" in name:
                print(json.dumps({"loggedIn": True, "accessToken": secret, "refresh_token": refresh}))
                sys.exit(0)
            if "codex" in name:
                print("Logged in using ChatGPT")
                print("refresh_token=" + refresh)
                sys.exit(0)
            sys.exit(1)
            """
        clis = {cmd: write_cli(self.tmp, cmd, body) for cmd in ("cursor-agent", "claude", "codex")}
        self.box.find = lambda cmd: clis.get(cmd)
        self.write_account(access_token=SECRET, refresh_token=REFRESH,
                           expires_at=time.time() + 3600, id_token="header.payload.sig")
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.box.MEMORY.update_settings({"ollama_url": "http://127.0.0.1:%d" % port})
        log = os.path.join(self.tmp, "auth.log")
        os.environ["AUTH_LOG"] = log
        srv = serve(self.box)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)

        code, _ = request(srv, "/api/auth-status", host="evil.example")
        self.assertEqual(code, 403)
        self.assertFalse(os.path.exists(log))

        code, raw = request(srv, "/api/auth-status")
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assert_clean(data)
        rows = _rows(data)
        for sid in ("cursor", "claude", "codex", "chatgpt"):
            self.assertEqual(rows[sid]["state"], "ready", sid)
            self.assertTrue(rows[sid]["signed_in"], sid)
        self.assertEqual(rows["ollama"]["state"], "missing")
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(REFRESH.encode(), raw)
        self.assertIn(("127.0.0.1:%d" % port).encode(), raw)
        with open(log, encoding="utf-8") as handle:
            logged = handle.read().splitlines()
        self.assertGreaterEqual(len(logged), 3)
        self.assertFalse(any("whoami" in line for line in logged))

        code, raw = request(srv, "/api/auth-status")
        self.assertEqual(code, 200)
        with open(log, encoding="utf-8") as handle:
            cached = handle.read().splitlines()
        self.assertEqual(len(cached), len(logged))
        self.assert_clean(json.loads(raw))

        code, raw = request(srv, "/api/auth-status?fresh=1")
        self.assertEqual(code, 200)
        with open(log, encoding="utf-8") as handle:
            refreshed = handle.read().splitlines()
        self.assertGreater(len(refreshed), len(logged))
        self.assert_clean(json.loads(raw))


def authstatus_find():
    import chatbox
    return chatbox.find


if __name__ == "__main__":
    unittest.main()

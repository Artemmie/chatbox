"""Sign in with ChatGPT helpers: PKCE, ID tokens, and the Responses stream. No network."""
import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import chatgpt

from tests.support import isolate


def _b64_int(number):
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return chatgpt.b64url(raw)


class TokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.key = str(Path(cls.tmp) / "key.pem")
        subprocess.check_call(["openssl", "genrsa", "-out", cls.key, "2048"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        modulus = subprocess.check_output(
            ["openssl", "rsa", "-in", cls.key, "-noout", "-modulus"],
            stderr=subprocess.DEVNULL).decode().strip().split("=", 1)[1]
        n = int(modulus, 16)
        e = 65537
        cls.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                              "n": _b64_int(n), "e": chatgpt.b64url(e.to_bytes(3, "big"))}]}

    def mint(self, payload, alg="RS256"):
        header = {"alg": alg, "kid": "k1", "typ": "JWT"}
        head = chatgpt.b64url(json.dumps(header, separators=(",", ":")).encode())
        body = chatgpt.b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing = f"{head}.{body}".encode()
        if alg == "none":
            return f"{head}.{body}."
        sig = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", self.key], input=signing)
        return f"{head}.{body}.{chatgpt.b64url(sig)}"

    def payload(self, **extra):
        now = int(time.time())
        data = {"iss": chatgpt.ISSUER, "sub": "user-1", "aud": "oaiapp_test",
                "nonce": "nonce-1", "exp": now + 600, "email": "a@b.c"}
        data.update(extra)
        return data, now

    def test_pkce_is_s256(self):
        verifier, challenge = chatgpt.pkce_pair()
        self.assertEqual(challenge, chatgpt.b64url(hashlib.sha256(verifier.encode()).digest()))
        self.assertNotIn("=", challenge)

    def test_id_token_checks(self):
        payload, now = self.payload()
        token = self.mint(payload)
        got = chatgpt.verify_id_token(token, self.jwks, "oaiapp_test", "nonce-1", now=now)
        self.assertEqual(got["email"], "a@b.c")
        with self.assertRaises(ValueError):
            chatgpt.verify_id_token(self.mint(payload, alg="none"), self.jwks, "oaiapp_test", "nonce-1", now=now)
        with self.assertRaises(ValueError):
            chatgpt.verify_id_token(token, self.jwks, "oaiapp_test", "other-nonce", now=now)
        # A 2048-bit signature is 256 bytes, so its base64url form ends with two
        # leftover bits. Flipping only the last character can change padding and
        # leave the decoded signature identical. Change a real byte instead.
        head, body, sig_b64 = token.split(".")
        sig = bytearray(chatgpt.b64url_decode(sig_b64))
        sig[0] ^= 0x01
        tampered = f"{head}.{body}.{chatgpt.b64url(bytes(sig))}"
        self.assertNotEqual(chatgpt.b64url_decode(tampered.split(".")[2]), chatgpt.b64url_decode(sig_b64))
        with self.assertRaises(ValueError):
            chatgpt.verify_id_token(tampered, self.jwks, "oaiapp_test", "nonce-1", now=now)

    def test_responses_need_a_completed_event_and_do_not_store(self):
        parser = chatgpt.ResponsesParser()
        parser.feed("response.output_text.delta",
                    json.dumps({"type": "response.output_text.delta", "delta": "Hi"}))
        self.assertIn("error", parser.finish())
        parser.feed("response.completed", json.dumps({
            "type": "response.completed",
            "response": {"model": "m", "usage": {"input_tokens": 2, "output_tokens": 3}}}))
        done = parser.finish()
        self.assertEqual(done["reply"], "Hi")
        self.assertEqual(done["usage"]["input"], 2)
        failed = chatgpt.ResponsesParser()
        failed.feed("response.failed", json.dumps({
            "type": "response.failed",
            "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "no"}}}))
        self.assertIn("limit", failed.finish()["error"].lower())
        blocked = chatgpt.ResponsesParser()
        blocked.feed("response.failed", json.dumps({
            "type": "response.failed",
            "response": {"error": {"code": "subscription_sharing_user_not_eligible", "message": "no"}}}))
        self.assertEqual(blocked.code, "subscription_sharing_user_not_eligible")
        self.assertIn("doesn't allow apps", blocked.finish()["error"])

        box, tmp = isolate()
        seen = {}
        original_open = chatgpt._open_stream

        def fake_open(url, token, body, timeout, turn):
            seen["url"] = url
            seen["body"] = json.loads(body)
            seen["token"] = token
            sse = (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                'event: response.completed\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1},"model":"m"}}\n\n'
            )

            def chunks():
                yield sse
            return 200, chunks()

        chatgpt._open_stream = fake_open
        self.addCleanup(setattr, chatgpt, "_open_stream", original_open)
        svc = box.chatgpt.SERVICE
        svc.store.put_account({
            "email": "a@b.c", "subject": "user-1", "client_id": "oaiapp_test",
            "access_token": "tok", "refresh_token": "ref", "expires_at": time.time() + 3600,
            "scopes": [chatgpt.PLAN_SCOPE], "id_token": "x",
        })
        mode = stat.S_IMODE(os.stat(svc.store.path).st_mode)
        self.assertEqual(mode, 0o600)
        gen = svc.stream_turn("hello", None, "m", None, 30)
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            result = stop.value
        self.assertTrue(seen["url"].endswith("/v1/responses"))
        self.assertIs(seen["body"]["store"], False)
        self.assertIs(seen["body"]["stream"], True)
        self.assertEqual(seen["token"], "tok")
        self.assertEqual(result["reply"], "OK")
        self.assertEqual(len(result["session_id"]), 32)
        box.chatgpt.init(tempfile.mkdtemp(), lambda url: None)
        self.assertIn("not signed in", box.preflight("chatgpt").lower())

    def test_chatgpt_rejects_a_zip(self):
        import attachments
        raw = b"PK\x03\x04" + b"\x00" * 16
        folder = tempfile.mkdtemp()
        saved = attachments.save(folder, "bundle.zip", "application/zip", base64.b64encode(raw).decode())
        with self.assertRaises(ValueError) as caught:
            attachments.for_chatgpt([attachments.load(folder, saved["id"])])
        self.assertIn("can't take", str(caught.exception))


class SignInRecoveryTests(unittest.TestCase):
    def test_query_keeps_plus_in_the_code(self):
        parsed = chatgpt.parse_oauth_query("code=abc+def%2F&state=s1&client_id=oaiapp_x")
        self.assertEqual(parsed["code"], "abc+def/")
        self.assertEqual(parsed["client_id"], "oaiapp_x")

    def test_callback_ignores_favicon_and_keeps_the_first_code(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), chatgpt._Callback)
        httpd.result = None
        httpd.lock = __import__("threading").Lock()
        notes = []
        httpd.log = lambda step, **fields: notes.append((step, fields))
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=2) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(get("/favicon.ico"), 404)
        self.assertIsNone(httpd.result)
        self.assertEqual(get("/auth/callback"), 204)
        self.assertIsNone(httpd.result)
        self.assertEqual(get("/auth/callback?code=first+code&state=ok&client_id=oaiapp_kept"), 200)
        self.assertEqual(httpd.result["code"], "first+code")
        self.assertEqual(get("/auth/callback?code=second&state=ok&client_id=oaiapp_other"), 200)
        self.assertEqual(httpd.result["code"], "first+code")
        self.assertTrue(any(fields.get("ignored") == "duplicate" for _, fields in notes))

    def _run_login(self, fail_codes):
        folder = tempfile.mkdtemp()
        issued = "oaiapp_kept"
        urls = []
        exchanges = []
        secret_code = "abc+def"
        secret_token = "fixture-access-value"

        def open_url(url):
            urls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = query["redirect_uri"][0]
            state = query["state"][0]
            client = query["client_id"][0]
            code = secret_code if client == "dynamic_agent_client" else "second-code"
            callback = redirect + "?" + urllib.parse.urlencode(
                {"code": code, "state": state, "client_id": issued if client == "dynamic_agent_client" else client})
            urllib.request.urlopen(callback, timeout=5).read()

        def fake_json(method, url, headers, body, timeout):
            form = urllib.parse.parse_qs(body.decode())
            exchanges.append(form)
            code = form["code"][0]
            if code in fail_codes:
                raise chatgpt.HTTPStatus(400, {"error": "invalid_grant", "error_description": "expired"})
            return {"access_token": secret_token, "refresh_token": "fixture-refresh-value", "id_token": "header.payload.sig",
                    "expires_in": 3600,
                    "scope": "openid profile email offline_access resource.invoke " + chatgpt.PLAN_SCOPE}

        original_json = chatgpt._json_request
        original_verify = chatgpt.verify_id_token
        original_discovery = chatgpt.load_discovery
        chatgpt._json_request = fake_json
        chatgpt.verify_id_token = lambda token, jwks, audience, nonce, now=None: {
            "sub": "user-1", "email": "a@b.c", "iss": chatgpt.ISSUER}
        chatgpt.load_discovery = lambda: None
        self.addCleanup(setattr, chatgpt, "_json_request", original_json)
        self.addCleanup(setattr, chatgpt, "verify_id_token", original_verify)
        self.addCleanup(setattr, chatgpt, "load_discovery", original_discovery)
        service = chatgpt.Service(folder, open_url)
        service._jwks = lambda force=False: {"keys": []}
        job = chatgpt._Login()
        service._login_flow(job, fresh=True)
        with open(os.path.join(folder, "chatbox.log"), encoding="utf-8") as handle:
            log = handle.read()
        return service, job, urls, exchanges, log, issued, secret_code, secret_token

    def test_invalid_grant_retries_once_with_the_issued_client(self):
        service, job, urls, exchanges, log, issued, secret_code, secret_token = self._run_login({ "abc+def" })
        self.assertEqual(job.state, "done", job.last)
        self.assertEqual(len(urls), 2)
        first = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
        second = urllib.parse.parse_qs(urllib.parse.urlparse(urls[1]).query)
        self.assertEqual(first["client_id"][0], "dynamic_agent_client")
        self.assertEqual(first["agent_name_hint"][0], "Chatbox")
        self.assertEqual(second["client_id"][0], issued)
        self.assertNotIn("agent_name_hint", second)
        self.assertEqual(first["ext_agent_host_id"], second["ext_agent_host_id"])
        self.assertTrue(first["ext_agent_host_id"][0].startswith("urn:uuid:"))
        self.assertEqual(len(exchanges), 2)
        for form, url in zip(exchanges, urls):
            auth = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = form["redirect_uri"][0]
            self.assertEqual(redirect, auth["redirect_uri"][0])
            self.assertTrue(redirect.startswith("http://127.0.0.1:"))
            self.assertTrue(redirect.endswith("/auth/callback"))
            self.assertNotIn("localhost", redirect)
            self.assertEqual(form["resource"][0], "https://api.openai.com/v1")
            self.assertEqual(form["client_id"][0], issued)
            challenge = chatgpt.b64url(hashlib.sha256(form["code_verifier"][0].encode()).digest())
            self.assertEqual(challenge, auth["code_challenge"][0])
        self.assertEqual(exchanges[0]["code"][0], secret_code)
        self.assertNotEqual(exchanges[0]["code_verifier"], exchanges[1]["code_verifier"])
        self.assertNotEqual(exchanges[0]["redirect_uri"], exchanges[1]["redirect_uri"])
        self.assertTrue(service.logged_in())
        self.assertEqual(service.store.active()["client_id"], issued)
        self.assertIn("error=invalid_grant", log)
        self.assertIn("step=retry", log)
        self.assertIn("client_kind=issued", log)
        self.assertIn("http_status=400", log)
        self.assertNotIn(secret_code, log)
        self.assertNotIn(secret_token, log)
        self.assertNotIn(exchanges[0]["code_verifier"][0], log)
        self.assertNotIn("oaiapp_kept", log)

    def test_second_invalid_grant_stops_and_keeps_the_client(self):
        service, job, urls, exchanges, log, issued, secret_code, secret_token = self._run_login(
            {"abc+def", "second-code"})
        self.assertEqual(job.state, "failed")
        self.assertIn("kept this registration", job.last)
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(exchanges), 2)
        self.assertFalse(service.logged_in())
        self.assertEqual(service.store.active()["client_id"], issued)
        self.assertNotIn(secret_code, log)
        self.assertNotIn("second-code", log)

    def test_log_drops_secret_fields(self):
        folder = tempfile.mkdtemp()
        line = chatgpt.signin_log(os.path.join(folder, "chatbox.log"), "token_exchange",
                                  code="should-not-appear", client_kind="issued", http_status=400,
                                  error="invalid_grant")
        self.assertNotIn("should-not-appear", line)
        self.assertIn("client_kind=issued", line)
        self.assertIn("error=invalid_grant", line)


class PlanUsageTests(unittest.TestCase):
    """Consent, missing plan scope, and an ineligible workspace stop OAuth. They do not loop."""

    def _service(self, open_url, scope):
        folder = tempfile.mkdtemp()
        issued = "oaiapp_kept"

        def fake_json(method, url, headers, body, timeout):
            return {"access_token": "fixture-access-value", "refresh_token": "fixture-refresh-value",
                    "id_token": "header.payload.sig", "expires_in": 3600, "scope": scope}

        original = (chatgpt._json_request, chatgpt.verify_id_token, chatgpt.load_discovery)
        chatgpt._json_request = fake_json
        chatgpt.verify_id_token = lambda token, jwks, audience, nonce, now=None: {
            "sub": "user-1", "email": "a@b.c", "iss": chatgpt.ISSUER}
        chatgpt.load_discovery = lambda: None
        self.addCleanup(setattr, chatgpt, "_json_request", original[0])
        self.addCleanup(setattr, chatgpt, "verify_id_token", original[1])
        self.addCleanup(setattr, chatgpt, "load_discovery", original[2])
        service = chatgpt.Service(folder, open_url)
        service._jwks = lambda force=False: {"keys": []}
        return service, issued

    def _callback(self, url, **fields):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        redirect = query["redirect_uri"][0]
        fields.setdefault("state", query["state"][0])
        urllib.request.urlopen(redirect + "?" + urllib.parse.urlencode(fields), timeout=5).read()

    def test_access_denied_marks_plan_disabled_and_does_not_retry(self):
        urls = []

        def open_url(url):
            urls.append(url)
            self._callback(url, error="access_denied", client_id="oaiapp_kept")

        service, issued = self._service(open_url, chatgpt.PLAN_SCOPE)
        job = chatgpt._Login()
        service._login_flow(job, fresh=True)
        self.assertEqual(job.state, "blocked", job.last)
        self.assertEqual(len(urls), 1)
        self.assertIn("doesn't allow apps", job.last)
        self.assertIn("Codex", job.last)
        self.assertTrue(service.store.plan_blocked())
        self.assertFalse(service.logged_in())
        self.assertEqual(service.store.active()["client_id"], issued)
        self.assertEqual(service.status()["plan_message"], chatgpt.PLAN_BLOCKED_MESSAGE)

    def test_missing_plan_scope_keeps_sign_in_and_does_not_retry(self):
        urls = []

        def open_url(url):
            urls.append(url)
            self._callback(url, code="one-code", client_id="oaiapp_kept")

        scope = "openid profile email offline_access resource.invoke"
        service, issued = self._service(open_url, scope)
        job = chatgpt._Login()
        service._login_flow(job, fresh=True)
        self.assertEqual(job.state, "blocked", job.last)
        self.assertEqual(len(urls), 1)
        self.assertIn("doesn't allow apps", job.last)
        acc = service.store.active()
        self.assertEqual(acc["email"], "a@b.c")
        self.assertEqual(acc["client_id"], issued)
        self.assertNotIn(chatgpt.PLAN_SCOPE, acc["scopes"])
        self.assertEqual(acc["plan_usage"], "disabled")
        self.assertFalse(service.logged_in())
        self.assertTrue(service.status()["plan_blocked"])

    def test_ineligible_response_disables_plan_and_is_not_retried(self):
        folder = tempfile.mkdtemp()
        service = chatgpt.Service(folder, lambda url: None)
        service.store.put_account({
            "email": "a@b.c", "subject": "user-1", "client_id": "oaiapp_test",
            "access_token": "fixture-access-value", "refresh_token": "fixture-refresh-value",
            "expires_at": time.time() + 3600, "plan_usage": "enabled",
            "scopes": chatgpt.SCOPES.split(),
        }, active=True)
        calls = []
        body = json.dumps({"error": {"code": "subscription_sharing_user_not_eligible", "message": "no"}})

        def fake_open(url, token, payload, timeout, turn):
            calls.append(url)
            return 403, [body]

        original = chatgpt._open_stream
        chatgpt._open_stream = fake_open
        self.addCleanup(setattr, chatgpt, "_open_stream", original)
        def run(message):
            gen = service.stream_turn(message, None, "m", None, 30)
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                return stop.value

        first = run("Hi")
        self.assertEqual(first["error"], chatgpt.PLAN_BLOCKED_MESSAGE)
        self.assertEqual(service.store.active()["plan_usage"], "disabled")
        second = run("Hi again")
        self.assertEqual(second["error"], chatgpt.PLAN_BLOCKED_MESSAGE)
        self.assertEqual(len(calls), 1)

    def test_closed_browser_times_out_without_disabling_plan(self):
        original = chatgpt.LOGIN_LIMIT
        chatgpt.LOGIN_LIMIT = 0
        self.addCleanup(setattr, chatgpt, "LOGIN_LIMIT", original)
        urls = []
        service, _issued = self._service(lambda url: urls.append(url), chatgpt.PLAN_SCOPE)
        job = chatgpt._Login()
        service._login_flow(job, fresh=True)
        self.assertEqual(job.state, "timeout", job.last)
        self.assertIn("closed the sign-in window", job.last)
        self.assertEqual(len(urls), 1)
        self.assertFalse(service.store.plan_blocked())
        self.assertNotIn("doesn't allow apps", job.last)


if __name__ == "__main__":
    unittest.main()

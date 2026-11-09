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
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
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


if __name__ == "__main__":
    unittest.main()

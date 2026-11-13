"""ChatGPT in Chatbox, using the user's own ChatGPT plan. No API key.

This is OpenAI's documented Sign in with ChatGPT flow for open-source apps that
run on the user's computer:

  https://developers.openai.com/siwc/token-sharing-open-source
  https://developers.openai.com/siwc/token-sharing-open-source/sign-in
  https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference

The user signs in in their browser. Chatbox stores the OAuth tokens locally
(mode 0600) and calls the public Responses API with store=false and stream=true.
It does not read browser cookies, scrape chatgpt.com, or call undocumented
backend-api endpoints.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import streaming

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
# Shown when consent, the granted scopes, or Responses say this plan cannot be shared with apps.
PLAN_BLOCKED_MESSAGE = (
    "Your ChatGPT plan or workspace doesn't allow apps to use it yet. "
    "Try signing in with a different workspace, or use Codex, which runs on your ChatGPT subscription."
)
SIGNIN_TIMEOUT_MESSAGE = (
    "You closed the sign-in window, or it timed out. Nothing was changed. "
    "Click Continue with ChatGPT when you want to try again."
)
CALLBACK_PATH = "/auth/callback"
AGENT_NAME = "Chatbox"
LOGIN_LIMIT = 10 * 60
SESSION_ID = re.compile(r"^[a-f0-9]{32}$")

# Documented production endpoints. Discovery can override these; tests can too.
ENDPOINTS = {
    "issuer": ISSUER,
    "authorization": ISSUER + "/api/accounts/authorize",
    "token": ISSUER + "/api/accounts/oauth/token",
    "jwks": ISSUER + "/.well-known/jwks.json",
    "revocation": ISSUER + "/api/accounts/oauth/revoke",
}


class HTTPStatus(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


def http_request(method, url, headers=None, body=None, timeout=30):
    """Perform one HTTP request. Returns (status, response headers, body bytes)."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _json_request(method, url, headers, body, timeout):
    status, hdrs, raw = http_request(method, url, headers, body, timeout)
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = {"error": raw.decode(errors="replace")[:500]}
    if status >= 400:
        raise HTTPStatus(status, data)
    return data


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(text):
    if isinstance(text, str):
        text = text.encode()
    return base64.urlsafe_b64decode(text + b"=" * (-len(text) % 4))


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def rsa_pkcs1_sha256_verify(message, signature, n, e):
    """True when signature is RSASSA-PKCS1-v1_5 over SHA-256."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s_int = int.from_bytes(signature, "big")
    if not 0 < s_int < n:
        return False
    em = pow(s_int, e, n).to_bytes(k, "big")
    digest = hashlib.sha256(message).digest()
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    if k < len(prefix) + len(digest) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(prefix) - len(digest) - 3) + b"\x00" + prefix + digest
    return em == expected


def verify_id_token(token, jwks, audience, nonce, now=None):
    """Validate an OpenAI ID token. Returns the payload, or raises ValueError."""
    try:
        h_b, p_b, s_b = token.split(".")
    except ValueError:
        raise ValueError("The ID token was not a JWT.") from None
    try:
        header = json.loads(b64url_decode(h_b))
        payload = json.loads(b64url_decode(p_b))
        signature = b64url_decode(s_b)
    except (ValueError, json.JSONDecodeError):
        raise ValueError("The ID token could not be read.") from None
    if header.get("alg") != "RS256":
        raise ValueError("The ID token used an unexpected algorithm.")
    keys = [k for k in (jwks or {}).get("keys", []) if k.get("kty") == "RSA"]
    kid = header.get("kid")
    if kid:
        keys = [k for k in keys if k.get("kid") == kid]
    if len(keys) != 1:
        raise ValueError("The ID token's signing key was not found.")
    key = keys[0]
    try:
        n = int.from_bytes(b64url_decode(key["n"]), "big")
        e = int.from_bytes(b64url_decode(key["e"]), "big")
    except (KeyError, ValueError):
        raise ValueError("The signing key could not be read.") from None
    if not rsa_pkcs1_sha256_verify(f"{h_b}.{p_b}".encode(), signature, n, e):
        raise ValueError("The ID token signature is not valid.")
    now = time.time() if now is None else now
    if payload.get("iss") != ENDPOINTS["issuer"]:
        raise ValueError("The ID token issuer was not OpenAI.")
    aud = payload.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if audience not in auds:
        raise ValueError("The ID token was not issued for this sign-in.")
    if not isinstance(payload.get("exp"), (int, float)) or payload["exp"] < now - 60:
        raise ValueError("The ID token has expired.")
    if not nonce or payload.get("nonce") != nonce:
        raise ValueError("The ID token did not match this sign-in attempt.")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise ValueError("The ID token had no account id.")
    return payload


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


class AccountStore:
    """One local credential file. Tokens are never returned to the browser."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "chatgpt")
        self.path = os.path.join(self.dir, "account.json")
        self.sessions = os.path.join(self.dir, "sessions")
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("accounts", {})
        if not str(data.get("host_id", "")).startswith("urn:uuid:"):
            data["host_id"] = "urn:uuid:" + str(uuid.uuid4())
            self.save(data)
        return data

    def save(self, data):
        _write_private(self.path, json.dumps(data, indent=2))

    def host_id(self):
        with self.lock:
            return self.load()["host_id"]

    def active(self):
        with self.lock:
            data = self.load()
            acc = data["accounts"].get(data.get("active") or "")
            return dict(acc) if isinstance(acc, dict) else None

    def logged_in(self):
        acc = self.active()
        if not acc or acc.get("plan_usage") == "disabled" or PLAN_SCOPE not in (acc.get("scopes") or []):
            return False
        return bool(acc.get("refresh_token") or (acc.get("access_token") and acc.get("expires_at", 0) > time.time()))

    def plan_blocked(self):
        with self.lock:
            data = self.load()
        if data.get("plan_usage") == "disabled":
            return True
        acc = data["accounts"].get(data.get("active") or "")
        return isinstance(acc, dict) and acc.get("plan_usage") == "disabled"

    def set_plan_usage(self, enabled, client_id=None):
        """Remember whether this computer may spend the ChatGPT plan. Does not start OAuth."""
        value = "enabled" if enabled else "disabled"
        with self.lock:
            data = self.load()
            data["plan_usage"] = value
            cid = client_id or data.get("active") or ""
            acc = data["accounts"].get(cid)
            if isinstance(acc, dict):
                acc["plan_usage"] = value
            self.save(data)

    def put_account(self, account, active=True):
        with self.lock:
            data = self.load()
            data["accounts"][account["client_id"]] = account
            if active:
                data["active"] = account["client_id"]
            self.save(data)

    def remember_client(self, client_id):
        """Keep an issued client id before tokens exist, so a retry does not register a new one."""
        if not client_id or client_id == "dynamic_agent_client":
            return
        with self.lock:
            data = self.load()
            acc = data["accounts"].get(client_id)
            if not isinstance(acc, dict):
                acc = {"client_id": client_id, "email": "", "subject": "", "scopes": []}
            else:
                acc = dict(acc)
                acc["client_id"] = client_id
            data["accounts"][client_id] = acc
            data["active"] = client_id
            self.save(data)

    def clear_tokens(self):
        """Sign out: forget tokens, keep the client id so the next sign-in can reuse it."""
        with self.lock:
            data = self.load()
            acc = data["accounts"].get(data.get("active") or "")
            if isinstance(acc, dict):
                for key in ("access_token", "refresh_token", "id_token", "expires_at"):
                    acc.pop(key, None)
                acc["scopes"] = [s for s in acc.get("scopes") or [] if s != PLAN_SCOPE]
            self.save(data)
            return data

    def session_messages(self, sid):
        if not sid or not SESSION_ID.match(sid):
            return []
        try:
            with open(os.path.join(self.sessions, sid + ".json"), encoding="utf-8") as f:
                return json.load(f).get("messages", [])
        except (OSError, ValueError):
            return []

    def save_session(self, sid, messages):
        os.makedirs(self.sessions, exist_ok=True)
        path = os.path.join(self.sessions, sid + ".json")
        _write_private(path, json.dumps({"messages": messages}))


def _form(fields):
    return urllib.parse.urlencode(fields).encode()


def _token_error(body):
    if isinstance(body, dict):
        return body.get("error_description") or body.get("error") or "Sign-in failed."
    return "Sign-in failed."


def client_kind(client_id):
    """'dynamic' for the first-time entrypoint, otherwise an issued registration."""
    if not client_id or client_id == "dynamic_agent_client":
        return "dynamic"
    return "issued"


def parse_oauth_query(query):
    """Parse a callback query without turning '+' into a space (authorization codes may contain it)."""
    out = {}
    for part in (query or "").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = urllib.parse.unquote(key)
        value = urllib.parse.unquote(value)
        if key and key not in out:
            out[key] = value
    return out


_LOG_KEYS = {"client_kind", "http_status", "error", "error_description", "redirect_host",
             "redirect_path", "redirect_port", "resource", "pkce", "reason", "has_code",
             "ignored", "detail"}


def _safe_detail(value):
    text = str("" if value is None else value).replace("\n", " ")
    text = re.sub(r"(?i)\b(code|token|verifier|secret|id_token|refresh_token|access_token)=[^&\s]+",
                  r"\1=redacted", text)
    return text[:300]


def signin_log(log_path, step, **fields):
    """One line in chatbox.log. Never pass codes, tokens, or PKCE verifiers."""
    parts = [f"step={_safe_detail(step)}"]
    for key in sorted(fields):
        if key not in _LOG_KEYS:
            continue
        value = _safe_detail(fields[key]) if key in ("error", "error_description", "detail") else str(fields[key])
        value = value.replace("\n", " ")[:120]
        parts.append(f"{key}={value}")
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " chatgpt-signin " + " ".join(parts)
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return line


def oauth_error_fields(exc):
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    err = body.get("error") if isinstance(body.get("error"), str) else ""
    desc = body.get("error_description") if isinstance(body.get("error_description"), str) else ""
    return {"http_status": getattr(exc, "status", 0), "error": err, "error_description": desc}


def friendly_api_error(code, message):
    if code == "subscription_sharing_user_not_eligible":
        return PLAN_BLOCKED_MESSAGE
    if code == "subscription_sharing_usage_limit_exceeded":
        return ("ChatGPT plan limit reached for now. Usage is shared with other apps on this plan. "
                "Check ChatGPT settings, then try again later.")
    if code == "subscription_sharing_usage_unavailable":
        return ("ChatGPT plan usage isn't available for this account. Plus or Pro is required, "
                "and Chatbox must be allowed in ChatGPT settings.")
    return message or code or "ChatGPT could not answer."


def classify_error(raw):
    """(machine code, message) from a Responses or admission body. Code may be empty."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        return "", "ChatGPT could not answer."
    if not isinstance(data, dict):
        return "", "ChatGPT could not answer."
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code") or ""
        if not isinstance(code, str):
            code = ""
        return code, friendly_api_error(code, err.get("message") or "")
    if isinstance(err, str) and err:
        return "", friendly_api_error("", err)
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return "", detail[:300]
    return "", "ChatGPT could not answer."


class ResponsesParser:
    """SSE events from POST /v1/responses. Success requires response.completed."""

    def __init__(self):
        self.buf = ""
        self.completed = False
        self.error = None
        self.code = ""
        self.usage = {}

    def feed(self, event_name, data):
        if not data or data == "[DONE]":
            return []
        try:
            ev = json.loads(data)
        except ValueError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type") or event_name
        if kind == "response.output_text.delta":
            delta = ev.get("delta") or ""
            if delta:
                self.buf += delta
                return [("delta", delta)]
        elif kind == "response.completed":
            self.completed = True
            resp = ev.get("response") or {}
            usage = resp.get("usage") or {}
            self.usage = {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
                          "model": resp.get("model")}
            if not self.buf:
                self.buf = output_text(resp)
                return [("delta", self.buf)] if self.buf else []
        elif kind in ("response.failed", "error"):
            err = ev.get("error") or (ev.get("response") or {}).get("error") or {}
            if isinstance(err, dict):
                self.code = err.get("code") or ""
                self.error = friendly_api_error(self.code, err.get("message") or "")
            else:
                self.error = friendly_api_error("", str(err))
        elif kind == "response.incomplete":
            self.error = "ChatGPT stopped before the reply was finished."
        return []

    def finish(self):
        if not self.completed:
            return {"error": self.error or "ChatGPT did not finish the reply."}
        if self.error and not self.buf:
            return {"error": self.error}
        return {"reply": self.buf, "usage": self.usage}


def output_text(resp):
    parts = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(block.get("text") or "")
    return "".join(parts)


def iter_sse(chunks):
    """Yield (event_name, data) from an SSE byte stream given as text chunks."""
    buf = ""
    for chunk in chunks:
        buf += chunk
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            event, data = "message", []
            for line in raw.splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                yield event, "\n".join(data)
    if buf.strip():
        event, data = "message", []
        for line in buf.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            yield event, "\n".join(data)


class Service:
    def __init__(self, data_dir, open_url):
        self.store = AccountStore(data_dir)
        self.open_url = open_url
        self.job = None
        self.jwks = None
        self.jwks_at = 0
        self.lock = threading.Lock()

    # ---- status / actions (Accounts screen) ----------------------------
    def status(self):
        acc = self.store.active()
        logged = self.store.logged_in()
        blocked = self.store.plan_blocked()
        email = ((acc or {}).get("email") or "") if (logged or blocked) else ""
        job = self.job.view() if self.job else None
        return {"id": "chatgpt", "name": "ChatGPT",
                "plan": "ChatGPT Plus or Pro, via Sign in with ChatGPT (no API key)",
                "installed": True, "logged_in": logged, "account": email or None,
                "plan_blocked": blocked,
                "plan_message": PLAN_BLOCKED_MESSAGE if blocked else "",
                "has_client": bool(acc and acc.get("client_id")),
                "job": job}

    def action(self, action):
        if action == "cancel":
            if self.job and self.job.state == "running":
                self.job.cancel()
            return
        if action == "dismiss":
            if self.job and self.job.state != "running":
                self.job = None
            return
        if action == "logout":
            self._logout()
            return
        if action not in ("login", "login_new"):
            raise ValueError("Unknown action")
        if self.job and self.job.state == "running":
            raise ValueError("Already running — finish or cancel it first.")
        fresh = action == "login_new"
        self.job = _Login()
        threading.Thread(target=self._login, args=(self.job, fresh), daemon=True).start()

    def logged_in(self):
        return self.store.logged_in()

    # ---- OAuth ----------------------------------------------------------
    def _jwks(self, force=False):
        if self.jwks and not force and time.time() - self.jwks_at < 3600:
            return self.jwks
        data = _json_request("GET", ENDPOINTS["jwks"], {"Accept": "application/json"}, None, 20)
        self.jwks, self.jwks_at = data, time.time()
        return data

    def _log_path(self):
        return os.path.join(os.path.dirname(self.store.dir), "chatbox.log")

    def _signin_log(self, step, **fields):
        return signin_log(self._log_path(), step, **fields)

    def _login(self, job, fresh):
        try:
            self._login_flow(job, fresh)
        except Exception as e:
            self._signin_log("login", detail=type(e).__name__)
            if job.state == "running":
                job.fail(str(e) or "Sign-in failed.")

    def _login_flow(self, job, fresh):
        """One sign-in. On invalid_grant, keep the issued client and authorize once more with it."""
        existing = None if fresh else self.store.active()
        client_id = (existing or {}).get("client_id") or "dynamic_agent_client"
        outcome = self._authorize_once(job, client_id, existing)
        if outcome.get("finished") or outcome.get("cancelled") or job.state != "running":
            return
        issued = outcome.get("issued")
        if outcome.get("invalid_grant") and issued and issued != "dynamic_agent_client":
            self._signin_log("retry", client_kind="issued", reason="invalid_grant")
            job.last = ("The first sign-in code was rejected, which can happen while ChatGPT finishes "
                        "registering this app. Your browser will open once more — pick the same account. "
                        "You should not have to approve a new app.")
            job.output = job.last
            outcome = self._authorize_once(job, issued, self.store.active() or existing)
            if outcome.get("finished") or outcome.get("cancelled") or job.state != "running":
                return
        if outcome.get("tokens"):
            self._accept_tokens(job, outcome["tokens"], outcome["issued"], outcome["nonce"],
                                previous=outcome.get("existing"))
            return
        if job.state == "running":
            message = outcome.get("message") or "Sign-in failed."
            if outcome.get("invalid_grant"):
                message += (" Chatbox kept this registration. Click Continue with ChatGPT again; "
                            "the browser should only ask you to confirm the account.")
            job.fail(message)

    def _authorize_once(self, job, client_id, existing):
        """Open the browser once and exchange the returned code. Does not mark the job failed."""
        load_discovery()
        host_id = self.store.host_id()
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Callback)
        httpd.result = None
        httpd.lock = threading.Lock()
        httpd.log = lambda step, **fields: self._signin_log(step, **fields)
        port = httpd.server_address[1]
        redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        kind = client_kind(client_id)
        self._signin_log("authorize", client_kind=kind, redirect_host="127.0.0.1",
                         redirect_path=CALLBACK_PATH, redirect_port=port, resource="set", pkce="S256")
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "ext_agent_host_id": host_id,
        }
        if client_id == "dynamic_agent_client":
            params["agent_name_hint"] = AGENT_NAME
        elif existing and existing.get("email"):
            params["login_hint"] = existing["email"]
        url = ENDPOINTS["authorization"] + "?" + urllib.parse.urlencode(params)
        # id_token_hint is sent only to OpenAI's authorize endpoint, never written to the log or the Accounts panel.
        if client_id != "dynamic_agent_client" and existing and existing.get("id_token"):
            url += "&" + urllib.parse.urlencode({"id_token_hint": existing["id_token"]})
            job.url = None
            if not job.last:
                job.last = "Continue in your browser to sign in again."
        else:
            job.url = url
            if not job.last:
                job.last = "Continue with ChatGPT in your browser."
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        job.httpd = httpd
        for _ in range(50):  # docs: the loopback listener is up before the browser opens
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                    break
            except OSError:
                time.sleep(0.02)
        try:
            try:
                self.open_url(url)
            except Exception:
                self._signin_log("authorize", client_kind=kind, detail="browser_open_failed")
                if not job.last:
                    job.last = "Open the sign-in page in your browser."
            deadline = time.time() + LOGIN_LIMIT
            while time.time() < deadline and job.state == "running":
                with httpd.lock:
                    if httpd.result is not None:
                        break
                time.sleep(0.05)
            with httpd.lock:
                captured = dict(httpd.result) if httpd.result else None
        finally:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
            job.httpd = None
        if job.state == "cancelled":
            self._signin_log("authorize", client_kind=kind, detail="cancelled")
            return {"cancelled": True, "finished": True}
        if not captured:
            # The browser was closed, or the wait ended, with no callback. Do not start another one.
            self._signin_log("callback", client_kind=kind, detail="timeout")
            job.timeout(SIGNIN_TIMEOUT_MESSAGE)
            return {"finished": True}
        if captured.get("state") != state:
            self._signin_log("callback", client_kind=kind, detail="state_mismatch")
            return {"message": "Sign-in could not be verified (the browser returned an unexpected response)."}
        if captured.get("error"):
            err = captured.get("error") or ""
            self._signin_log("callback", client_kind=kind,
                             error=err if err in ("access_denied", "invalid_request", "server_error") else "oauth_error")
            if err == "access_denied":
                # Consent was declined or the workspace blocked plan use. Keep the registration
                # and stop. Do not open the browser again.
                issued_id = captured.get("client_id") or ""
                if issued_id and issued_id != "dynamic_agent_client":
                    self.store.remember_client(issued_id)
                    self.store.set_plan_usage(False, issued_id)
                else:
                    self.store.set_plan_usage(False)
                job.blocked(PLAN_BLOCKED_MESSAGE)
                return {"finished": True}
            return {"message": "Sign-in was not completed."}
        issued = captured.get("client_id") or (None if client_id == "dynamic_agent_client" else client_id)
        if client_id == "dynamic_agent_client":
            if not issued or issued == "dynamic_agent_client":
                self._signin_log("callback", client_kind="dynamic", detail="missing_issued_client")
                return {"message": "Sign-in did not finish registering Chatbox. Try again."}
        elif issued and issued != client_id:
            self._signin_log("callback", client_kind=client_kind(issued), detail="client_id_mismatch")
            return {"message": "Sign-in returned a different ChatGPT registration than the one on this computer."}
        else:
            issued = client_id
        # Retain the registration before exchanging, per the sign-in docs. A rejected code must not
        # force the next attempt to call dynamic_agent_client again.
        self.store.remember_client(issued)
        self._signin_log("callback", client_kind=client_kind(issued),
                         has_code="yes" if captured.get("code") else "no")
        if not captured.get("code"):
            return {"issued": issued, "message": "Sign-in did not return a code. Try again."}
        form = {"grant_type": "authorization_code", "client_id": issued, "code": captured["code"],
                "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE}
        try:
            tokens = _json_request("POST", ENDPOINTS["token"],
                                   {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                                   _form(form), 30)
        except HTTPStatus as exc:
            info = oauth_error_fields(exc)
            self._signin_log("token_exchange", client_kind=client_kind(issued), **info)
            invalid = info.get("error") == "invalid_grant"
            return {"invalid_grant": invalid, "issued": issued,
                    "message": "Could not finish sign-in. " + (_token_error(exc.body) or "Error")}
        except (urllib.error.URLError, OSError, socket.timeout):
            self._signin_log("token_exchange", client_kind=client_kind(issued), detail="network")
            return {"issued": issued, "message": "Could not reach OpenAI to finish sign-in. Try again."}
        self._signin_log("token_exchange", client_kind=client_kind(issued), http_status=200)
        return {"tokens": tokens, "issued": issued, "nonce": nonce, "existing": existing}

    def _accept_tokens(self, job, tokens, client_id, nonce, previous):
        scopes = (tokens.get("scope") or "").split()
        id_token = tokens.get("id_token")
        if not id_token or not tokens.get("access_token"):
            job.fail("Sign-in did not return the expected credentials.")
            return
        try:
            payload = verify_id_token(id_token, self._jwks(), client_id, nonce)
        except ValueError:
            try:
                payload = verify_id_token(id_token, self._jwks(force=True), client_id, nonce)
            except ValueError as e:
                job.fail(str(e))
                return
        if previous and previous.get("subject") and payload.get("sub") != previous.get("subject"):
            job.fail("That sign-in was for a different ChatGPT account. Use “Use a different account”.")
            return
        record = _record(tokens, payload, client_id, scopes)
        if PLAN_SCOPE not in scopes:
            # A valid sign-in without plan permission. Keep it, and do not start OAuth again.
            record["plan_usage"] = "disabled"
            self.store.put_account(record, active=True)
            self.store.set_plan_usage(False, client_id)
            job.blocked(PLAN_BLOCKED_MESSAGE)
            return
        record["plan_usage"] = "enabled"
        self.store.put_account(record, active=True)
        self.store.set_plan_usage(True, client_id)
        job.succeed("Signed in" + (f" as {payload.get('email')}" if payload.get("email") else "") + ".")

    def _logout(self):
        load_discovery()
        acc = self.store.active()
        error = None
        if acc and acc.get("refresh_token") and acc.get("client_id"):
            try:
                status, _, _ = http_request(
                    "POST", ENDPOINTS["revocation"],
                    {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                    _form({"token": acc["refresh_token"], "token_type_hint": "refresh_token",
                           "client_id": acc["client_id"]}), 20)
                if status >= 500:
                    error = "Chatbox signed out on this computer, but could not confirm sign-out with OpenAI. You can disconnect Chatbox in ChatGPT settings."
                elif status >= 400 and status != 200:
                    error = "Chatbox signed out on this computer, but OpenAI did not confirm it. You can disconnect Chatbox in ChatGPT settings."
            except (urllib.error.URLError, OSError, socket.timeout):
                error = "Chatbox signed out on this computer, but could not reach OpenAI to finish sign-out. You can disconnect Chatbox in ChatGPT settings."
        self.store.clear_tokens()
        if error:
            self.job = _Login()
            self.job.fail(error)

    def access_token(self):
        """A usable access token, refreshing if it is close to expiry."""
        with self.lock:
            acc = self.store.active()
            if not acc or not acc.get("access_token"):
                raise RuntimeError("ChatGPT is not signed in. Open Accounts and choose Continue with ChatGPT.")
            if acc.get("plan_usage") == "disabled" or PLAN_SCOPE not in (acc.get("scopes") or []):
                raise RuntimeError(PLAN_BLOCKED_MESSAGE)
            if acc.get("expires_at", 0) > time.time() + 60:
                return acc["access_token"], acc
            if not acc.get("refresh_token"):
                raise RuntimeError("ChatGPT session expired. Open Accounts and choose Continue with ChatGPT.")
            try:
                tokens = _json_request(
                    "POST", ENDPOINTS["token"],
                    {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                    _form({"grant_type": "refresh_token", "client_id": acc["client_id"],
                           "refresh_token": acc["refresh_token"], "resource": RESOURCE}), 30)
            except HTTPStatus as e:
                raise RuntimeError("ChatGPT session expired. Open Accounts and choose Continue with ChatGPT.") from e
            scopes = (tokens.get("scope") or " ".join(acc.get("scopes") or [])).split()
            updated = dict(acc)
            updated["access_token"] = tokens.get("access_token") or acc["access_token"]
            if tokens.get("refresh_token"):
                updated["refresh_token"] = tokens["refresh_token"]
            if tokens.get("id_token"):
                updated["id_token"] = tokens["id_token"]
            updated["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
            updated["scopes"] = scopes
            updated["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.store.put_account(updated, active=True)
            return updated["access_token"], updated

    # ---- inference ------------------------------------------------------
    def list_models(self):
        if not self.store.logged_in():
            return []
        try:
            token, _ = self.access_token()
            data = _json_request("GET", RESOURCE + "/models",
                                 {"Authorization": "Bearer " + token, "Accept": "application/json"}, None, 20)
        except (HTTPStatus, RuntimeError, urllib.error.URLError, OSError, socket.timeout):
            return []
        rows = data.get("models")
        if isinstance(rows, list):
            out = [[m.get("slug") or m.get("id"), m.get("display_name") or m.get("slug") or m.get("id")]
                   for m in rows if isinstance(m, dict) and m.get("visibility", "list") == "list" and (m.get("slug") or m.get("id"))]
            return out
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        return [[m["id"], m["id"]] for m in rows if isinstance(m, dict) and m.get("id")]

    def stream_turn(self, message, session_id, model, turn, timeout, attachments=None):
        """Yield ('delta', text). Return {reply, session_id, usage} or {error}."""
        if turn is not None and turn.cancel.is_set():
            raise streaming.Cancelled()
        try:
            token, _ = self.access_token()
        except RuntimeError as e:
            return {"error": str(e)}
        if not model:
            models = self.list_models()
            if not models:
                return {"error": "No ChatGPT models are available for this account. Sign in again, or try later."}
            model = models[0][0]
        history = self.store.session_messages(session_id)
        history.append({"role": "user", "content": user_content(message, attachments)})
        body = {"model": model, "input": history, "store": False, "stream": True}
        raw = json.dumps(body).encode()
        started = time.monotonic()
        try:
            status, chunks = _open_stream(RESOURCE + "/responses", token, raw, min(timeout, 180), turn)
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            if turn is not None and turn.cancel.is_set():
                raise streaming.Cancelled() from None
            return {"error": f"Could not reach ChatGPT ({e})."}
        if status == 401:
            # One refresh was already attempted inside access_token. Ask the user to sign in again.
            return {"error": "ChatGPT session expired. Open Accounts and choose Continue with ChatGPT."}
        if status >= 400:
            raw = "".join(chunks) if isinstance(chunks, list) else ""
            code, text = classify_error(raw)
            if code == "subscription_sharing_user_not_eligible":
                self.store.set_plan_usage(False)
            return {"error": text}
        parser = ResponsesParser()
        try:
            pending = ""
            for chunk in chunks:
                if turn is not None and turn.cancel.is_set():
                    raise streaming.Cancelled()
                if time.monotonic() - started > timeout:
                    raise streaming.TurnTimeout("total", timeout)
                pending += chunk
                while "\n\n" in pending:
                    raw_ev, pending = pending.split("\n\n", 1)
                    for kind, text in _feed_block(parser, raw_ev):
                        yield kind, text
            if pending.strip():
                for kind, text in _feed_block(parser, pending):
                    yield kind, text
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()
            if turn is not None:
                turn.resp = None
        result = parser.finish()
        if parser.code == "subscription_sharing_user_not_eligible":
            self.store.set_plan_usage(False)
            return {"error": PLAN_BLOCKED_MESSAGE}
        if result.get("reply") is None:
            return result
        history[-1] = {"role": "user", "content": message if isinstance(message, str) else message}
        # Store text only, so later turns don't resend image bytes.
        if not isinstance(history[-1]["content"], str):
            history[-1]["content"] = message if isinstance(message, str) else "See the attached files."
        history.append({"role": "assistant", "content": result["reply"]})
        if not session_id or not SESSION_ID.match(session_id):
            session_id = uuid.uuid4().hex
        self.store.save_session(session_id, history)
        result["session_id"] = session_id
        return result


def _shutdown(httpd):
    try:
        httpd.shutdown()
    except Exception:
        pass


def load_discovery():
    """Fill endpoints from OpenAI's published discovery document. Keep the defaults if it is unreachable."""
    try:
        data = _json_request("GET", ISSUER + "/.well-known/openid-configuration",
                             {"Accept": "application/json"}, None, 15)
    except (HTTPStatus, urllib.error.URLError, OSError, socket.timeout, ValueError):
        return
    for key, field in (("authorization", "authorization_endpoint"), ("token", "token_endpoint"),
                       ("jwks", "jwks_uri"), ("revocation", "revocation_endpoint"), ("issuer", "issuer")):
        if data.get(field):
            ENDPOINTS[key] = data[field]


def _record(tokens, payload, client_id, scopes):
    return {"email": payload.get("email") or "", "subject": payload["sub"], "client_id": client_id,
            "issuer": payload.get("iss") or ENDPOINTS["issuer"],
            "id_token": tokens.get("id_token") or "", "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or "",
            "expires_at": time.time() + int(tokens.get("expires_in") or 3600),
            "scopes": scopes, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def user_content(message, attachments):
    """Responses API input for the latest user turn. Text, plus images and PDFs it accepts."""
    attachments = attachments or []
    if not attachments:
        return message
    parts = [{"type": "input_text", "text": message or "See the attached files."}]
    for item in attachments:
        kind = item.get("kind")
        if kind == "image" and item.get("data_url"):
            parts.append({"type": "input_image", "image_url": item["data_url"]})
        elif kind == "pdf" and item.get("file_data"):
            parts.append({"type": "input_file", "filename": item.get("name") or "file.pdf",
                          "file_data": item["file_data"]})
        elif kind == "text" and item.get("text"):
            parts[0]["text"] += f"\n\nAttached file {item.get('name') or 'file'}:\n\n{item['text']}"
    return parts


def _feed_block(parser, raw):
    event, data = "message", []
    for line in raw.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if not data:
        return []
    return parser.feed(event, "\n".join(data))


def _open_stream(url, token, body, timeout, turn):
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, [e.read().decode(errors="replace")]
    if turn is not None:
        turn.resp = resp

    def chunks():
        try:
            while True:
                bit = resp.read(1024)
                if not bit:
                    break
                yield bit.decode("utf-8", "replace")
        finally:
            resp.close()

    chunks.close = resp.close
    return resp.status, chunks()


def _http_error_text(chunks):
    raw = "".join(chunks) if not isinstance(chunks, list) else "".join(chunks)
    return classify_error(raw)[1]


class _Login:
    def __init__(self):
        self.kind = "login"
        self.state = "running"
        self.url = None
        self.output = ""
        self.last = ""
        self.httpd = None

    def cancel(self):
        self.state = "cancelled"
        if self.httpd:
            threading.Thread(target=_shutdown, args=(self.httpd,), daemon=True).start()

    def fail(self, message):
        self.state = "failed"
        self.last = message
        self.output = message

    def blocked(self, message):
        self.state = "blocked"
        self.last = message
        self.output = message

    def timeout(self, message):
        self.state = "timeout"
        self.last = message
        self.output = message

    def succeed(self, message):
        self.state = "done"
        self.last = message
        self.output = message

    def view(self):
        return {"kind": self.kind, "state": self.state, "url": self.url if self.state == "running" else None,
                "output": (self.output or "")[-1500:], "last": (self.last or "")[:300],
                "asks_code": False, "user_code": None}


class _Callback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _page(self, text):
        body = ("<!doctype html><meta charset=utf-8><title>Chatbox</title><p>" + text + "</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        # Browsers request /favicon.ico (and sometimes the callback with no query) after the redirect.
        # Those must not replace a real code, and a code is single-use at the token endpoint.
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403)
            return
        qs = parse_oauth_query(parsed.query)
        log = getattr(self.server, "log", None)
        if "code" not in qs and "error" not in qs:
            if log:
                log("callback", ignored="no_code")
            self.send_response(204)
            self.end_headers()
            return
        with self.server.lock:
            if self.server.result is not None:
                if log:
                    log("callback", ignored="duplicate")
                self._page("You can close this tab and return to Chatbox.")
                return
            self.server.result = qs
        if qs.get("error"):
            self._page("You can close this tab and return to Chatbox.")
        else:
            self._page("Signed in. You can close this tab and return to Chatbox.")


# Filled in by chatbox once DATA_DIR is known.
SERVICE = None


def init(data_dir, open_url):
    global SERVICE
    SERVICE = Service(data_dir, open_url)
    return SERVICE


def status():
    return SERVICE.status()


def action(name):
    return SERVICE.action(name)


def logged_in():
    return SERVICE.logged_in() if SERVICE else False


def list_models():
    return SERVICE.list_models() if SERVICE else []


def stream_turn(message, session_id, model, turn, timeout, attachments=None):
    return SERVICE.stream_turn(message, session_id, model, turn, timeout, attachments)

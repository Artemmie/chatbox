"""Helpers for Chatbox tests. Keeps them off the real ~/.local/share/chatbox data."""
import os
import tempfile
import threading
import urllib.request

import chatbox


def isolate(tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    os.environ["CHATBOX_DATA"] = tmp
    chatbox.DATA_DIR = tmp
    chatbox.CHATS_DIR = os.path.join(tmp, "chats")
    chatbox.MEMORY = chatbox.memory.Store(tmp)
    chatbox.USAGE = chatbox.usagemod.Log(tmp)
    chatbox.OLLAMA = chatbox.ollama.Sessions(tmp)
    chatbox.chatgpt.init(tmp, lambda url: None)
    chatbox.ACCOUNTS._cache = None
    chatbox.ACCOUNTS._cache_time = 0
    chatbox._model_cache.clear()
    return chatbox, tmp


def serve(box):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), box.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def request(srv, path, body=None, host=None):
    port = srv.server_address[1]
    headers = {"Host": host or f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

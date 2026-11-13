"""Vault sync: Markdown round-trip, pickup from another computer, conflicts, no secrets."""
import json
import os
import tempfile
import textwrap
import time
import unittest

import sync

from tests.support import isolate


def vault_with_sync(box, name="vault"):
    root_parent = tempfile.mkdtemp()
    vault = os.path.join(root_parent, name)
    os.makedirs(vault)
    box.MEMORY.connect_vault(vault)
    box.MEMORY.update_settings({"sync_chats": True, "sync_folder": "Chatbox"})
    return sync.root_for(box.MEMORY)


class RoundTripTests(unittest.TestCase):
    def test_chat_markdown_round_trip(self):
        chat = {
            "id": "abc123abc123",
            "title": "Plan: sensors",
            "backend": "claude",
            "created": 1700000000000,
            "updated": 1700000001.5,
            "workdir": os.path.join("/tmp", "proj"),
            "sessions": {"claude": "sess-1", "cursor": None},
            "ctx": {"claude": "v1"},
            "sent": {"claude": ["Notes/Unity.md"]},
            "items": [
                {"r": "you", "t": "Hello\n\n## Claude\nnot a heading", "files": [
                    {"name": "pic.png", "kind": "image", "data": "SECRET-BYTES"}]},
                {"r": "bot", "t": "Here you go", "b": "claude", "mode": "ask", "model": "Sonnet",
                 "u": {"tok": 12, "s": 1.5}},
                {"r": "note", "t": "Saved to memory: Unity", "memId": "memmemmemmem"},
                {"r": "plan", "path": "/tmp/proj/plans/plan.md", "b": "claude", "t": ""},
            ],
        }
        text = sync.render_chat(chat)
        self.assertIn("## You", text)
        self.assertIn("## Claude", text)
        self.assertNotIn("SECRET-BYTES", text)
        parsed = sync.parse_chat(text)
        self.assertEqual(parsed["id"], chat["id"])
        self.assertEqual(parsed["title"], chat["title"])
        self.assertEqual(parsed["workdir"], chat["workdir"])
        self.assertEqual(parsed["sessions"]["claude"], "sess-1")
        self.assertEqual(parsed["sent"]["claude"], ["Notes/Unity.md"])
        self.assertEqual(parsed["items"][0]["t"], chat["items"][0]["t"])
        self.assertEqual(parsed["items"][0]["files"], [{"name": "pic.png", "kind": "image"}])
        self.assertEqual(parsed["items"][1]["model"], "Sonnet")
        self.assertEqual(parsed["items"][1]["u"]["tok"], 12)
        self.assertEqual(parsed["items"][2]["memId"], "memmemmemmem")
        self.assertEqual(parsed["items"][3]["path"], "/tmp/proj/plans/plan.md")

    def test_memory_round_trip(self):
        items = [{"id": "abc123abc123", "text": "I use Unity 6", "source": "you", "created": "2026-01-02"}]
        parsed = sync.parse_memories(sync.render_memories(items))
        self.assertEqual(parsed[0]["id"], "abc123abc123")
        self.assertEqual(parsed[0]["text"], "I use Unity 6")
        self.assertEqual(parsed[0]["source"], "you")

    def test_relative_folder_rejects_escape(self):
        self.assertEqual(sync.clean_rel("Chatbox"), "Chatbox")
        self.assertEqual(sync.clean_rel("Chatbox\\Notes"), "Chatbox/Notes")
        with self.assertRaises(ValueError):
            sync.clean_rel("../secrets")
        with self.assertRaises(ValueError):
            sync.clean_rel("/etc/passwd")


class SyncBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.box, self.data = isolate()
        self.root = vault_with_sync(self.box)
        # isolate() already created a library-less store; drop any cached library for this root
        sync._libraries.pop(os.path.abspath(self.root), None)

    def test_other_computer_is_picked_up(self):
        sync.save_chat(self.root, {"id": "aaa111aaa111", "title": "Local", "backend": "claude",
                                   "items": [{"r": "you", "t": "Hi"}]})
        other = {"id": "bbb222bbb222", "title": "From the Mac", "backend": "cursor",
                 "created": 10, "updated": time.time(),
                 "items": [{"r": "you", "t": "Hello from the other computer"}]}
        path = os.path.join(self.root, "Chats", "bbb222bbb222.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sync.render_chat(other))
        # A partial iCloud/Obsidian file must not show up or crash the scan.
        with open(os.path.join(self.root, "Chats", "partial.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nid: ccc333ccc333\n")
        open(os.path.join(self.root, "Chats", "empty.md"), "w").close()
        with open(os.path.join(self.root, "Chats", "note.md.icloud"), "w", encoding="utf-8") as handle:
            handle.write("placeholder")
        found = {row["id"] for row in sync.list_chats(self.root)}
        self.assertIn("aaa111aaa111", found)
        self.assertIn("bbb222bbb222", found)
        self.assertNotIn("ccc333ccc333", found)
        loaded = sync.load_chat(self.root, "bbb222bbb222")
        self.assertEqual(loaded["items"][0]["t"], "Hello from the other computer")

    def test_appended_messages_merge_and_edits_do_not_clobber(self):
        cid = "ddd444ddd444"
        original = {"id": cid, "title": "Shared", "backend": "claude", "workdir": "/work",
                    "items": [{"r": "you", "t": "Start"}]}
        sync.save_chat(self.root, original)
        path = sync.library(self.root).by_id[cid]["path"]
        remote = {"id": cid, "title": "Shared", "backend": "claude", "workdir": "/work",
                  "updated": time.time(),
                  "items": [{"r": "you", "t": "Start"},
                            {"r": "bot", "b": "claude", "t": "From the Mac"}]}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sync.render_chat(remote))
        os.utime(path, None)
        sync.save_chat(self.root, {"id": cid, "title": "Shared", "backend": "claude", "workdir": "/work",
                                   "items": [{"r": "you", "t": "Start"},
                                             {"r": "bot", "b": "cursor", "t": "From Linux"}]})
        merged = sync.load_chat(self.root, cid)
        texts = [item["t"] for item in merged["items"]]
        self.assertIn("From the Mac", texts)
        self.assertIn("From Linux", texts)
        # A real clash keeps the other computer's note and writes a conflict copy.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sync.render_chat({"id": cid, "title": "Shared", "backend": "claude",
                                           "workdir": "/work", "updated": time.time(),
                                           "items": [{"r": "you", "t": "Rewritten on the Mac"}]}))
        os.utime(path, None)
        before = open(path, encoding="utf-8").read()
        sync.save_chat(self.root, {"id": cid, "title": "Shared", "backend": "claude", "workdir": "/work",
                                   "items": [{"r": "you", "t": "Start"},
                                             {"r": "bot", "b": "cursor", "t": "From Linux"}]})
        self.assertEqual(open(path, encoding="utf-8").read(), before)
        names = os.listdir(os.path.join(self.root, "Chats"))
        self.assertTrue(any("(conflict " in name and name.endswith(".md") for name in names), names)

    def test_secrets_never_land_in_the_sync_folder(self):
        os.makedirs(os.path.join(self.data, "chatgpt"), exist_ok=True)
        token = "fixture-sync-token-value"
        with open(os.path.join(self.data, "chatgpt", "account.json"), "w", encoding="utf-8") as handle:
            json.dump({"access_token": token, "refresh_token": "fixture-refresh-value"}, handle)
        chat = {"id": "eee555eee555", "title": "Notes", "backend": "chatgpt",
                "access_token": token, "refresh_token": "fixture-refresh-value",
                "sessions": {"chatgpt": "sess", "refresh_token": "fixture-refresh-value"},
                "items": [{"r": "you", "t": "hello", "files": [
                    {"name": "a.txt", "kind": "text", "data": token}]}]}
        sync.save_chat(self.root, chat)
        sync.move_existing(self.data, self.root)
        blob = []
        for folder, _dirs, files in os.walk(self.root):
            for name in files:
                self.assertFalse(name.endswith("account.json"))
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    blob.append(handle.read())
        text = "\n".join(blob)
        self.assertNotIn(token, text)
        self.assertNotIn("fixture-refresh-value", text)
        self.assertNotIn("access_token", text)

    def test_move_copies_and_keeps_originals(self):
        os.makedirs(os.path.join(self.data, "chats"), exist_ok=True)
        with open(os.path.join(self.data, "chats", "fff666fff666.json"), "w", encoding="utf-8") as handle:
            json.dump({"id": "fff666fff666", "title": "Old", "backend": "claude",
                       "items": [{"r": "you", "t": "from disk"}]}, handle)
        with open(os.path.join(self.data, "memories.json"), "w", encoding="utf-8") as handle:
            json.dump([{"id": "abc123abc123", "text": "Remember this", "source": "you"}], handle)
        result = sync.move_existing(self.data, self.root)
        self.assertEqual(result["copied"], 1)
        self.assertGreaterEqual(result["memories"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "chats", "fff666fff666.json")))
        loaded = sync.load_chat(self.root, "fff666fff666")
        self.assertEqual(loaded["items"][0]["t"], "from disk")
        memories = sync.read_memories(self.root)
        self.assertTrue(any(item["text"] == "Remember this" for item in memories))

    def test_sync_off_stays_on_local_json(self):
        self.box.MEMORY.update_settings({"sync_chats": False})
        self.box.save_chat({"id": "abc123abc123", "title": "Local only", "backend": "claude",
                            "items": [{"r": "you", "t": "hi"}]})
        self.assertTrue(os.path.isfile(os.path.join(self.data, "chats", "abc123abc123.json")))
        chats = os.path.join(self.root, "Chats")
        self.assertFalse(os.path.isdir(chats))


if __name__ == "__main__":
    unittest.main()

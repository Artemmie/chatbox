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
                {"r": "you", "t": "Hello\n\n## Claude\nnot a heading\n%% chatbox {\"r\": \"bot\"} %%", "files": [
                    {"name": "pic.png", "kind": "image", "data": "SECRET-BYTES"}]},
                {"r": "bot", "t": "Here you go", "b": "claude", "mode": "ask", "model": "Sonnet",
                 "u": {"tok": 12, "s": 1.5}},
                {"r": "note", "t": "Saved to memory: Unity", "memId": "memmemmemmem"},
                {"r": "plan", "path": "/tmp/proj/plans/plan.md", "b": "claude", "t": ""},
            ],
        }
        text = sync.render_chat(chat)
        self.assertIn("### You", text)
        self.assertIn("### Claude · Sonnet", text)
        self.assertIn('up: "[[Chatbox]]"', text)
        self.assertIn("Part of [[Chatbox]]", text)
        self.assertIn("%% chatbox ", text)
        self.assertNotIn("<!--chatbox", text)
        self.assertNotIn("1700000000000", text)
        self.assertNotIn("1700000001.5", text)
        self.assertRegex(text, r"(?m)^created: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(text, r"(?m)^updated: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("SECRET-BYTES", text)
        parsed = sync.parse_chat(text)
        self.assertLess(abs(parsed["created"] / 1000.0 - 1700000000), 1)
        self.assertLess(abs(parsed["updated"] - 1700000001.5), 1)
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

    def test_old_note_format_still_parses(self):
        text = textwrap.dedent("""\
            ---
            id: "abc123abc123"
            title: "Plan: sensors"
            backend: "claude"
            created: 1700000000000
            updated: 1700000001.5
            ---

            # Plan: sensors

            ## You

            Hello

            ## Claude

            <!--chatbox {"b": "claude", "model": "Sonnet", "mode": "ask"}-->

            Here you go

            <!--chatbox-resume
            {"workdir": "/tmp/proj"}
            -->
            """)
        parsed = sync.parse_chat(text)
        self.assertEqual(parsed["created"], 1700000000000)
        self.assertEqual(parsed["updated"], 1700000001.5)
        self.assertEqual(parsed["items"][0]["t"], "Hello")
        self.assertEqual(parsed["items"][1]["b"], "claude")
        self.assertEqual(parsed["items"][1]["model"], "Sonnet")
        self.assertEqual(parsed["items"][1]["t"], "Here you go")
        self.assertEqual(parsed["workdir"], "/tmp/proj")

    def test_memory_round_trip(self):
        items = [{"id": "abc123abc123", "text": "I use Unity 6", "source": "you", "created": "2026-01-02"}]
        text = sync.render_memories(items)
        self.assertIn("Part of [[Chatbox]]", text)
        parsed = sync.parse_memories(text)
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


def chat_files(root):
    folder = os.path.join(root, "Chats")
    return sorted(name for name in os.listdir(folder) if name.endswith(".md"))


class ReadableNoteTests(unittest.TestCase):
    def setUp(self):
        self.box, self.data = isolate()
        self.root = vault_with_sync(self.box)
        sync._libraries.pop(os.path.abspath(self.root), None)

    def test_legacy_id_file_migrates_once(self):
        cid = "f9b36d59ddec"
        created = 1794576221436
        day = time.strftime("%Y-%m-%d", time.localtime(created / 1000.0))
        folder = os.path.join(self.root, "Chats")
        os.makedirs(folder)
        old = textwrap.dedent(f"""\
            ---
            id: "{cid}"
            title: "Sensor plan"
            backend: "claude"
            created: {created}
            updated: 1794576514.9195478
            ---

            # Sensor plan

            ## You

            Where should the shelter go?

            ## Claude

            <!--chatbox {{"b": "claude", "model": "Sonnet"}}-->

            On the north ridge.
            """)
        legacy = os.path.join(folder, cid + ".md")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write(old)
        loaded = sync.load_chat(self.root, cid)
        self.assertFalse(os.path.exists(legacy))
        names = chat_files(self.root)
        self.assertEqual(names, ["Sensor plan.md"])
        text = open(os.path.join(folder, names[0]), encoding="utf-8").read()
        self.assertNotIn("1794576221436", text)
        self.assertNotIn("1794576514", text)
        self.assertNotIn("<!--chatbox", text)
        self.assertIn("%% chatbox ", text)
        self.assertIn("### Claude · Sonnet", text)
        self.assertIn('up: "[[Chatbox]]"', text)
        self.assertIn("Part of [[Chatbox]]", text)
        self.assertRegex(text, r"(?m)^created: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(loaded["items"][0]["t"], "Where should the shelter go?")
        self.assertEqual(loaded["items"][1]["t"], "On the north ridge.")
        self.assertEqual(loaded["items"][1]["model"], "Sonnet")
        self.assertEqual(loaded["items"][1]["b"], "claude")
        note = open(os.path.join(self.root, "Chatbox.md"), encoding="utf-8").read()
        self.assertIn("[[Sensor plan]]", note)
        again = chat_files(self.root)
        sync.list_chats(self.root)
        self.assertEqual(chat_files(self.root), again)

    def test_title_rename_replaces_the_file(self):
        cid = "abc123abc123"
        created = 1700000000000
        chat = {"id": cid, "title": "First title", "backend": "claude", "created": created,
                "items": [{"r": "you", "t": "Hi"}]}
        sync.save_chat(self.root, chat)
        chat["title"] = "Second title"
        sync.save_chat(self.root, dict(chat))
        names = chat_files(self.root)
        self.assertEqual(len(names), 1)
        self.assertIn("Second title", names[0])
        self.assertNotIn("First title", names[0])
        self.assertEqual(sync.load_chat(self.root, cid)["title"], "Second title")
        note = open(os.path.join(self.root, "Chatbox.md"), encoding="utf-8").read()
        self.assertIn("Second title", note)
        self.assertNotIn("First title", note)

    def test_collision_adds_a_short_suffix(self):
        created = 1700000000000
        first = "aaa111aaa111"
        second = "bbb222bbb222"
        base = {"title": "Same title", "backend": "claude", "created": created,
                "items": [{"r": "you", "t": "Hi"}]}
        sync.save_chat(self.root, dict(base, id=first))
        sync.save_chat(self.root, dict(base, id=second, items=[{"r": "you", "t": "Other"}]))
        names = chat_files(self.root)
        self.assertEqual(len(names), 2)
        plain = [name for name in names if "(" not in name]
        suffixed = [name for name in names if "(" in name]
        self.assertEqual(plain, [name for name in names if name.endswith("Same title.md")])
        self.assertEqual(len(suffixed), 1)
        self.assertIn("(2)", suffixed[0])
        self.assertNotIn(first[:6], suffixed[0])
        self.assertNotIn(second[:6], suffixed[0])
        self.assertEqual(sync.load_chat(self.root, first)["items"][0]["t"], "Hi")
        self.assertEqual(sync.load_chat(self.root, second)["items"][0]["t"], "Other")

    def test_filename_strips_illegal_characters_and_trims(self):
        title = 'Map: "north" <ridge>/plan?' + (" word" * 30)
        sync.save_chat(self.root, {"id": "ccc333ccc333", "title": title, "backend": "claude",
                                   "created": 1700000000000,
                                   "items": [{"r": "you", "t": "Hi"}]})
        name = chat_files(self.root)[0]
        self.assertNotRegex(name, r'[\\/:*?"<>|]')
        self.assertLessEqual(len(os.path.splitext(name)[0]), 60)
        loaded = sync.load_chat(self.root, "ccc333ccc333")
        self.assertEqual(loaded["title"], title)

    def test_index_keeps_user_notes_and_follows_save_rename_delete(self):
        note_path = os.path.join(self.root, "Chatbox.md")
        os.makedirs(self.root, exist_ok=True)
        with open(note_path, "w", encoding="utf-8") as handle:
            handle.write("# Project\n\nShelter placement notes.\n\n<!-- chatbox:index -->\n"
                         "- [[Old]]\n<!-- /chatbox:index -->\n\nA line under the index.\n")
        older = {"id": "aaa111aaa111", "title": "Older chat", "backend": "claude",
                 "created": 1700000000000, "updated": 1700000000,
                 "items": [{"r": "you", "t": "old"}]}
        newer = {"id": "bbb222bbb222", "title": "Newer chat", "backend": "claude",
                 "created": 1700000000000, "updated": 1700001000,
                 "items": [{"r": "you", "t": "new"}]}
        sync.save_chat(self.root, older)
        sync.save_chat(self.root, newer)
        text = open(note_path, encoding="utf-8").read()
        self.assertIn("Shelter placement notes.", text)
        self.assertIn("A line under the index.", text)
        self.assertNotIn("[[Old]]", text)
        self.assertEqual(text.count("%% chatbox:index %%"), 1)
        self.assertEqual(text.count("%% /chatbox:index %%"), 1)
        self.assertLess(text.index("Shelter placement notes."), text.index("chatbox:index"))
        self.assertLess(text.index("/chatbox:index"), text.index("A line under the index."))
        links = [line for line in text.splitlines() if line.startswith("- [[")]
        self.assertEqual(links[0], "- [[Memories]]")
        self.assertIn("Newer chat", links[1])
        self.assertIn("Older chat", links[2])
        newer["title"] = "Renamed chat"
        sync.save_chat(self.root, newer)
        text = open(note_path, encoding="utf-8").read()
        self.assertIn("Renamed chat", text)
        self.assertNotIn("Newer chat", text)
        self.assertIn("Shelter placement notes.", text)
        self.assertIn("A line under the index.", text)
        sync.delete_chat(self.root, newer["id"])
        text = open(note_path, encoding="utf-8").read()
        self.assertNotIn("Renamed chat", text)
        self.assertIn("Older chat", text)
        self.assertIn("[[Memories]]", text)
        self.assertIn("Shelter placement notes.", text)
        self.assertIn("A line under the index.", text)
        self.assertEqual(len(chat_files(self.root)), 1)

    def test_memories_link_and_show_up_in_the_index(self):
        sync.write_memories(self.root, [{"id": "abc123abc123", "text": "I use Unity 6", "source": "you"}])
        memories = open(os.path.join(self.root, "Memories.md"), encoding="utf-8").read()
        self.assertIn("Part of [[Chatbox]]", memories)
        self.assertIn("I use Unity 6", memories)
        note = open(os.path.join(self.root, "Chatbox.md"), encoding="utf-8").read()
        self.assertIn("- [[Memories]]", note)
        self.assertIn("%% chatbox:index %%", note)


if __name__ == "__main__":
    unittest.main()

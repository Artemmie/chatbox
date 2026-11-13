"""Project-aware vault: filing, bots, backfill, and links that resolve."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

import sync
import vault

from tests.support import isolate, request, serve


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class MemoryLinkTests(unittest.TestCase):
    def test_memories_note_exists_before_it_is_linked(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "Chats"))
        _write(os.path.join(root, "Chats", "Hello.md"), "---\nid: \"abc123abc123\"\ntitle: \"Hello\"\n---\n")
        block = sync._index_block(root)
        self.assertIn("- [[Memories]]", block)
        self.assertTrue(os.path.isfile(os.path.join(root, "Memories.md")))
        self.assertGreater(os.path.getsize(os.path.join(root, "Memories.md")), 0)

    def test_missing_memories_note_is_not_linked(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "Memories.md"))
        block = sync._index_block(root)
        self.assertNotIn("[[Memories]]", block)


class BackfillTests(unittest.TestCase):
    def test_local_chats_export_once_into_the_project(self):
        box, data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        _write(os.path.join(root, "Projects", "Ocean Sweeper.md"),
               "# Ocean Sweeper\nBack to [[Projects]]\n\nA sweeper on a hex grid.\n")
        os.makedirs(os.path.join(data, "chats"))
        chat = {"id": "abc123abc123", "title": "Hull layout", "backend": "claude",
                "items": [{"r": "you", "t": "Plan the Ocean Sweeper hull"}]}
        _write(os.path.join(data, "chats", "abc123abc123.json"),
               json.dumps(chat))
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True})
        sync._libraries.clear()
        filed = os.path.join(root, "Projects", "Ocean Sweeper", "Chats", "Hull layout.md")
        self.assertTrue(os.path.isfile(filed), os.listdir(os.path.join(root, "Projects")))
        self.assertIn("Ocean Sweeper", _read(filed))
        again = sync.backfill_local(box.MEMORY)
        self.assertEqual(again["copied"], 0)
        self.assertEqual(again["moved"], 0)
        matches = []
        for dirpath, _dirs, files in os.walk(root):
            if ".chatbox-backup" in dirpath:
                continue
            for name in files:
                if name == "Hull layout.md":
                    matches.append(os.path.join(dirpath, name))
        self.assertEqual(len(matches), 1)

    def test_rehome_through_a_symlinked_vault(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        real = os.path.join(parent, "real")
        link = os.path.join(parent, "link")
        os.makedirs(real)
        os.symlink(real, link)
        box.MEMORY.connect_vault(link)
        box.MEMORY.update_settings({"sync_chats": True, "sync_folder": "Notes"})
        sync._libraries.clear()
        old = sync.root_for(box.MEMORY)
        sync.save_chat(old, {"id": "eee555eee555", "title": "Linked", "backend": "claude",
                             "items": [{"r": "you", "t": "through the link"}]})
        box.MEMORY.update_settings({"sync_folder": "Archive"})
        sync._libraries.clear()
        loaded = sync.load_chat(sync.root_for(box.MEMORY), "eee555eee555", vault_path=link)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["items"][0]["t"], "through the link")

    def test_old_sync_folder_is_rehomed_without_a_second_copy(self):
        box, data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True, "sync_folder": "Notes"})
        sync._libraries.clear()
        old = sync.root_for(box.MEMORY)
        sync.save_chat(old, {"id": "ddd444ddd444", "title": "Old export", "backend": "cursor",
                             "items": [{"r": "you", "t": "hello from the old folder"}]})
        self.assertTrue(any(name.endswith(".md") for name in os.listdir(os.path.join(old, "Chats"))))
        box.MEMORY.update_settings({"sync_folder": "Archive"})
        sync._libraries.clear()
        sync.backfill_local(box.MEMORY)
        loaded = sync.load_chat(sync.root_for(box.MEMORY), "ddd444ddd444",
                                vault_path=root)
        self.assertEqual(loaded["items"][0]["t"], "hello from the old folder")
        copies = []
        for dirpath, _dirs, files in os.walk(root):
            if ".chatbox-backup" in dirpath:
                continue
            for name in files:
                path = os.path.join(dirpath, name)
                if name.endswith(".md") and sync._note_id(path) == "ddd444ddd444":
                    copies.append(path)
        self.assertEqual(len(copies), 1)
        archive = os.path.normcase(os.path.realpath(os.path.join(root, "Archive")))
        self.assertTrue(os.path.normcase(os.path.realpath(copies[0])).startswith(archive + os.sep))
        sync.backfill_local(box.MEMORY)
        copies_again = [os.path.join(dirpath, name)
                        for dirpath, _dirs, files in os.walk(root)
                        if ".chatbox-backup" not in dirpath
                        for name in files
                        if sync._note_id(os.path.join(dirpath, name)) == "ddd444ddd444"]
        self.assertEqual(copies_again, copies)


class BotsTests(unittest.TestCase):
    def test_empty_vault_has_the_bots_hub_only(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True})
        hub = _read(os.path.join(root, "Bots", "Bots.md"))
        self.assertIn("Back to [[Home]]", hub)
        home = _read(os.path.join(root, "Home.md"))
        self.assertIn("[[Bots]]", home)
        self.assertIn("[[Projects]]", home)
        self.assertTrue(os.path.isfile(os.path.join(root, "Inbox.md")))
        self.assertFalse(os.path.exists(os.path.join(root, "Bots", "Claude.md")))
        box.save_chat({"id": "abc123abc123", "title": "Sensor plan", "backend": "claude",
                       "items": [{"r": "you", "t": "Where should the shelter go?"}]})
        note = _read(os.path.join(root, "Bots", "Claude.md"))
        self.assertIn("Recent chats", note)
        self.assertIn("[[Sensor plan]]", note)
        self.assertIn("%% chatbox:chats %%", note)
        self.assertFalse(os.path.exists(os.path.join(root, "Bots", "Codex.md")))
        box.save_chat({"id": "bbb222bbb222", "title": "Local try", "backend": "ollama",
                       "items": [{"r": "you", "t": "Hi"},
                                 {"r": "bot", "b": "ollama", "t": "Hello", "model": "llama3"}]})
        ollama = _read(os.path.join(root, "Bots", "Ollama.md"))
        self.assertIn("llama3", ollama)
        self.assertIn("[[Local try]]", ollama)


class PromoteTests(unittest.TestCase):
    def test_lone_note_promotes_when_a_chat_is_filed(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True})
        _write(os.path.join(root, "Projects", "Маяки.md"),
               "# Маяки\nBack to [[Projects]]\n\nUser wrote this.\n\nSee [[Projects/Маяки]].\n")
        _write(os.path.join(root, "Inbox.md"), "A path link: [[Projects/Маяки]]\n")
        box.save_chat({"id": "ccc333ccc333", "title": "Range", "backend": "claude",
                       "project": "Маяки", "project_lock": True,
                       "items": [{"r": "you", "t": "Place the Маяки"}]})
        hub = os.path.join(root, "Projects", "Маяки", "Маяки.md")
        self.assertTrue(os.path.isfile(hub))
        self.assertFalse(os.path.isfile(os.path.join(root, "Projects", "Маяки.md")))
        self.assertIn("User wrote this.", _read(hub))
        chat = os.path.join(root, "Projects", "Маяки", "Chats", "Range.md")
        self.assertTrue(os.path.isfile(chat))
        self.assertIn("Back to [[Маяки]]", _read(chat))
        self.assertIn("[[Projects/Маяки/Маяки]]", _read(os.path.join(root, "Inbox.md")))
        self.assertTrue(os.path.isdir(os.path.join(root, ".chatbox-backup")))
        other = os.path.join(root, "Projects", "Other.md")
        _write(other, "# Other\nBack to [[Projects]]\n\nNothing about the first project.\n")
        ocean = _read(hub) + _read(chat)
        self.assertNotIn("[[Other]]", ocean)


class LinkTests(unittest.TestCase):
    def test_rename_and_delete_update_links(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True, "sync_folder": "Projects/Chatbox"})
        box.save_chat({"id": "abc123abc123", "title": "Alpha", "backend": "claude",
                       "items": [{"r": "you", "t": "Start"}]})
        pointer = os.path.join(root, "Inbox.md")
        _write(pointer, "See [[Alpha]] for the plan.\n")
        box.save_chat({"id": "abc123abc123", "title": "Beta", "backend": "claude",
                       "project_lock": True, "project": "",
                       "items": [{"r": "you", "t": "Start"}]})
        text = _read(pointer)
        self.assertIn("[[Beta]]", text)
        self.assertNotIn("[[Alpha]]", text)
        box.delete_chat("abc123abc123")
        text = _read(pointer)
        self.assertNotIn("[[Beta]]", text)
        self.assertNotIn("[[Alpha]]", text)


class NoticeTests(unittest.TestCase):
    def test_missing_obsidian_is_a_notice(self):
        self.assertFalse(vault.obsidian_installed(
            system="linux", home=tempfile.mkdtemp(), environ={},
            which=lambda _name: None, exists=lambda _path: False))
        home = tempfile.mkdtemp()
        app = os.path.join(home, ".local", "share", "flatpak", "app", "md.obsidian.Obsidian")
        self.assertTrue(vault.obsidian_installed(
            system="linux", home=home, environ={}, which=lambda _name: None,
            exists=lambda path: path == app))
        notice = vault.connection_notice("", False, False)
        self.assertEqual(notice["notice"], "Obsidian not found.")
        self.assertEqual(notice["download"], "https://obsidian.md/download")
        box, data = isolate()
        box.MEMORY.update_settings({"sync_chats": True, "vault_path": os.path.join(data, "missing")})
        payload = box.settings_payload()
        self.assertFalse(payload["obsidian_app"]["connected"])
        self.assertTrue(payload["obsidian_app"]["notice"])
        box.save_chat({"id": "abc123abc123", "title": "Kept", "backend": "claude",
                       "items": [{"r": "you", "t": "hi"}]})
        loaded = box.load_chat("abc123abc123")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["title"], "Kept")
        place = box.settings_payload()["notes"]
        self.assertTrue(place["fallback"])
        self.assertIn("doesn't exist", place["status"])
        self.assertNotIn(os.path.join(data, "missing"), place["status"])

    def test_version_is_read_not_hardcoded(self):
        path = os.path.join(os.path.dirname(__file__), "..", "packaging", "build.py")
        spec = importlib.util.spec_from_file_location("pack_build_version_env", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        previous = os.environ.get("CHATBOX_VERSION")
        os.environ["CHATBOX_VERSION"] = "read-from-env"
        try:
            self.assertEqual(mod.version(), "read-from-env")
        finally:
            if previous is None:
                os.environ.pop("CHATBOX_VERSION", None)
            else:
                os.environ["CHATBOX_VERSION"] = previous


class MapTests(unittest.TestCase):
    def test_search_prefers_the_project_and_map_stays_within_budget(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        _write(os.path.join(root, "Projects", "Projects.md"),
               "# Projects\nBack to [[Home]]\n\n- [[Ocean Sweeper]] — hex sweeper\n")
        _write(os.path.join(root, "Projects", "Ocean Sweeper", "Ocean Sweeper.md"),
               "# Ocean Sweeper\nBack to [[Projects]]\n\nHex sweeper.\n")
        _write(os.path.join(root, "Projects", "Ocean Sweeper", "Decisions.md"),
               "# Decisions\n\nSensor sensor shelter placement.\n")
        _write(os.path.join(root, "Studies", "Sensors.md"),
               "# Sensors\n\n" + ("sensor shelter placement " * 30) + "\n")
        lib = vault.Vault(root)
        hits = lib.search("sensor shelter placement", project="Ocean Sweeper")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "Decisions")
        self.assertIn("Projects > Ocean Sweeper > Decisions", hits[0]["breadcrumb"])
        text = lib.map_text(budget=500)
        self.assertLessEqual(len(text), 500)
        self.assertIn("Ocean Sweeper", lib.map_text())


class SidebarProjectTests(unittest.TestCase):
    """Projects in the sidebar: create, move a note, rename, and delete without losing notes."""

    def _vault(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        root = os.path.join(parent, "vault")
        os.makedirs(root)
        box.MEMORY.connect_vault(root)
        box.MEMORY.update_settings({"sync_chats": True, "notes_storage": "obsidian"})
        sync._libraries.clear()
        return box, root

    def _live(self, root, cid):
        return sync.find_chat_notes(root).get(cid, [])

    def test_moving_a_chat_moves_the_vault_note(self):
        box, root = self._vault()
        chat = {"id": "aaa111aaa111", "title": "Move me", "backend": "claude",
                "project": "Alpha", "project_lock": True,
                "items": [{"r": "you", "t": "Keep this sentence."}]}
        box.save_chat(dict(chat))
        first = self._live(root, chat["id"])
        self.assertEqual(len(first), 1)
        self.assertIn(os.path.join("Alpha", "Chats"), first[0])
        chat["project"] = "Beta"
        box.save_chat(dict(chat))
        notes = self._live(root, chat["id"])
        self.assertEqual(len(notes), 1)
        self.assertIn(os.path.join("Beta", "Chats"), notes[0])
        self.assertFalse(os.path.exists(first[0]))
        self.assertIn("Keep this sentence.", _read(notes[0]))
        loaded = box.load_chat(chat["id"])
        self.assertEqual(loaded["project"], "Beta")
        self.assertTrue(loaded["project_lock"])

    def test_rename_keeps_notes_and_delete_only_unlinks_chats(self):
        box, root = self._vault()
        box.create_project("Ocean")
        lone = os.path.join(root, "Projects", "Ocean.md")
        with open(lone, "a", encoding="utf-8") as handle:
            handle.write("\nUser wrote this.\n")
        box.save_chat({"id": "ccc333ccc333", "title": "Range", "backend": "claude",
                       "project": "Ocean", "project_lock": True,
                       "items": [{"r": "you", "t": "Place the shelter"}]})
        hub = os.path.join(root, "Projects", "Ocean", "Ocean.md")
        self.assertIn("User wrote this.", _read(hub))
        map_note = os.path.join(root, "Projects", "Ocean", "Map.md")
        original = "Keep this sensor map.\n"
        _write(map_note, original)
        with open(os.path.join(root, "Inbox.md"), "a", encoding="utf-8") as handle:
            handle.write("\nSee [[Ocean]] and [[Projects/Ocean/Ocean]].\n")
        box.rename_project("Ocean", "Harbor")
        self.assertFalse(os.path.exists(os.path.join(root, "Projects", "Ocean")))
        self.assertFalse(os.path.exists(lone))
        hub = os.path.join(root, "Projects", "Harbor", "Harbor.md")
        self.assertIn("User wrote this.", _read(hub))
        self.assertIn("# Harbor", _read(hub))
        moved_map = os.path.join(root, "Projects", "Harbor", "Map.md")
        self.assertEqual(_read(moved_map), original)
        inbox = _read(os.path.join(root, "Inbox.md"))
        self.assertIn("[[Harbor]]", inbox)
        self.assertIn("[[Projects/Harbor/Harbor]]", inbox)
        self.assertNotIn("[[Ocean]]", inbox)
        loaded = box.load_chat("ccc333ccc333")
        self.assertEqual(loaded["project"], "Harbor")
        self.assertIn("Place the shelter", loaded["items"][0]["t"])
        self.assertEqual(len(self._live(root, "ccc333ccc333")), 1)
        names = [item["name"] for item in vault.Vault(root).map()["projects"]]
        self.assertIn("Harbor", names)
        self.assertNotIn("Ocean", names)
        box.delete_project("Harbor")
        self.assertEqual(_read(moved_map), original)
        self.assertTrue(os.path.isfile(hub))
        self.assertIn("User wrote this.", _read(hub))
        self.assertTrue(os.path.isfile(os.path.join(root, "Projects", "Harbor", ".chatbox-retired")))
        loaded = box.load_chat("ccc333ccc333")
        self.assertEqual(loaded["project"], "")
        self.assertIn("Place the shelter", loaded["items"][0]["t"])
        notes = self._live(root, "ccc333ccc333")
        self.assertEqual(len(notes), 1)
        self.assertNotIn(os.path.join("Harbor", "Chats"), notes[0])
        names = [item["name"] for item in vault.Vault(root).map()["projects"]]
        self.assertNotIn("Harbor", names)
        box.create_project("Harbor")
        names = [item["name"] for item in vault.Vault(root).map()["projects"]]
        self.assertIn("Harbor", names)
        self.assertFalse(os.path.isfile(os.path.join(root, "Projects", "Harbor", ".chatbox-retired")))
        self.assertIn("User wrote this.", _read(hub))

    def test_delete_of_a_lone_note_keeps_the_note(self):
        box, root = self._vault()
        box.create_project("Sketch")
        path = os.path.join(root, "Projects", "Sketch.md")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\nKeep this project note.\n")
        box.delete_project("Sketch")
        text = _read(path)
        self.assertIn("Keep this project note.", text)
        self.assertNotIn("Sketch", [item["name"] for item in vault.Vault(root).map()["projects"]])

    def test_sync_folder_and_bad_names_are_refused(self):
        box, root = self._vault()
        self.assertTrue(os.path.isdir(os.path.join(root, "Projects", "Chatbox")))
        with self.assertRaises(ValueError):
            box.rename_project("Chatbox", "Other")
        self.assertTrue(os.path.isdir(os.path.join(root, "Projects", "Chatbox")))
        with self.assertRaises(ValueError):
            box.delete_project("Chatbox")
        with self.assertRaises(ValueError):
            box.create_project("../etc")
        box.create_project("Alpha")
        box.create_project("Beta")
        with self.assertRaises(ValueError):
            box.rename_project("Alpha", "Beta")
        self.assertTrue(os.path.exists(os.path.join(root, "Projects", "Alpha.md"))
                        or os.path.isdir(os.path.join(root, "Projects", "Alpha")))

    def test_notes_off_cannot_add_a_project(self):
        box, _data = isolate()
        box.MEMORY.set_notes_storage("off")
        with self.assertRaises(ValueError):
            box.create_project("Nope")

    def test_project_api_returns_the_list(self):
        box, _root = self._vault()
        srv = serve(box)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        code, raw = request(srv, "/api/projects", {"action": "create", "name": "From API"})
        self.assertEqual(code, 200)
        body = json.loads(raw)
        self.assertNotIn("error", body)
        self.assertTrue(any(item["name"] == "From API" for item in body["projects"]))
        code, raw = request(srv, "/api/projects", {"action": "nope"})
        self.assertIn("error", json.loads(raw))


if __name__ == "__main__":
    unittest.main()

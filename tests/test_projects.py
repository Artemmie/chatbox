"""Project-aware vault: filing, bots, backfill, and links that resolve."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

import sync
import vault

from tests.support import isolate


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
        self.assertTrue(os.path.isfile(os.path.join(data, "chats", "abc123abc123.json")))

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


if __name__ == "__main__":
    unittest.main()

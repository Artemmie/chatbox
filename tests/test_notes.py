"""Notes storage: Obsidian vault, Chatbox's own folder, or off."""
import os
import shutil
import tempfile
import unittest

import memory
import vault

from tests.support import isolate


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class NotesStorageTests(unittest.TestCase):
    def setUp(self):
        self._installed = vault.obsidian_installed
        vault.obsidian_installed = lambda *args, **kwargs: False

    def tearDown(self):
        vault.obsidian_installed = self._installed

    def test_first_run_without_obsidian_uses_the_chatbox_folder(self):
        box, data = isolate()
        payload = box.settings_payload()
        self.assertEqual(payload["notes"]["mode"], "chatbox")
        self.assertTrue(payload["notes"]["banner"])
        self.assertTrue(payload["notes"]["local_path"].endswith(os.path.join("vault") if False else "vault"))
        self.assertEqual(os.path.basename(payload["notes"]["local_path"]), "vault")
        box.save_chat({"id": "abc123abc123", "title": "Shelter", "backend": "claude",
                       "items": [{"r": "you", "t": "Where should the shelter go?"}]})
        vault_dir = os.path.join(data, "vault")
        self.assertTrue(os.path.isfile(os.path.join(vault_dir, "Home.md")))
        self.assertTrue(os.path.isfile(os.path.join(vault_dir, "Projects", "Projects.md")))
        found = []
        for dirpath, _dirs, files in os.walk(vault_dir):
            if "Shelter.md" in files:
                found.append(os.path.join(dirpath, "Shelter.md"))
        self.assertEqual(len(found), 1)
        note = box.MEMORY.vault()
        _write(os.path.join(vault_dir, "Projects", "Ocean Sweeper.md"),
               "# Ocean Sweeper\nBack to [[Projects]]\n\n" + ("hex grid sweeper " * 4) + "\n")
        hits = note.search("hex grid sweeper")
        self.assertTrue(any("Ocean Sweeper" in h["title"] or "Ocean" in h["title"] for h in hits), hits)
        message, _notes = box._prepare_message({"backend": "chatgpt", "message": "hex grid sweeper", "mode": "ask"})
        self.assertIn("Ocean Sweeper", message)

    def test_off_keeps_chats_out_of_the_notes_folder(self):
        box, data = isolate()
        box.MEMORY.set_notes_storage("off")
        box.save_chat({"id": "abc123abc123", "title": "Local", "backend": "claude",
                       "items": [{"r": "you", "t": "hi"}]})
        self.assertTrue(os.path.isfile(os.path.join(data, "chats", "abc123abc123.json")))
        self.assertFalse(os.path.isdir(os.path.join(data, "vault", "Projects")))

    def test_obsidian_installed_does_not_force_the_chatbox_folder(self):
        vault.obsidian_installed = lambda *args, **kwargs: True
        box, data = isolate()
        self.assertEqual(box.MEMORY.ensure_notes_choice(), "off")
        self.assertFalse(os.path.isdir(os.path.join(data, "vault")))
        payload = box.settings_payload()
        self.assertEqual(payload["notes"]["mode"], "off")
        self.assertFalse(payload["notes"]["banner"])

    def test_copy_into_a_vault_skips_duplicates(self):
        box, data = isolate()
        box.MEMORY.ensure_notes_choice()
        src = os.path.join(data, "vault")
        _write(os.path.join(src, "Projects", "Chatbox", "Chats", "Hello.md"),
               "---\nid: \"abc123abc123\"\ntitle: \"Hello\"\n---\n\n# Hello\n\nFrom Chatbox.\n")
        _write(os.path.join(src, "Inbox.md"), "# Inbox\nBack to [[Home]]\n\nAlready here.\n")
        _write(os.path.join(src, "Studies", "New.md"), "# New\n\nOnly in Chatbox.\n")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, True)
        kept = os.path.join(dest, "Projects", "Chatbox", "Chats", "Hello.md")
        _write(kept, "---\nid: \"abc123abc123\"\ntitle: \"Hello\"\n---\n\n# Hello\n\nKept.\n")
        _write(os.path.join(dest, "Inbox.md"), "# Inbox\n\nUser text.\n")
        result = box.MEMORY.set_notes_storage("obsidian", dest, copy=True)
        self.assertGreater(result["copied"], 0)
        self.assertEqual(result["mode"], "obsidian")
        hello = open(kept, encoding="utf-8").read()
        self.assertIn("Kept.", hello)
        self.assertNotIn("From Chatbox.", hello)
        inbox = open(os.path.join(dest, "Inbox.md"), encoding="utf-8").read()
        self.assertIn("User text.", inbox)
        self.assertTrue(os.path.isfile(os.path.join(dest, "Studies", "New.md")))
        again = memory.copy_notes(src, dest)
        self.assertEqual(again, 0)

    def test_settings_page_lists_the_three_choices(self):
        text = open(os.path.join(os.path.dirname(__file__), "..", "ui", "index.html"), encoding="utf-8").read()
        self.assertLess(text.index("Use my Obsidian vault"), text.index("Keep notes inside Chatbox"))
        self.assertLess(text.index("Keep notes inside Chatbox"), text.index("> Off<"))
        self.assertIn("Recommended", text)
        self.assertIn("https://obsidian.md/download", text)
        self.assertIn("Open in Obsidian later", text)
        self.assertIn("Choose vault folder", text)


if __name__ == "__main__":
    unittest.main()

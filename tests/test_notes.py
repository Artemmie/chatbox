"""Notes storage: Obsidian vault, Chatbox's own folder, or off."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import folderpick
import memory
import vault

from tests.support import isolate, request, serve


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
        root = os.path.join(os.path.dirname(__file__), "..", "ui")
        text = open(os.path.join(root, "index.html"), encoding="utf-8").read()
        self.assertLess(text.index("Obsidian vault"), text.index("Inside Chatbox"))
        self.assertLess(text.index("Inside Chatbox"), text.index(">Off<"))
        self.assertIn("Recommended", text)
        self.assertIn("https://obsidian.md/download", text)
        self.assertIn("Open in Obsidian later", text)
        self.assertIn("Change…", text)
        self.assertIn('id="vaultpickpath"', text)
        self.assertIn("No vault chosen", text)
        self.assertIn("Advanced", text)
        self.assertIn("Open folder", text)
        script = open(os.path.join(root, "js", "app.js"), encoding="utf-8").read()
        self.assertIn("$('#vaultpickpath').value=p.settings.vault_path||''", script)

    def test_obsidian_mode_uses_vault_path_not_the_chatbox_folder(self):
        import sync
        box, data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        vault_dir = os.path.join(parent, "Main vault")
        os.makedirs(vault_dir)
        box.MEMORY.update_settings({
            "notes_storage": "obsidian",
            "vault_path": vault_dir,
            "sync_chats": True,
            "sync_folder": "Projects/Chatbox",
        })
        root = sync.root_for(box.MEMORY)
        self.assertEqual(os.path.realpath(root), os.path.realpath(os.path.join(vault_dir, "Projects", "Chatbox")))
        internal = os.path.realpath(os.path.join(data, "vault"))
        self.assertFalse(os.path.realpath(root).startswith(internal + os.sep) or os.path.realpath(root) == internal)
        box.save_chat({"id": "abc123abc123", "title": "Shelter", "backend": "claude",
                       "items": [{"r": "you", "t": "Where should the shelter go?"}]})
        found = []
        for dirpath, _dirs, files in os.walk(vault_dir):
            if "Shelter.md" in files:
                found.append(os.path.join(dirpath, "Shelter.md"))
        self.assertEqual(len(found), 1)
        self.assertFalse(os.path.isdir(os.path.join(data, "vault", "Projects")))
        payload = box.settings_payload()
        self.assertEqual(payload["settings"]["vault_path"], os.path.abspath(vault_dir))
        self.assertIn("✓ Saving chats to Main vault › Projects › Chatbox", payload["notes"]["status"])
        self.assertNotIn(vault_dir, payload["notes"]["status"])
        self.assertFalse(payload["notes"]["fallback"])

    def test_chatbox_mode_ignores_a_saved_vault_path(self):
        import sync
        box, data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        vault_dir = os.path.join(parent, "Main vault")
        os.makedirs(vault_dir)
        box.MEMORY.update_settings({
            "notes_storage": "chatbox",
            "vault_path": vault_dir,
            "sync_chats": True,
            "sync_folder": "Projects/Chatbox",
        })
        root = sync.root_for(box.MEMORY)
        internal = os.path.realpath(os.path.join(data, "vault"))
        self.assertTrue(os.path.realpath(root).startswith(internal + os.sep) or os.path.realpath(root) == internal)
        self.assertFalse(os.path.realpath(root).startswith(os.path.realpath(vault_dir)))
        status = box.MEMORY.notes_status()["line"]
        self.assertIn("✓ Saving chats to Chatbox › Projects › Chatbox", status)
        self.assertNotIn("Main vault", status)
        self.assertNotIn(vault_dir, status)

    def test_obsidian_without_a_real_folder_falls_back_and_says_so(self):
        import sync
        box, data = isolate()
        missing = os.path.join(data, "missing")
        box.MEMORY.update_settings({
            "notes_storage": "obsidian",
            "vault_path": missing,
            "sync_chats": True,
            "sync_folder": "Projects/Chatbox",
        })
        root = sync.root_for(box.MEMORY)
        internal = os.path.realpath(os.path.join(data, "vault"))
        self.assertTrue(os.path.realpath(root).startswith(internal + os.sep))
        place = box.settings_payload()["notes"]
        self.assertTrue(place["fallback"])
        self.assertIn("doesn't exist", place["status"])
        self.assertIn("Chatbox › Projects › Chatbox", place["status"])
        self.assertNotIn(missing, place["status"])
        self.assertFalse(box.settings_payload()["obsidian_app"]["connected"])

    def test_changing_the_sync_folder_moves_a_chat_once(self):
        box, _data = isolate()
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        vault_dir = os.path.join(parent, "Main vault")
        os.makedirs(vault_dir)
        box.MEMORY.update_settings({
            "notes_storage": "obsidian",
            "vault_path": vault_dir,
            "sync_chats": True,
            "sync_folder": "Notes",
        })
        box.save_chat({"id": "abc123abc123", "title": "Once", "backend": "claude",
                       "items": [{"r": "you", "t": "hi"}]})
        box.MEMORY.update_settings({"sync_folder": "Archive"})
        box.MEMORY.update_settings({"sync_folder": "Archive"})
        found = []
        for dirpath, _dirs, files in os.walk(vault_dir):
            if ".chatbox-backup" in dirpath.split(os.sep):
                continue
            if "Once.md" in files:
                found.append(os.path.join(dirpath, "Once.md"))
        self.assertEqual(len(found), 1)
        self.assertIn(os.path.join("Archive", "Chats"), found[0])

    def test_typed_path_that_is_missing_is_rejected(self):
        box, data = isolate()
        kept = os.path.join(data, "kept")
        os.makedirs(kept)
        box.MEMORY.set_notes_storage("obsidian", kept)
        try:
            box.MEMORY.set_notes_storage("obsidian", os.path.join(data, "nope"))
        except ValueError as exc:
            self.assertIn("doesn't exist", str(exc))
        else:
            self.fail("missing folder was accepted")
        self.assertEqual(box.MEMORY.settings()["vault_path"], os.path.abspath(kept))


class PickFolderTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        self.srv = serve(self.box)
        self._bridge = folderpick._bridge
        folderpick.set_bridge(None)

    def tearDown(self):
        folderpick.set_bridge(self._bridge)
        self.srv.shutdown()
        self.srv.server_close()

    def test_endpoint_returns_the_mocked_dialog_and_a_cancel(self):
        seen = {}

        def fake(initial="", timeout=None):
            seen["initial"] = initial
            seen["timeout"] = timeout
            return {"path": os.path.join(self.tmp, "picked"), "cancelled": False}

        saved = os.path.join(self.tmp, "Main vault")
        self.box.MEMORY.update_settings({"vault_path": saved, "notes_storage": "obsidian"}, _prepare=False)
        original = folderpick.pick
        folderpick.pick = fake
        try:
            code, raw = request(self.srv, "/api/pick-folder", {})
            self.assertEqual(code, 200)
            body = json.loads(raw)
            self.assertEqual(body["path"], os.path.join(self.tmp, "picked"))
            self.assertFalse(body["cancelled"])
            self.assertEqual(seen["initial"], saved)

            def cancelled(initial="", timeout=None):
                return {"path": "", "cancelled": True}

            folderpick.pick = cancelled
            code, raw = request(self.srv, "/api/pick-folder", {})
            body = json.loads(raw)
            self.assertTrue(body["cancelled"])
            self.assertEqual(body["path"], "")
        finally:
            folderpick.pick = original

    def test_bridge_is_preferred_and_a_timeout_is_reported(self):
        seen = {}

        def bridge(initial, timeout):
            seen["initial"] = initial
            seen["timeout"] = timeout
            return {"path": "/from/qt", "cancelled": False}

        folderpick.set_bridge(bridge)
        result = folderpick.pick("/Users/art/Documents/Main vault")
        self.assertEqual(result["path"], "/from/qt")
        self.assertFalse(result["cancelled"])
        self.assertEqual(seen["initial"], "/Users/art/Documents/Main vault")
        self.assertEqual(seen["timeout"], folderpick.PICK_TIMEOUT)

        def boom(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="zenity", timeout=1)

        result = folderpick._from_argv(["zenity"], 1, run=boom)
        self.assertIn("timed out", result["error"].lower())
        self.assertFalse(result["cancelled"])

    def test_linux_prefers_zenity_and_cancel_is_empty(self):
        calls = []

        def which(name):
            return "/usr/bin/" + name if name == "zenity" else None

        class Proc:
            returncode = 1
            stdout = ""
            stderr = ""

        def run(argv, **_kwargs):
            calls.append(argv)
            return Proc()

        result = folderpick._linux("/tmp", 5, which=which, run=run)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["path"], "")
        self.assertEqual(calls[0][0], "zenity")
        self.assertIn("--directory", calls[0])

        calls.clear()

        def which_kde(name):
            return "/usr/bin/kdialog" if name == "kdialog" else None

        folderpick._linux("/tmp", 5, which=which_kde, run=run)
        self.assertEqual(calls[0][0], "kdialog")
        self.assertIn("--getexistingdirectory", calls[0])

        start = self.tmp

        class Cancel:
            returncode = 0
            stdout = ""
            stderr = ""

        osa = []

        def osa_run(argv, **_kwargs):
            osa.append(list(argv))
            return Cancel()

        result = folderpick._darwin(start, 5, run=osa_run)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(osa), 1)
        self.assertEqual(osa[0][0], "osascript")
        self.assertIn(os.path.abspath(start), osa[0])

        class Ok:
            returncode = 0
            stdout = "/home/me/Main vault\n"
            stderr = ""

        result = folderpick._linux("/tmp", 5, which=which, run=lambda argv, **_kwargs: Ok())
        self.assertEqual(result["path"], "/home/me/Main vault")
        self.assertFalse(result["cancelled"])

    def test_save_endpoint_prefills_from_settings_and_rejects_a_missing_folder(self):
        saved = os.path.join(self.tmp, "Main vault")
        os.makedirs(saved)
        self.box.MEMORY.update_settings({
            "notes_storage": "obsidian", "vault_path": saved, "sync_chats": True,
            "sync_folder": "Projects/Chatbox",
        })
        code, raw = request(self.srv, "/api/settings")
        self.assertEqual(code, 200)
        body = json.loads(raw)
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(saved))
        self.assertIn("Main vault › Projects › Chatbox", body["notes"]["status"])
        code, raw = request(self.srv, "/api/notes", {"mode": "obsidian", "path": os.path.join(self.tmp, "nope")})
        body = json.loads(raw)
        self.assertIn("doesn't exist", body["error"])
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(saved))

    def test_saving_the_same_vault_reports_it_and_a_file_is_rejected(self):
        saved = os.path.join(self.tmp, "Main vault")
        os.makedirs(saved)
        self.box.MEMORY.set_notes_storage("obsidian", saved)
        code, raw = request(self.srv, "/api/notes", {"mode": "obsidian", "path": saved + os.sep})
        self.assertEqual(code, 200)
        body = json.loads(raw)
        self.assertTrue(body["unchanged"])
        self.assertEqual(body["notice"], "Already using this vault")
        self.assertEqual(body["vault_name"], "Main vault")
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(saved))
        other = os.path.join(self.tmp, "Other vault")
        os.makedirs(other)
        code, raw = request(self.srv, "/api/notes", {"mode": "obsidian", "path": other})
        body = json.loads(raw)
        self.assertFalse(body["unchanged"])
        self.assertEqual(body["notice"], "Saved: notes go to Other vault")
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(other))
        note = os.path.join(self.tmp, "not-a-folder.md")
        with open(note, "w", encoding="utf-8") as handle:
            handle.write("# no\n")
        code, raw = request(self.srv, "/api/notes", {"mode": "obsidian", "path": note})
        body = json.loads(raw)
        self.assertIn("isn't a folder", body["error"])
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(other))
        missing = os.path.join(self.tmp, "gone")
        code, raw = request(self.srv, "/api/notes", {"mode": "obsidian", "path": missing})
        body = json.loads(raw)
        self.assertIn("doesn't exist", body["error"])
        self.assertEqual(body["settings"]["vault_path"], os.path.abspath(other))


class NotesCardTests(unittest.TestCase):
    def setUp(self):
        self._installed = vault.obsidian_installed
        vault.obsidian_installed = lambda *args, **kwargs: False
        self.box, self.data = isolate()

    def tearDown(self):
        vault.obsidian_installed = self._installed

    def test_chatbox_mode_is_not_shown_as_an_obsidian_vault(self):
        self.box.MEMORY.update_settings({"notes_storage": "chatbox", "vault_path": "", "sync_chats": True})
        payload = self.box.settings_payload()
        card = payload["notes"]["card"]
        noun = "note" if card["count"] == 1 else "notes"
        self.assertEqual(card["title"], f"Notes: Inside Chatbox · {card['count']} {noun}")
        self.assertGreater(card["count"], 0)
        self.assertFalse(card["disconnect"])
        self.assertTrue(card["open_folder"])
        self.assertFalse(card["open_obsidian"])
        self.assertFalse(card["show_save"])
        self.assertNotIn("Connected", card["title"])
        self.assertEqual(os.path.realpath(card["path"]), os.path.realpath(os.path.join(self.data, "vault")))
        self.assertEqual(payload["settings"]["vault_path"], "")

    def test_save_button_shows_only_when_auto_sync_is_off(self):
        self.box.MEMORY.update_settings({"notes_storage": "chatbox", "sync_chats": True})
        self.assertFalse(self.box.settings_payload()["notes"]["card"]["show_save"])
        self.box.MEMORY.update_settings({"sync_chats": False}, _prepare=False)
        card = self.box.settings_payload()["notes"]["card"]
        self.assertTrue(card["show_save"])
        self.assertTrue(card["title"].startswith("Notes: Inside Chatbox"))
        self.box.MEMORY.set_notes_storage("off")
        self.assertFalse(self.box.settings_payload()["notes"]["card"]["show_save"])

    def test_obsidian_card_names_the_vault(self):
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        vault_dir = os.path.join(parent, "Main vault")
        os.makedirs(vault_dir)
        self.box.MEMORY.set_notes_storage("obsidian", vault_dir)
        card = self.box.settings_payload()["notes"]["card"]
        self.assertTrue(card["title"].startswith("Main vault · "))
        self.assertTrue(card["disconnect"])
        self.assertFalse(card["show_save"])
        self.assertEqual(os.path.realpath(card["path"]), os.path.realpath(vault_dir))
        self.assertFalse(card["title"].startswith("Notes: Inside Chatbox"))

    def test_open_in_obsidian_follows_the_install_check(self):
        self.box.MEMORY.update_settings({"notes_storage": "chatbox", "sync_chats": True})
        self.assertFalse(self.box.settings_payload()["notes"]["card"]["open_obsidian"])
        vault.obsidian_installed = lambda *args, **kwargs: True
        self.assertTrue(self.box.settings_payload()["notes"]["card"]["open_obsidian"])

    def test_manual_save_updates_the_same_note(self):
        import sync
        self.box.MEMORY.set_notes_storage("chatbox")
        self.box.MEMORY.update_settings({"sync_chats": False}, _prepare=False)
        notes = self.box.MEMORY.vault()
        cid = "abc123abc123"
        first = [{"role": "you", "who": "You", "text": "Where should the shelter go?"}]
        rel1 = notes.save_chat(first, "Claude", chat_id=cid)
        second = first + [{"role": "bot", "who": "Claude", "text": "On the ridge."}]
        rel2 = notes.save_chat(second, "Claude", chat_id=cid)
        self.assertEqual(rel1, rel2)
        found = sync.find_chat_notes(notes.path).get(cid) or []
        self.assertEqual(len(found), 1)
        with open(found[0], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("On the ridge.", text)
        self.assertIn(cid, text)
        self.assertNotIn("(2)", os.path.basename(found[0]))
        again = notes.save_chat(second + [{"role": "you", "who": "You", "text": "And the sensor?"}],
                                "Claude", chat_id=cid)
        self.assertEqual(again, rel1)
        self.assertEqual(len(sync.find_chat_notes(notes.path).get(cid) or []), 1)

    def test_save_endpoint_does_not_create_a_second_note(self):
        import sync
        self.box.MEMORY.set_notes_storage("chatbox")
        self.box.MEMORY.update_settings({"sync_chats": False}, _prepare=False)
        srv = serve(self.box)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        body = {"action": "save_chat", "id": "abc123abc123", "backend": "Claude",
                "messages": [{"role": "you", "who": "You", "text": "First draft"}]}
        code, raw = request(srv, "/api/obsidian", body)
        self.assertEqual(code, 200)
        saved = json.loads(raw)["saved"]
        body["messages"] = body["messages"] + [{"role": "bot", "who": "Claude", "text": "Revised"}]
        code, raw = request(srv, "/api/obsidian", body)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["saved"], saved)
        notes = self.box.MEMORY.vault()
        found = sync.find_chat_notes(notes.path).get("abc123abc123") or []
        self.assertEqual(len(found), 1)
        with open(found[0], encoding="utf-8") as handle:
            self.assertIn("Revised", handle.read())

    def test_disconnect_choice_switches_storage_immediately(self):
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        vault_dir = os.path.join(parent, "Main vault")
        os.makedirs(vault_dir)
        self.box.MEMORY.set_notes_storage("obsidian", vault_dir)
        srv = serve(self.box)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        code, raw = request(srv, "/api/notes", {"mode": "chatbox"})
        self.assertEqual(code, 200)
        card = json.loads(raw)["notes"]["card"]
        self.assertTrue(card["title"].startswith("Notes: Inside Chatbox"))
        self.assertFalse(card["disconnect"])
        self.assertEqual(self.box.MEMORY.notes_mode(), "chatbox")
        self.box.MEMORY.set_notes_storage("obsidian", vault_dir)
        code, raw = request(srv, "/api/notes", {"mode": "off"})
        body = json.loads(raw)
        self.assertEqual(body["notes"]["mode"], "off")
        self.assertFalse(body["notes"]["card"]["disconnect"])
        self.assertFalse(body["notes"]["card"]["show_save"])
        self.assertEqual(body["settings"]["notes_storage"], "off")


if __name__ == "__main__":
    unittest.main()

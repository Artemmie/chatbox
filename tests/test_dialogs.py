"""In-app dialogs, and the Qt page that still answers a native confirm()."""
import os
import re
import shutil
import tempfile
import unittest

import accounts
import vault


ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class DialogUiTests(unittest.TestCase):
    def test_native_dialogs_are_replaced_and_the_page_still_answers_them(self):
        js = _read("ui", "js", "app.js")
        html = _read("ui", "index.html")
        app = _read("app.py")
        self.assertIn('id="ask"', html)
        self.assertIn('aria-labelledby="asktitle"', html)
        self.assertIn('aria-describedby="asktext"', html)
        self.assertIn("function ask(", js)
        self.assertIn("function askConfirm", js)
        self.assertIn("function askAlert", js)
        self.assertIn("function askPrompt", js)
        self.assertIsNone(re.search(r"(?<![\w$])(confirm|alert|prompt)\s*\(", js))
        self.assertIn("e.key!=='Enter'", js)
        self.assertIn("addEventListener('cancel'", js)
        self.assertIn("class DialogPage", app)
        self.assertIn("def javaScriptAlert", app)
        self.assertIn("def javaScriptConfirm", app)
        self.assertIn("def javaScriptPrompt", app)
        self.assertIn("QMessageBox.question", app)
        self.assertIn("QMessageBox.information", app)
        self.assertIn("DialogPage(profile, view)", app)
        self.assertNotIn("QWebEnginePage(profile, view)", app)

    def test_settings_check_now_and_the_save_button_follow_the_server(self):
        js = _read("ui", "js", "app.js")
        html = _read("ui", "index.html")
        self.assertIn("Check for updates now", html)
        self.assertLess(html.index("Check for updates"), html.index("Check for updates now"))
        self.assertIn("Checking…", js)
        self.assertIn("{action:'now'}", js)
        self.assertIn("You're on", _read("update.py"))
        self.assertIn("btn.hidden=!card.show_save", js)
        self.assertNotIn("hidden=!VAULT", js)
        self.assertIn("applyNotes(p)", js)
        self.assertIn("Keep notes inside Chatbox", js)
        self.assertIn("Turn notes off", js)
        self.assertIn("Open in Obsidian", html)
        self.assertIn(">Disconnect<", html)
        self.assertNotIn("Obsidian Connected", js)
        self.assertIn('id="savebtn"', html)
        self.assertIn("id:CHAT.id", js)

    def test_obsidian_link_quotes_the_folder(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        weird = os.path.join(folder, "My Vault")
        os.mkdir(weird)
        url = accounts.obsidian_url(weird)
        self.assertTrue(url.startswith("obsidian://open?path="))
        self.assertIn("My%20Vault", url)
        self.assertNotIn(" ", url.split("path=", 1)[1])
        original = vault.obsidian_installed
        vault.obsidian_installed = lambda *args, **kwargs: False
        self.addCleanup(setattr, vault, "obsidian_installed", original)
        with self.assertRaises(ValueError) as caught:
            accounts.open_in_obsidian(weird)
        self.assertIn("not installed", str(caught.exception))
        seen = []
        vault.obsidian_installed = lambda *args, **kwargs: True
        original_launch = accounts._launch
        accounts._launch = lambda target: seen.append(target) or True
        self.addCleanup(setattr, accounts, "_launch", original_launch)
        accounts.open_in_obsidian(weird)
        self.assertEqual(seen, [url])


if __name__ == "__main__":
    unittest.main()

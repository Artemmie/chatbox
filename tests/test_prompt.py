"""Chatbox prompt: the setting, and the command line that carries it."""
import json
import os
import tempfile
import unittest

import memory

from tests.support import isolate, request, serve


PROMPT = 'Say "hi"\nthen stop'


class SystemPromptSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = memory.Store(self.tmp)

    def test_missing_key_uses_the_default_and_context_stays_separate(self):
        with open(self.store.settings_path, "w", encoding="utf-8") as handle:
            json.dump({"instructions": "Be brief."}, handle)
        self.assertEqual(self.store.system_prompt(), memory.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Chatbox", memory.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("Markdown", memory.DEFAULT_SYSTEM_PROMPT)
        self.store.update_settings({"instructions": "Be brief."})
        ctx = self.store.context()
        self.assertIn("Be brief.", ctx)
        self.assertNotIn(memory.DEFAULT_SYSTEM_PROMPT, ctx)

    def test_clear_and_reset(self):
        self.store.update_settings({"system_prompt": "  Custom prompt.  "})
        self.assertEqual(self.store.system_prompt(), "Custom prompt.")
        self.store.update_settings({"system_prompt": ""})
        self.assertEqual(self.store.system_prompt(), "")
        self.store.update_settings({"system_prompt": None})
        self.assertEqual(self.store.settings()["system_prompt"], memory.DEFAULT_SYSTEM_PROMPT)

    def test_settings_page_offers_save_and_reset_next_to_instructions(self):
        root = os.path.join(os.path.dirname(__file__), "..", "ui")
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(root, "js", "app.js"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertLess(html.index('id="instructions"'), html.index('id="promptblock"'))
        self.assertIn(">Chatbox prompt<", html)
        self.assertIn('id="system_prompt"', html)
        self.assertIn("Reset to default", html)
        self.assertIn("no system-prompt flag", html)
        self.assertIn("{settings:{system_prompt:v}}", script)
        self.assertIn("{settings:{system_prompt:null}}", script)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        self.box.find = lambda cmd: os.path.join("/bin", cmd)
        self.box.MEMORY.update_settings({"instructions": "Be brief.", "system_prompt": PROMPT})

    def _flag(self, cmd, name):
        self.assertIn(name, cmd)
        return cmd[cmd.index(name) + 1]

    def test_claude_gets_the_flag_on_every_call(self):
        message, _notes = self.box._prepare_message(
            {"backend": "claude", "message": "hi", "mode": "ask"})
        cmd = self.box.build_command("claude", message, None, "ask", "")
        self.assertEqual(self._flag(cmd, "--append-system-prompt"), PROMPT)
        self.assertEqual(cmd.count("--append-system-prompt"), 1)
        self.assertEqual(cmd[-1], message)
        self.assertNotIn(PROMPT, message)
        self.assertNotIn("[Chatbox prompt]", message)
        self.assertIn("Be brief.", message)
        resumed = self.box.build_command("claude", "next", "sess-1", "ask", "sonnet")
        self.assertEqual(self._flag(resumed, "--append-system-prompt"), PROMPT)
        self.assertIn("--resume", resumed)
        self.assertEqual(resumed[-1], "next")

    def test_empty_prompt_adds_no_flag(self):
        self.box.MEMORY.update_settings({"system_prompt": ""})
        cmd = self.box.build_command("claude", "hi", None, "ask", "")
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[-1], "hi")
        blank = self.box.build_command("claude", "hi", None, "ask", "", system_prompt="  \n")
        self.assertNotIn("--append-system-prompt", blank)
        codex = self.box.build_command("codex", "hi", "sess-1", "ask", "")
        self.assertFalse(any(part.startswith("developer_instructions=") for part in codex))
        self.assertNotIn("--append-system-prompt", codex)

    def test_codex_uses_developer_instructions_on_every_call(self):
        cmd = self.box.build_command("codex", "hello", None, "ask", "")
        flag = next(part for part in cmd if part.startswith("developer_instructions="))
        self.assertEqual(flag, 'developer_instructions="Say \\"hi\\"\\nthen stop"')
        self.assertTrue(any(part.startswith('sandbox_mode="') for part in cmd))
        self.assertLess(cmd.index("-c"), cmd.index(flag))
        self.assertEqual(cmd[-1], "hello")
        resumed = self.box.build_command("codex", "next", "sess-1", "agent", "gpt")
        self.assertIn("resume", resumed)
        self.assertTrue(any(part.startswith("developer_instructions=") for part in resumed))
        self.assertEqual(resumed[-1], "next")
        # The ignored `instructions` key is not what we send.
        self.assertFalse(any(part.startswith("instructions=") for part in cmd))

    def test_cursor_has_no_flag_and_the_first_message_is_not_duplicated(self):
        message, _notes = self.box._prepare_message(
            {"backend": "cursor", "message": "hi", "mode": "ask"})
        cmd = self.box.build_command("cursor", message, None, "ask", "")
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertFalse(any("developer_instructions" in part for part in cmd))
        self.assertNotIn("--system-prompt", cmd)
        self.assertEqual(message.count(PROMPT), 1)
        self.assertIn("[Chatbox prompt]\n" + PROMPT + "\n[End of Chatbox prompt]", message)
        self.assertIn("Be brief.", message)
        self.assertLess(message.index(PROMPT), message.index("Be brief."))
        self.assertTrue(message.endswith("The user's message:\nhi"))
        version = self.box.MEMORY.version()
        again, _notes = self.box._prepare_message({
            "backend": "cursor", "message": "next", "mode": "ask",
            "session_id": "sess-1", "context_version": version,
        })
        self.assertEqual(again, "next")
        self.assertNotIn(PROMPT, again)

    def test_memory_off_still_sends_the_prompt_and_not_the_instructions(self):
        self.box.MEMORY.update_settings({"use_memory": False, "system_prompt": "Be Chatbox."})
        cursor, _notes = self.box._prepare_message(
            {"backend": "cursor", "message": "hi", "mode": "ask"})
        self.assertIn("Be Chatbox.", cursor)
        self.assertNotIn("Be brief.", cursor)
        claude, _notes = self.box._prepare_message(
            {"backend": "claude", "message": "hi", "mode": "ask"})
        self.assertEqual(claude, "hi")
        cmd = self.box.build_command("claude", claude, None, "ask", "")
        self.assertEqual(self._flag(cmd, "--append-system-prompt"), "Be Chatbox.")

    def test_settings_api_clears_and_resets(self):
        srv = serve(self.box)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        code, raw = request(srv, "/api/settings", {"settings": {"system_prompt": ""}})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["settings"]["system_prompt"], "")
        self.assertEqual(self.box.MEMORY.system_prompt(), "")
        code, raw = request(srv, "/api/settings", {"settings": {"system_prompt": None}})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["settings"]["system_prompt"], memory.DEFAULT_SYSTEM_PROMPT)
        code, raw = request(srv, "/api/settings", {"settings": {"instructions": "Stay brief."}})
        body = json.loads(raw)
        self.assertEqual(body["settings"]["instructions"], "Stay brief.")
        self.assertEqual(body["settings"]["system_prompt"], memory.DEFAULT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()

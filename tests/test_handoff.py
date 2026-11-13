"""Handoff transcript, fresh CLI sessions, and per-assistant labels in sync notes."""
import json
import os
import tempfile
import unittest

import handoff
import sync
import vault

from tests.support import isolate, request, serve
from tests.test_server import write_cli


def history():
    return [
        {"r": "you", "t": "Where should the shelter go?"},
        {"r": "bot", "b": "claude", "model": "Sonnet", "t": "On the ridge."},
    ]


class TranscriptTests(unittest.TestCase):
    def test_short_transcript_names_the_speaker_and_the_assistant(self):
        text = handoff.compact_transcript([
            {"r": "you", "t": "Where should the shelter go?",
             "files": [{"name": "ridge map.png", "kind": "image"}]},
            {"r": "bot", "b": "claude", "model": "Sonnet", "t": "On the ridge."},
            {"r": "err", "b": "ollama", "t": "Can't reach Ollama."},
            {"r": "note", "t": "saved a memory"},
        ])
        self.assertIn("You: Where should the shelter go? [attached: ridge map.png]", text)
        self.assertIn("Claude (Sonnet): On the ridge.", text)
        self.assertIn("Ollama (error): Can't reach Ollama.", text)
        self.assertNotIn("saved a memory", text)
        self.assertNotIn("summarized", text)
        self.assertNotIn("…", text)
        self.assertLessEqual(len(text), handoff.BUDGET)

    def test_empty_and_non_lists_are_blank(self):
        self.assertEqual(handoff.compact_transcript([]), "")
        self.assertEqual(handoff.compact_transcript(None), "")
        self.assertEqual(handoff.compact_transcript("nope"), "")

    def test_long_history_is_summarized_to_the_budget(self):
        items = []
        for i in range(30):
            items.append({"r": "you", "t": f"Question {i} " + ("detail " * 400)})
            items.append({"r": "bot", "b": "claude" if i % 2 == 0 else "cursor",
                          "t": f"Answer {i} starts here. " + ("pad " * 400)})
        items.append({"r": "you", "t": "LATEST-QUESTION about the shelter" + (" detail" * 400)})
        items.append({"r": "bot", "b": "cursor",
                      "t": "LATEST-ANSWER put it by the road. " + ("end " * 400)})
        text = handoff.compact_transcript(items)
        self.assertLessEqual(len(text), handoff.BUDGET)
        self.assertIn("LATEST-QUESTION", text)
        self.assertIn("LATEST-ANSWER", text)
        self.assertIn("Cursor:", text)
        self.assertIn("Claude:", text)
        self.assertNotIn("Question 0 " + ("detail " * 50), text)
        self.assertIn("summarized", text)

    def test_one_long_turn_is_trimmed_and_keeps_its_start(self):
        text = handoff.compact_transcript([
            {"r": "bot", "b": "claude", "t": "START " + ("word " * 3000) + "END"},
        ])
        self.assertLessEqual(len(text), handoff.BUDGET)
        self.assertIn("START", text)
        self.assertNotIn("END", text)
        self.assertIn("…", text)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.box, _tmp = isolate()
        self.box.MEMORY.add("I use Unity 6 and C#")

    def test_fresh_session_includes_transcript_and_memory(self):
        message, _notes = self.box._prepare_message({
            "backend": "cursor", "mode": "ask", "message": "What about the road?",
            "history": history(),
        })
        self.assertIn("I use Unity 6 and C#", message)
        self.assertIn("Claude (Sonnet): On the ridge.", message)
        self.assertIn("The user's message:\n", message)
        self.assertLess(message.index("I use Unity 6 and C#"), message.index("Claude (Sonnet):"))
        self.assertLess(message.index("End of earlier conversation"), message.index("The user's message:"))
        self.assertTrue(message.rstrip().endswith("What about the road?"))

    def test_resume_does_not_repeat_the_transcript(self):
        message, _notes = self.box._prepare_message({
            "backend": "claude", "mode": "ask", "message": "Next question",
            "session_id": "sess-1", "history": history(),
        })
        self.assertNotIn("Earlier conversation", message)
        self.assertIn("I use Unity 6 and C#", message)
        self.assertIn("(Context updated.) The user's message:\n", message)

    def test_handoff_drops_the_old_session_and_keeps_plan_rules_after_the_transcript(self):
        message, _notes = self.box._prepare_message({
            "backend": "cursor", "mode": "plan", "message": "Continue",
            "session_id": "old-session", "handoff": True, "history": history(),
        })
        self.assertIsNone(self.box._session_for_turn({
            "session_id": "old-session", "handoff": True}))
        self.assertIn("Earlier conversation", message)
        self.assertIn("Claude (Sonnet):", message)
        self.assertLess(message.index("End of earlier conversation"), message.index("PLAN mode"))
        self.assertLess(message.index("PLAN mode"), message.index("The user's message:"))
        self.assertNotIn("(Context updated.)", message)


class SyncLabelTests(unittest.TestCase):
    def test_each_turn_names_the_assistant_that_answered(self):
        chat = {
            "id": "abc123abc123", "title": "Shelter", "backend": "claude",
            "items": [
                {"r": "you", "t": "Where should the shelter go?"},
                {"r": "bot", "b": "claude", "model": "Sonnet", "t": "On the ridge."},
                {"r": "you", "t": "Ask Cursor too"},
                {"r": "bot", "b": "cursor", "t": "Closer to the road."},
                {"r": "err", "b": "ollama", "t": "Ollama is not running."},
            ],
        }
        text = sync.render_chat(chat)
        self.assertIn("### Claude · Sonnet", text)
        self.assertIn("### Cursor", text)
        self.assertIn("### Ollama", text)
        self.assertLess(text.index("### Claude"), text.index("### Cursor"))
        self.assertLess(text.index("On the ridge."), text.index("Closer to the road."))
        parsed = sync.parse_chat(text)
        bots = [item for item in parsed["items"] if item["r"] in ("bot", "err")]
        self.assertEqual([item["b"] for item in bots], ["claude", "cursor", "ollama"])
        self.assertEqual(bots[2]["r"], "err")
        self.assertEqual(bots[0]["model"], "Sonnet")

    def test_a_turn_without_an_assistant_uses_the_chat_backend(self):
        text = sync.render_chat({
            "id": "abc123abc123", "title": "Fallback", "backend": "codex",
            "items": [{"r": "you", "t": "Hi"}, {"r": "bot", "t": "Hello from Codex"}],
        })
        self.assertIn("### Codex", text)
        self.assertNotIn("### Assistant", text)
        parsed = sync.parse_chat(text)
        self.assertEqual(parsed["items"][1]["b"], "codex")
        self.assertEqual(parsed["items"][1]["t"], "Hello from Codex")

    def test_saved_vault_note_names_each_assistant(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        note = vault.Vault(root)
        rel = note.save_chat([
            {"role": "you", "text": "Where should the shelter go?"},
            {"role": "bot", "who": "Claude", "text": "On the ridge."},
            {"role": "bot", "b": "cursor", "text": "Closer to the road."},
        ], "Claude")
        with open(os.path.join(root, rel), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("**You:**", text)
        self.assertIn("**Claude:**", text)
        self.assertIn("**Cursor:**", text)
        self.assertLess(text.index("**Claude:**"), text.index("**Cursor:**"))
        self.assertIn("On the ridge.", text)
        self.assertIn("Closer to the road.", text)


class FreshCliTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        self.prompt = os.path.join(self.tmp, "prompt.txt")
        self.argv = os.path.join(self.tmp, "argv.txt")
        os.environ["PROMPT_FILE"] = self.prompt
        os.environ["ARGV_FILE"] = self.argv
        cli = write_cli(self.tmp, "claude", """\
            #!/usr/bin/env python3
            import json, os, sys
            open(os.environ["PROMPT_FILE"], "w", encoding="utf-8").write(sys.argv[-1])
            open(os.environ["ARGV_FILE"], "w", encoding="utf-8").write("\\n".join(sys.argv))
            print(json.dumps({"type": "result", "is_error": False, "result": "Joined",
                              "session_id": "newsess"}), flush=True)
            """)
        self.box.find = lambda cmd: cli if cmd == "claude" else None
        self.srv = serve(self.box)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("PROMPT_FILE", None)
        os.environ.pop("ARGV_FILE", None)

    def events(self, body):
        code, raw = request(self.srv, "/api/chat", body)
        self.assertEqual(code, 200, raw)
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def sent(self):
        with open(self.prompt, encoding="utf-8") as handle:
            prompt = handle.read()
        with open(self.argv, encoding="utf-8") as handle:
            argv = handle.read()
        return prompt, argv

    def test_a_fresh_cli_session_gets_the_transcript_in_the_prompt(self):
        evs = self.events({"backend": "claude", "message": "What about the road?", "mode": "ask",
                           "workdir": self.tmp, "history": history()})
        self.assertEqual(evs[-1]["type"], "done")
        self.assertEqual(evs[-1].get("reply"), "Joined")
        prompt, argv = self.sent()
        self.assertIn("Earlier conversation", prompt)
        self.assertIn("On the ridge.", prompt)
        self.assertIn("The user's message:", prompt)
        self.assertIn("What about the road?", prompt)
        self.assertNotIn("--resume", argv)

    def test_resume_does_not_paste_the_transcript_again(self):
        evs = self.events({"backend": "claude", "message": "Next", "mode": "ask",
                           "workdir": self.tmp, "session_id": "old-session", "history": history()})
        self.assertEqual(evs[-1]["type"], "done")
        prompt, argv = self.sent()
        self.assertNotIn("Earlier conversation", prompt)
        self.assertIn("--resume", argv)
        self.assertIn("old-session", argv)

    def test_handoff_starts_fresh_even_when_a_session_id_is_sent(self):
        evs = self.events({"backend": "claude", "message": "Continue", "mode": "ask",
                           "workdir": self.tmp, "session_id": "old-session", "handoff": True,
                           "history": history()})
        self.assertEqual(evs[-1]["type"], "done")
        prompt, argv = self.sent()
        self.assertIn("Earlier conversation", prompt)
        self.assertIn("Claude (Sonnet): On the ridge.", prompt)
        self.assertNotIn("--resume", argv)


if __name__ == "__main__":
    unittest.main()

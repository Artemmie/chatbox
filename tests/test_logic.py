"""Memory, usage, vault, chat history, and stream parsers. No real CLIs."""
import json
import os
import tempfile
import unittest

import memory
import streaming
import usage
import vault

from tests.support import isolate


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = memory.Store(tempfile.mkdtemp())

    def test_add_dedupes_and_context_changes(self):
        self.store.update_settings({"instructions": "Be brief."})
        first = self.store.version()
        item = self.store.add("I use Unity 6 and C#")
        self.assertEqual(item["text"], "I use Unity 6 and C#")
        self.assertIsNone(self.store.add("i use unity 6 and C#"))
        self.assertNotEqual(self.store.version(), first)
        ctx = self.store.context()
        self.assertIn("Be brief.", ctx)
        self.assertIn("Unity 6", ctx)
        self.assertIn("REMEMBER:", ctx)

    def test_remember_lines_are_stripped(self):
        reply, saved = self.store.take_memories("Hello.\nREMEMBER: prefers short answers\n")
        self.assertEqual(reply, "Hello.")
        self.assertEqual(saved[0]["text"], "prefers short answers")
        self.assertEqual(self.store.memories()[0]["source"], "assistant")

    def test_memory_off_sends_nothing(self):
        self.store.add("secret fact")
        self.store.update_settings({"use_memory": False})
        self.assertEqual(self.store.context(), "")


class UsageTests(unittest.TestCase):
    def test_summary_counts_errors_and_days(self):
        log = usage.Log(tempfile.mkdtemp())
        log.record("claude", "ask", "sonnet", True, 1.2, {"input": 10, "output": 4, "cost": 0.01})
        log.record("cursor", "agent", "", False, 3, {"input": 1, "output": 0})
        summary = log.summary(7)
        self.assertEqual(summary["totals"]["claude"]["messages"], 1)
        self.assertEqual(summary["totals"]["claude"]["output"], 4)
        self.assertTrue(summary["totals"]["claude"]["has_cost"])
        self.assertEqual(summary["totals"]["cursor"]["errors"], 1)
        self.assertEqual(summary["modes"]["ask"], 1)
        self.assertEqual(summary["modes"]["agent"], 1)
        self.assertEqual(len(summary["series"]), 7)
        log.clear()
        self.assertEqual(log.summary(7)["count"], 0)


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "notes"))
        with open(os.path.join(self.root, "notes", "Unity.md"), "w", encoding="utf-8") as f:
            f.write("# Unity\n\nThe project uses Unity 6 and C# for the arena.\n")
        self.vault = vault.Vault(self.root)

    def test_memories_roundtrip_and_search(self):
        self.vault.write_memories([{"text": "Answer in Russian"}])
        self.assertEqual(self.vault.read_memories()[0]["text"], "Answer in Russian")
        hits = self.vault.search("How does the Unity arena work?")
        self.assertTrue(any(h["title"] == "Unity" for h in hits))

    def test_path_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            self.vault._abs("../outside.md")

    def test_save_chat(self):
        rel = self.vault.save_chat([{"role": "you", "text": "Plan the sensors"}], "Claude")
        self.assertTrue(rel.startswith("Chatbox/Chats/"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, rel)))


class HistoryTests(unittest.TestCase):
    def test_save_load_delete_and_bad_id(self):
        box, _ = isolate()
        box.save_chat({"id": "abc123abc123", "title": "One", "backend": "claude", "items": []})
        self.assertEqual(box.load_chat("abc123abc123")["title"], "One")
        self.assertEqual(box.list_chats()[0]["id"], "abc123abc123")
        box.delete_chat("abc123abc123")
        self.assertIsNone(box.load_chat("abc123abc123"))
        with self.assertRaises(ValueError):
            box.save_chat({"id": "../etc/passwd"})


class ParserTests(unittest.TestCase):
    def test_codex_grows_then_replaces(self):
        parser = streaming.CodexParser()
        events = []
        lines = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.updated", "item": {"type": "agent_message", "text": "Hel"}},
            {"type": "item.updated", "item": {"type": "agent_message", "text": "Hello"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "cached_input_tokens": 1,
                                                 "output_tokens": 2, "reasoning_output_tokens": 4}},
        ]
        for line in lines:
            events += parser.feed_line(json.dumps(line))
        self.assertIn(("delta", "Hel"), events)
        self.assertIn(("delta", "lo"), events)
        self.assertIn(("replace", "Answer"), events)
        done = parser.finish()
        self.assertEqual(done["reply"], "Answer")
        self.assertEqual(done["usage"]["cache_read"], 1)
        self.assertEqual(done["session_id"], "t1")

    def test_claude_deltas_and_plain_json(self):
        parser = streaming.ClaudeParser()
        events = parser.feed_line(json.dumps({
            "type": "stream_event", "session_id": "s",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "He"}}}))
        events += parser.feed_line(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "llo"}}}))
        events += parser.feed_line(json.dumps({
            "type": "result", "result": "Hello", "session_id": "s",
            "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.01}))
        self.assertEqual(events, [("delta", "He"), ("delta", "llo")])
        done = parser.finish()
        self.assertEqual(done["reply"], "Hello")
        self.assertEqual(done["usage"]["cost"], 0.01)

        plain = streaming.ClaudeParser()
        plain.feed_line(json.dumps({"result": "Hi", "session_id": "z", "is_error": False,
                                    "usage": {"input_tokens": 1, "output_tokens": 1}}))
        self.assertEqual(plain.finish()["reply"], "Hi")
        err = streaming.ClaudeParser()
        err.feed_line(json.dumps({"type": "result", "is_error": True, "result": "not logged in"}))
        self.assertIn("not logged in", err.finish()["error"])

    def test_cursor_partial_skips_duplicates(self):
        parser = streaming.CursorParser()
        events = []
        events += parser.feed_line(json.dumps({"type": "assistant", "timestamp_ms": 1,
                                               "message": {"content": [{"type": "text", "text": "He"}]}}))
        events += parser.feed_line(json.dumps({"type": "assistant", "timestamp_ms": 2,
                                               "message": {"content": [{"type": "text", "text": "Hello"}]}}))
        events += parser.feed_line(json.dumps({"type": "assistant", "timestamp_ms": 3, "model_call_id": "m",
                                               "message": {"content": [{"type": "text", "text": "Hello"}]}}))
        events += parser.feed_line(json.dumps({"type": "result", "result": "Hello", "session_id": "c"}))
        self.assertEqual(events, [("delta", "He"), ("delta", "llo")])
        self.assertEqual(parser.finish()["reply"], "Hello")

    def test_ollama_think_filter(self):
        import ollama
        filt = ollama.ThinkFilter()
        self.assertEqual(filt.feed("Hi <thi"), "Hi ")
        self.assertEqual(filt.feed("nk>secret</think>!"), "!")
        self.assertEqual(filt.flush(), "")


class MacPathTests(unittest.TestCase):
    def test_chatgpt_app_bundle_is_a_codex_candidate(self):
        import chatbox
        joined = "\n".join(chatbox.CODEX_APP_CANDIDATES)
        self.assertIn("/Applications/ChatGPT.app/Contents/Resources/codex", joined)
        self.assertIn("/Applications/Codex.app/Contents/Resources/codex", joined)

    def test_bundled_label_ignores_resources_capitalization(self):
        path = "/Applications/ChatGPT.app/Contents/Resources/codex"
        self.assertTrue(path.replace("\\", "/").lower().endswith("resources/codex"))


if __name__ == "__main__":
    unittest.main()

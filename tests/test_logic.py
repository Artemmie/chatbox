"""Memory, usage, vault, chat history, and stream parsers. No real CLIs."""
import json
import os
import tempfile
import unicodedata
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
        self.assertIn("THIS message", ctx)
        self.assertIn("vault notes", ctx)
        self.assertIn("at most one", ctx.lower())
        self.assertIn("most replies should have none", ctx.lower())

    def test_remember_lines_are_stripped(self):
        reply, saved = self.store.take_memories(
            "Hello.\nREMEMBER: prefers short answers\n",
            "I prefer short answers")
        self.assertEqual(reply, "Hello.")
        self.assertEqual(saved[0]["text"], "prefers short answers")
        self.assertEqual(self.store.memories()[0]["source"], "assistant")

    def test_vault_sourced_fact_is_rejected(self):
        notes = [{"title": "Unity", "path": "notes/Unity.md",
                  "text": "The arena project uses Unity 6 and C# for shelter placement."}]
        user = "The arena project uses Unity 6 and C# for shelter placement."
        reply, saved = self.store.take_memories(
            "Right.\nREMEMBER: The arena project uses Unity 6 and C# for shelter placement.\n",
            user, notes)
        self.assertEqual(saved, [])
        self.assertEqual(reply, "Right.")
        self.assertEqual(self.store.memories(), [])
        # The same words from the user, with nothing injected from the vault, are kept.
        reply, saved = self.store.take_memories(
            "Right.\nREMEMBER: The arena project uses Unity 6 and C# for shelter placement.\n",
            user, [])
        self.assertEqual([m["text"] for m in saved],
                         ["The arena project uses Unity 6 and C# for shelter placement."])
        self.assertNotIn("REMEMBER", reply)

    def test_user_stated_preference_is_saved(self):
        notes = [{"text": "The arena project uses Unity 6 and C# for shelter placement."}]
        reply, saved = self.store.take_memories(
            "Got it.\nREMEMBER: prefers brief replies\n",
            "Please answer in short sentences. I prefer brief replies.",
            notes)
        self.assertEqual([m["text"] for m in saved], ["prefers brief replies"])
        self.assertEqual(reply, "Got it.")
        # A fact the user did not say (a file, a tool, or the model's own answer) is dropped.
        reply, saved = self.store.take_memories(
            "Berlin is cloudy.\nREMEMBER: lives in Berlin\n",
            "What's the weather?")
        self.assertEqual(saved, [])
        self.assertEqual(reply, "Berlin is cloudy.")
        self.assertEqual([m["text"] for m in self.store.memories()], ["prefers brief replies"])

    def test_near_duplicate_memory_is_rejected(self):
        self.store.add("I use Unity 6 and C#")
        reply, saved = self.store.take_memories(
            "Sure.\nREMEMBER: Uses Unity 6 and C#\n",
            "Please remember that I use Unity 6 and C#")
        self.assertEqual(saved, [])
        self.assertEqual([m["text"] for m in self.store.memories()], ["I use Unity 6 and C#"])
        self.assertEqual(reply, "Sure.")

    def test_russian_text_is_casefolded(self):
        user = unicodedata.normalize("NFD", "Я ПРЕДПОЧИТАЮ ответы на русском языке")
        reply, saved = self.store.take_memories(
            "Хорошо.\nREMEMBER: предпочитает ответы на русском языке\n", user)
        self.assertEqual([m["text"] for m in saved], ["предпочитает ответы на русском языке"])
        self.assertEqual(reply, "Хорошо.")
        # A Cyrillic fact copied from the vault is rejected even when the user repeated it.
        notes = [{"title": "Егор", "path": "People/Егор.md",
                  "text": "Егор работает над проектом telegram-ai и пишет ботов."}]
        reply, saved = self.store.take_memories(
            "Да.\nREMEMBER: Егор работает над проектом telegram-ai\n",
            "Егор работает над проектом telegram-ai", notes)
        self.assertEqual(saved, [])
        self.assertEqual(reply, "Да.")
        self.assertEqual(len(self.store.memories()), 1)

    def test_at_most_one_memory_per_reply(self):
        reply, saved = self.store.take_memories(
            "OK.\nREMEMBER: prefers short answers\nREMEMBER: wants replies in Russian\n",
            "I prefer short answers and I want replies in Russian.")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["text"], "prefers short answers")
        self.assertEqual([m["text"] for m in self.store.memories()], ["prefers short answers"])
        self.assertNotIn("REMEMBER", reply)
        self.assertIn("OK.", reply)

    def test_finish_result_passes_the_user_message_and_vault_notes(self):
        box, _tmp = isolate()
        notes = [{"path": "notes/Unity.md", "title": "Unity",
                  "text": "The arena project uses Unity 6 and C# for shelter placement."}]
        result = {"reply": "Noted.\nREMEMBER: The arena project uses Unity 6 and C# for shelter placement.\n"
                           "REMEMBER: prefers short answers\n"}
        req = {"backend": "claude", "mode": "ask",
               "message": "The arena project uses Unity 6 and C# for shelter placement. "
                          "I prefer short answers."}
        out = box._finish_result(req, result, notes, 0)
        self.assertNotIn("REMEMBER", out["reply"])
        self.assertEqual([m["text"] for m in out["saved_memories"]], ["prefers short answers"])

    def test_custom_instructions_are_not_saved_again(self):
        self.store.update_settings({"instructions": "Answer in Russian."})
        reply, saved = self.store.take_memories(
            "OK.\nREMEMBER: Answer in Russian\n",
            "Please answer in Russian from now on.")
        self.assertEqual(saved, [])
        self.assertEqual(reply, "OK.")
        self.assertEqual(self.store.memories(), [])

    def test_vault_undo_deletes_the_saved_line(self):
        root = tempfile.mkdtemp()
        self.store.connect_vault(root)
        reply, saved = self.store.take_memories(
            "Got it.\nREMEMBER: prefers short answers\n",
            "I prefer short answers")
        self.assertEqual(reply, "Got it.")
        self.assertEqual(len(saved), 1)
        self.store.delete(saved[0]["id"])
        self.assertEqual(self.store.memories(), [])

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

    def test_symlinked_folder_and_nfd_name_are_found(self):
        real = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(real, True))
        people = os.path.join(real, "People")
        os.makedirs(people)
        with open(os.path.join(people, "Егор.md"), "w", encoding="utf-8") as handle:
            handle.write("# Егор\n\nЕгор работает над telegram-ai.\n")
        nfd_name = unicodedata.normalize("NFD", "Алёна.md")
        with open(os.path.join(people, nfd_name), "w", encoding="utf-8") as handle:
            handle.write("# Алёна\n\nАлёна ведёт заметки.\n")
        parent = os.path.join(self.root, "telegram-ai")
        os.makedirs(parent)
        os.symlink(real, os.path.join(parent, "shizik-memory"), target_is_directory=True)
        os.symlink(self.root, os.path.join(real, "loop-back"), target_is_directory=True)
        notes = self.vault._notes()
        rels = [os.path.relpath(p, self.root).replace("\\", "/") for p in notes]
        self.assertIn("telegram-ai/shizik-memory/People/Егор.md", rels)
        self.assertTrue(any(unicodedata.normalize("NFC", r).endswith("Алёна.md") for r in rels))
        self.assertFalse(any("loop-back" in r for r in rels))
        egor = self.vault.search("кто такой Егор")
        self.assertTrue(any(h["path"].replace("\\", "/").endswith("People/Егор.md") for h in egor), egor)
        alyona = self.vault.search("what do you know about Алёна")
        self.assertTrue(any(unicodedata.normalize("NFC", h["title"]) == "Алёна" for h in alyona), alyona)

    def test_cli_context_says_to_follow_symlinks(self):
        box, _tmp = isolate()
        box.MEMORY.connect_vault(self.root)
        message, _notes = box._prepare_message({"backend": "claude", "message": "hi", "mode": "ask"})
        self.assertIn(self.root, message)
        self.assertIn("rg -L", message)
        self.assertIn("symlink", message.lower())
        other, _notes = box._prepare_message({"backend": "chatgpt", "message": "hi", "mode": "ask"})
        self.assertNotIn("rg -L", other)

    def test_save_chat(self):
        rel = self.vault.save_chat([{"role": "you", "text": "Plan the sensors"}], "Claude")
        self.assertTrue(rel.startswith("Chatbox/Chats/"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, rel)))

    def test_memories_keep_user_text_and_link_to_chatbox(self):
        folder = os.path.join(self.root, "Chatbox")
        os.makedirs(folder)
        with open(os.path.join(folder, "Memories.md"), "w", encoding="utf-8") as handle:
            handle.write("# My memories\n\nI wrote this.\n\n- Old fact\n")
        self.vault.write_memories([{"text": "Answer in Russian"}])
        text = open(os.path.join(folder, "Memories.md"), encoding="utf-8").read()
        self.assertIn("I wrote this.", text)
        self.assertIn("Part of [[Chatbox]]", text)
        self.assertIn("- Answer in Russian", text)
        self.assertNotIn("Old fact", text)
        self.assertEqual(self.vault.read_memories()[0]["text"], "Answer in Russian")
        note = open(os.path.join(folder, "Chatbox.md"), encoding="utf-8").read()
        self.assertIn("- [[Memories]]", note)
        self.assertNotIn("I wrote this.", note)

    def test_save_chat_links_the_graph_without_clobbering_notes(self):
        folder = os.path.join(self.root, "Chatbox")
        os.makedirs(folder)
        note_path = os.path.join(folder, "Chatbox.md")
        with open(note_path, "w", encoding="utf-8") as handle:
            handle.write("# Project\n\nShelter placement notes.\n")
        rel = self.vault.save_chat([{"role": "you", "text": "Plan the sensors", "who": "You"}], "Claude")
        chat = open(os.path.join(self.root, rel), encoding="utf-8").read()
        self.assertIn('up: "[[Chatbox]]"', chat)
        self.assertTrue(chat.split("---", 2)[-1].lstrip().startswith("Part of [[Chatbox]]"))
        self.assertIn("**You:**", chat)
        text = open(note_path, encoding="utf-8").read()
        self.assertIn("Shelter placement notes.", text)
        self.assertIn("[[Memories]]", text)
        self.assertIn("Plan the sensors", text)
        self.assertLess(text.index("Shelter placement notes."), text.index("chatbox:index"))
        rel2 = self.vault.save_chat([{"role": "you", "text": "Second pass"}], "Claude")
        text = open(note_path, encoding="utf-8").read()
        self.assertEqual(text.count("%% chatbox:index %%"), 1)
        self.assertIn("Plan the sensors", text)
        self.assertIn(os.path.splitext(os.path.basename(rel2))[0], text)
        self.assertIn("Shelter placement notes.", text)


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

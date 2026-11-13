"""Deleting a chat: it stays deleted everywhere, and its memories can go with it."""
import json
import os
import unittest

import sync

from tests.support import isolate, request, serve
from tests.test_sync import vault_with_sync

CID = "abc123abc123"


def chat(cid=CID, title="Hi there"):
    return {"id": cid, "title": title, "backend": "claude", "created": 1700000000000,
            "updated": 1700000000, "items": [{"r": "you", "t": "hello"}]}


class DeleteStaysDeletedTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        self.root = vault_with_sync(self.box)

    def test_delete_removes_note_and_local_copy(self):
        os.makedirs(self.box.CHATS_DIR, exist_ok=True)
        local = os.path.join(self.box.CHATS_DIR, CID + ".json")
        with open(local, "w", encoding="utf-8") as f:
            json.dump(chat(), f)
        sync.save_chat(self.root, chat())
        self.box.delete_chat(CID)
        self.assertFalse(os.path.exists(local))
        self.assertNotIn(CID, sync.find_chat_notes(self.box.MEMORY.vault().path))
        self.assertIn(CID, sync.deleted_ids(self.root))

    def test_old_local_copy_on_another_computer_is_not_exported_again(self):
        sync.save_chat(self.root, chat())
        sync.delete_chat(self.root, CID)
        # The other computer still has its old JSON copy of the same chat.
        os.makedirs(self.box.CHATS_DIR, exist_ok=True)
        with open(os.path.join(self.box.CHATS_DIR, CID + ".json"), "w", encoding="utf-8") as f:
            json.dump(chat(), f)
        sync.backfill_local(self.box.MEMORY)
        self.assertNotIn(CID, sync.find_chat_notes(self.box.MEMORY.vault().path))

    def test_deleted_list_survives_index_updates_and_keeps_user_text(self):
        sync.save_chat(self.root, chat())
        sync.save_chat(self.root, chat("def456def456", "Other"))
        note = os.path.join(self.root, "Chatbox.md")
        with open(note, "a", encoding="utf-8") as f:
            f.write("\nMy own line.\n")
        sync.delete_chat(self.root, CID)
        sync.save_chat(self.root, chat("aaa111aaa111", "Third"))  # rewrites the index
        text = open(note, encoding="utf-8").read()
        self.assertIn("My own line.", text)
        self.assertEqual(text.count("%% chatbox:deleted %%"), 1)
        self.assertEqual(sync.deleted_ids(self.root), {CID})
        sync.delete_chat(self.root, "def456def456")
        self.assertEqual(sync.deleted_ids(self.root), {CID, "def456def456"})


class MemoryLinkTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()

    def test_link_round_trips_through_markdown(self):
        items = [{"id": "111111111111", "text": "I use Unity 6", "source": "assistant",
                  "created": "2026-11-14", "chat": CID},
                 {"id": "222222222222", "text": "Plain one", "source": "you"}]
        parsed = sync.parse_memories(sync.render_memories(items))
        self.assertEqual(parsed[0]["chat"], CID)
        self.assertEqual(parsed[0]["source"], "assistant")
        self.assertEqual(parsed[0]["created"], "2026-11-14")
        self.assertNotIn("chat", parsed[1])
        self.assertEqual(parsed[1]["text"], "Plain one")

    def _check_store(self):
        m = self.box.MEMORY
        m.update_settings({"auto_memory": True})
        a = m.add("I use Unity 6 for my games", chat=CID)
        m.add("Answer in short bullets")
        m.add("bad link stays unlinked", chat="not-an-id")
        self.assertEqual([x["text"] for x in m.for_chat(CID)], ["I use Unity 6 for my games"])
        m.delete_many([a["id"]])
        self.assertEqual(sorted(x["text"] for x in m.memories()),
                         ["Answer in short bullets", "bad link stays unlinked"])

    def test_local_store(self):
        self._check_store()

    def test_synced_store(self):
        vault_with_sync(self.box)
        self._check_store()

    def test_auto_saved_memory_gets_the_chat_id(self):
        _, saved = self.box.MEMORY.take_memories(
            "Sure.\nREMEMBER: I make games in Unity", "I make games in Unity", chat_id=CID)
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.box.MEMORY.for_chat(CID)[0]["text"], "I make games in Unity")


class MemoryDeleteAfterRestartTests(unittest.TestCase):
    def test_delete_works_when_the_file_was_written_elsewhere(self):
        box, _ = isolate()
        root = vault_with_sync(box)
        sync.write_memories(root, [{"id": "111111111111", "text": "Old fact", "source": "you"},
                                   {"id": "222222222222", "text": "Keep me", "source": "you"}])
        sync.library(root).memory_hash = None  # as after a restart, or a change from Obsidian Sync
        box.MEMORY.delete("111111111111")
        self.assertEqual([m["text"] for m in box.MEMORY.memories()], ["Keep me"])

    def test_change_between_read_and_write_is_still_merged(self):
        box, _ = isolate()
        root = vault_with_sync(box)
        sync.write_memories(root, [{"id": "111111111111", "text": "Mine", "source": "you"}])
        items = sync.read_memories(root)
        with open(sync.memories_path(root), "a", encoding="utf-8") as f:
            f.write("- Added in Obsidian meanwhile\n")
        sync.write_memories(root, items + [{"id": "333333333333", "text": "New", "source": "you"}])
        texts = [m["text"] for m in sync.read_memories(root)]
        self.assertIn("Added in Obsidian meanwhile", texts)
        self.assertIn("New", texts)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.box, self.tmp = isolate()
        vault_with_sync(self.box)
        self.srv = serve(self.box)
        self.addCleanup(self.srv.shutdown)

    def call(self, body):
        status, raw = request(self.srv, "/api/chats", body)
        self.assertEqual(status, 200)
        return json.loads(raw)

    def test_delete_with_and_without_memories(self):
        m = self.box.MEMORY
        self.call({"action": "save", "chat": chat()})
        keep = m.add("Keep this one", chat="def456def456")["id"]
        first = m.add("From this chat", chat=CID)["id"]
        self.assertEqual([x["id"] for x in self.call({"action": "memories", "id": CID})["memories"]], [first])
        # Only memories that really belong to this chat can be deleted with it.
        self.call({"action": "delete", "id": CID, "memories": [first, keep]})
        ids = {x["id"] for x in m.memories()}
        self.assertNotIn(first, ids)
        self.assertIn(keep, ids)
        self.assertEqual(self.call({"action": "memories", "id": CID})["memories"], [])

    def test_keep_memories(self):
        m = self.box.MEMORY
        self.call({"action": "save", "chat": chat()})
        mid = m.add("From this chat", chat=CID)["id"]
        self.call({"action": "delete", "id": CID, "memories": []})
        self.assertIn(mid, {x["id"] for x in m.memories()})


if __name__ == "__main__":
    unittest.main()

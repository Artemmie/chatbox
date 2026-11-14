"""Updates: fast-forward a local clone, refuse dirty or diverged history, never run a GitHub download."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import update


def _git(repo, *args):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})
    subprocess.check_call(["git", "-C", repo, *args], env=env, stdout=subprocess.DEVNULL)


def _commit(repo, name, text):
    path = os.path.join(repo, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


class GitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.src = os.path.join(self.tmp, "src")
        self.data = os.path.join(self.tmp, "data")
        self.other = os.path.join(self.tmp, "other")
        os.makedirs(self.data)
        subprocess.check_call(["git", "init", "-q", "-b", "main", self.src])
        _git(self.src, "config", "user.email", "test@example.com")
        _git(self.src, "config", "user.name", "Test")
        with open(os.path.join(self.src, "install.sh"), "w", encoding="utf-8") as handle:
            handle.write("#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' \"$*\" >> \"$CHATBOX_DATA/ran\"\n")
        os.chmod(os.path.join(self.src, "install.sh"), 0o755)
        with open(os.path.join(self.src, "README"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        _git(self.src, "add", "install.sh", "README")
        _git(self.src, "commit", "-m", "init")
        subprocess.check_call(["git", "init", "-q", "--bare", "-b", "main", self.origin])
        _git(self.src, "remote", "add", "origin", self.origin)
        _git(self.src, "push", "-u", "origin", "main")
        subprocess.check_call(["git", "clone", "-q", self.origin, self.other])
        _git(self.other, "config", "user.email", "test@example.com")
        _git(self.other, "config", "user.name", "Test")
        update.write_install(self.data, self.src, "browser", "")
        self._schedule = update._schedule
        update._schedule = lambda cmd: None
        update._ctx.clear()
        update.STATE.update(enabled=True, available=False, can_apply=False, checking=False,
                            message="", checked_at=0)
        self.addCleanup(self._restore)

    def _restore(self):
        update._schedule = self._schedule
        update._ctx.clear()

    def _advance(self, text="two\n"):
        _commit(self.other, "notes.txt", text)
        _git(self.other, "push", "origin", "main")

    def _head(self, repo):
        return subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()

    def test_fast_forward_updates_and_keeps_data(self):
        marker = "fixture-account-value"
        os.makedirs(os.path.join(self.data, "chats"))
        os.makedirs(os.path.join(self.data, "chatgpt"))
        with open(os.path.join(self.data, "chats", "one.json"), "w", encoding="utf-8") as handle:
            handle.write('{"title":"kept"}')
        with open(os.path.join(self.data, "settings.json"), "w", encoding="utf-8") as handle:
            handle.write('{"instructions":"kept"}')
        with open(os.path.join(self.data, "chatgpt", "account.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"access_token": marker}))
        before = self._head(self.src)
        self._advance()
        found = update.check_git(self.src, fetch=True)
        self.assertTrue(found["available"])
        self.assertTrue(found["can_apply"])
        cmds = []
        real = subprocess.run

        def wrapped(cmd, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
                cmds.append(list(cmd))
                self.assertNotIn("--force", cmd)
                self.assertNotIn("reset", cmd)
            return real(cmd, **kwargs)

        subprocess.run = wrapped
        self.addCleanup(setattr, subprocess, "run", real)
        restarted = []
        result = update.apply(self.data, self.src, port=8765, restarter=restarted.append)
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(self._head(self.src), before)
        self.assertEqual(self._head(self.src), self._head(self.other))
        merges = subprocess.check_output(["git", "-C", self.src, "rev-list", "--merges", "HEAD"], text=True)
        self.assertEqual(merges.strip(), "")
        self.assertTrue(any("--ff-only" in cmd for cmd in cmds), cmds)
        with open(os.path.join(self.data, "ran"), encoding="utf-8") as handle:
            self.assertIn("--browser", handle.read())
        self.assertEqual(open(os.path.join(self.data, "chats", "one.json"), encoding="utf-8").read(),
                         '{"title":"kept"}')
        self.assertIn(marker, open(os.path.join(self.data, "chatgpt", "account.json"), encoding="utf-8").read())
        self.assertEqual(open(os.path.join(self.data, "settings.json"), encoding="utf-8").read(),
                         '{"instructions":"kept"}')
        self.assertTrue(restarted)
        self.assertIn("--no-browser", restarted[0])
        self.assertIn("--port", restarted[0])
        self.assertIn("8765", restarted[0])

    def test_local_changes_abort(self):
        self._advance("remote\n")
        with open(os.path.join(self.src, "README"), "a", encoding="utf-8") as handle:
            handle.write("local edit\n")
        before = self._head(self.src)
        result = update.apply(self.data, self.src, restarter=lambda cmd: self.fail("restarted"))
        self.assertFalse(result["ok"])
        self.assertIn("local changes", result["error"])
        self.assertEqual(self._head(self.src), before)
        self.assertFalse(os.path.exists(os.path.join(self.data, "ran")))
        self.assertIn("local edit", open(os.path.join(self.src, "README"), encoding="utf-8").read())

    def test_diverged_history_is_not_merged(self):
        _commit(self.src, "local.txt", "mine\n")
        self._advance("theirs\n")
        before = self._head(self.src)
        found = update.check(self.data, self.src, fetch=True)
        self.assertTrue(found["available"])
        self.assertFalse(found["can_apply"])
        result = update.apply(self.data, self.src, restarter=lambda cmd: self.fail("restarted"))
        self.assertFalse(result["ok"])
        self.assertIn("fast-forward", result["error"])
        self.assertEqual(self._head(self.src), before)
        merges = subprocess.check_output(["git", "-C", self.src, "rev-list", "--merges", "HEAD"], text=True)
        self.assertEqual(merges.strip(), "")
        self.assertFalse(os.path.exists(os.path.join(self.src, "notes.txt")))

    def test_hooks_are_not_run(self):
        hook = os.path.join(self.src, ".git", "hooks", "post-merge")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho hooked > \"%s/HOOKED\"\n" % self.src)
        os.chmod(hook, 0o755)
        self._advance("hooked-remote\n")
        result = update.apply(self.data, self.src, restarter=lambda cmd: None)
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.exists(os.path.join(self.src, "HOOKED")))

    def test_install_record_and_distrobox_mode(self):
        self.assertEqual(update.write_install(self.data, self.src, "browser", "bad name")["distrobox"], "")
        info = update.write_install(self.data, self.src, "window", "mybox")
        self.assertEqual(info["source"], os.path.realpath(self.src))
        self.assertEqual(info["mode"], "window")
        self.assertEqual(info["distrobox"], "mybox")
        self.assertEqual(len(info["commit"]), 40)
        self._advance("box\n")
        update.apply(self.data, self.src, restarter=lambda cmd: None)
        with open(os.path.join(self.data, "ran"), encoding="utf-8") as handle:
            args = handle.read()
        self.assertNotIn("--browser", args)
        self.assertIn("--distrobox", args)
        self.assertIn("mybox", args)

    def test_checks_can_be_turned_off(self):
        calls = []
        original = update.fetch_upstream
        update.fetch_upstream = lambda *args, **kwargs: calls.append(1)
        self.addCleanup(setattr, update, "fetch_upstream", original)
        update._ctx.update(data_dir=self.data, here=self.src,
                           settings=lambda: {"auto_update": False, "auto_install_updates": True},
                           busy=lambda: False, port=lambda: None)
        view = update.consider(auto=True)
        self.assertEqual(calls, [])
        self.assertFalse(view["enabled"])
        self.assertFalse(view["available"])

    def test_startup_checks_only_after_an_hour(self):
        now = 10000
        self.assertTrue(update.should_check_on_startup(0, now=now))
        self.assertFalse(update.should_check_on_startup(now - 3599, now=now))
        self.assertTrue(update.should_check_on_startup(now - 3600, now=now))
        self.assertEqual(update.START_CHECK_AFTER, 60 * 60)
        self.assertGreater(update.CHECK_EVERY, update.START_CHECK_AFTER)

    def test_check_now_ignores_the_six_hour_gap(self):
        calls = []
        update._ctx.update(data_dir=self.data, here=self.src,
                           settings=lambda: {"auto_update": True, "auto_install_updates": False},
                           busy=lambda: False, port=lambda: None)
        update.STATE["checked_at"] = time.time()
        original = update.check

        def fake(*_args, **_kwargs):
            calls.append("check")
            return {"available": False, "can_apply": False, "current": "", "latest": "",
                    "message": "", "kind": "git", "url": ""}

        update.check = fake
        self.addCleanup(setattr, update, "check", original)
        view = update.check_now()
        self.assertEqual(calls, ["check"])
        self.assertTrue(view["summary"].startswith("You're on v"))
        self.assertIn("(latest)", view["summary"])
        self.assertFalse(view["available"])
        update.snapshot()
        self.assertEqual(calls, ["check"])
        failed = update.summary_line({"available": False, "message": "Could not check for an update."})
        self.assertEqual(failed, "Could not check for an update.")
        self.assertNotIn("latest", failed)

    def test_check_now_runs_when_automatic_checks_are_off(self):
        calls = []
        update._ctx.update(data_dir=self.data, here=self.src,
                           settings=lambda: {"auto_update": False},
                           busy=lambda: False, port=lambda: None)
        original = update.check
        update.check = lambda *_a, **_k: calls.append(1) or {
            "available": True, "can_apply": False, "message": "A newer Chatbox is published.",
            "kind": "git", "url": "", "current": "", "latest": "v9",
        }
        self.addCleanup(setattr, update, "check", original)
        view = update.check_now()
        self.assertEqual(calls, [1])
        self.assertTrue(view["available"])
        self.assertEqual(view["summary"], "A newer Chatbox is published.")
        self.assertNotIn("latest", view["summary"])

    def test_start_skips_a_check_from_the_last_hour(self):
        import threading
        when = time.time() - 30
        update.write_checked_at(self.data, when)
        update.STATE["checked_at"] = 0
        update._started = False
        calls = []
        original_consider = update.consider
        original_sleep = time.sleep
        update.consider = lambda *args, **kwargs: calls.append(kwargs)
        started = threading.Event()

        def stop(_seconds):
            started.set()
            raise SystemExit

        time.sleep = stop
        self.addCleanup(setattr, time, "sleep", original_sleep)
        self.addCleanup(setattr, update, "consider", original_consider)
        self.addCleanup(setattr, update, "_started", True)
        update.start(self.data, self.src, lambda: {"auto_update": True}, lambda: False, lambda: None)
        self.assertTrue(started.wait(2))
        self.assertEqual(calls, [])
        self.assertAlmostEqual(update.STATE["checked_at"], when, delta=1)
        self.assertAlmostEqual(update.read_checked_at(self.data), when, delta=1)

    def test_startup_tick_checks_when_the_last_one_is_old(self):
        calls = []
        original = update.consider
        update.consider = lambda *args, **kwargs: calls.append(kwargs)
        self.addCleanup(setattr, update, "consider", original)
        update.STATE["checked_at"] = time.time() - 3601
        update._startup_tick(True)
        self.assertEqual(calls, [{"auto": True}])
        calls.clear()
        update.STATE["checked_at"] = time.time()
        update._startup_tick(True)
        self.assertEqual(calls, [])


class GitHubUpdateTests(unittest.TestCase):
    def test_tag_check_does_not_download_or_run_anything(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        data = os.path.join(tmp, "data")
        here = os.path.join(tmp, "plain")
        os.makedirs(data)
        os.makedirs(here)
        local = "b" * 40
        remote = "a" * 40
        with open(os.path.join(data, "install.json"), "w", encoding="utf-8") as handle:
            json.dump({"source": here, "mode": "browser", "commit": local}, handle)
        seen = []

        def opener(url):
            seen.append(url)
            self.assertNotIn("zipball", url)
            self.assertNotIn("tarball", url)
            self.assertTrue(url.startswith("https://api.github.com/repos/Artemmie/chatbox/"))
            return json.dumps([{"name": "v9", "commit": {"sha": remote}}])

        found = update.check(data, here, opener=opener)
        self.assertEqual(found["kind"], "github")
        self.assertTrue(found["available"])
        self.assertFalse(found["can_apply"])
        self.assertEqual(seen, [update.TAGS_URL])
        original = urllib.request.urlopen

        def urlopen(req, timeout=15):
            url = req.full_url
            seen.append(url)
            self.assertNotIn("zipball", url)
            self.assertNotIn("tarball", url)
            raise urllib.error.URLError("offline")

        urllib.request.urlopen = urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)
        result = update.apply(data, here, restarter=lambda cmd: self.fail("restarted"))
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("url", "").startswith("https://github.com/Artemmie/chatbox"))
        self.assertFalse(any("zipball" in url or "tarball" in url for url in seen))


if __name__ == "__main__":
    unittest.main()


class InstalledVersionTests(unittest.TestCase):
    def test_install_copies_the_version_file(self):
        # An installed copy reads VERSION next to its own files. install.sh used to
        # copy only *.py and ui/, so the sidebar and Settings showed no version.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "install.sh"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertRegex(script, r'(?m)^cp .*"\$SRC"?/VERSION"? .*"\$D"/')
        self.assertTrue(os.path.isfile(os.path.join(root, "VERSION")))

"""Version bumps: semver order, title rules, and no release for docs-only pushes."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import importlib.util


def load_next_version():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "next_version.py")
    spec = importlib.util.spec_from_file_location("next_version", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


next_version = load_next_version()


def commit(subject, paths, body="", author_name="Dev", author_email="dev@example.com"):
    return {"subject": subject, "body": body, "author_name": author_name,
            "author_email": author_email, "paths": paths}


class NextVersionTests(unittest.TestCase):
    def test_patch_fix_and_double_digit(self):
        self.assertEqual(next_version.next_version("1.0.0", [commit("fix: crash", ["update.py"])]), "1.0.1")
        self.assertEqual(next_version.next_version("v1.0.9", [commit("chore: tidy", ["chatbox.py"])]), "1.0.10")
        self.assertEqual(next_version.next_version("1.0.10", [commit("fix: again", ["chatbox.py"])]), "1.0.11")

    def test_minor_from_feat_add_and_new_feature(self):
        code = ["update.py"]
        self.assertEqual(next_version.next_version("1.2.3", [commit("feat: shelters", code)]), "1.3.0")
        self.assertEqual(next_version.next_version("1.2.3", [commit("feat(map): shelters", code)]), "1.3.0")
        self.assertEqual(next_version.next_version("1.2.3", [commit("Add shelter placement", code)]), "1.3.0")
        self.assertEqual(next_version.next_version("1.2.3", [commit("This is a new feature", code)]), "1.3.0")
        self.assertEqual(next_version.next_version("1.2.3", [commit("Added a comment", code)]), "1.2.4")

    def test_major_from_breaking_marker(self):
        code = ["update.py"]
        self.assertEqual(next_version.next_version("1.2.3", [commit("feat!: new file format", code)]), "2.0.0")
        self.assertEqual(next_version.next_version("1.2.3", [commit("fix(api)!: drop the old path", code)]), "2.0.0")
        self.assertEqual(
            next_version.next_version("1.2.3", [commit("fix: rename", code, body="BREAKING CHANGE: drop v1 files")]),
            "2.0.0")

    def test_highest_bump_wins(self):
        commits = [
            commit("fix: typo", ["update.py"]),
            commit("feat: plans", ["vault.py"]),
            commit("docs: readme", ["README.md"]),
        ]
        self.assertEqual(next_version.next_version("1.4.5", commits), "1.5.0")
        commits.append(commit("feat!: replace the store", ["memory.py"]))
        self.assertEqual(next_version.next_version("1.4.5", commits), "2.0.0")

    def test_docs_and_ci_only_do_not_release(self):
        commits = [
            commit("Add screenshots", ["README.md", "docs/screenshots/chat.png"]),
            commit("docs: signing", ["SIGNING.md"]),
            commit("fix: release upload", [".github/workflows/release.yml"]),
        ]
        self.assertIsNone(next_version.next_version("1.0.0", commits))

    def test_docs_title_does_not_raise_a_code_fix(self):
        commits = [
            commit("Add screenshots", ["README.md", "docs/screenshots/chat.png"]),
            commit("fix: compare versions", ["update.py"]),
        ]
        self.assertEqual(next_version.next_version("1.0.0", commits), "1.0.1")

    def test_bump_commit_is_ignored(self):
        bot = commit("chore: release v1.0.1 [skip ci]", ["VERSION"],
                     author_name="github-actions[bot]",
                     author_email="41898282+github-actions[bot]@users.noreply.github.com")
        self.assertIsNone(next_version.next_version("1.0.1", [bot]))
        counted = commit("fix: crash", ["chatbox.py"])
        self.assertEqual(next_version.next_version("1.0.0", [counted, bot]), "1.0.1")
        skipped = commit("fix: hidden [skip ci]", ["chatbox.py"])
        self.assertIsNone(next_version.next_version("1.0.0", [skipped]))

    def test_merge_commit_uses_the_pull_request_title(self):
        merged = commit("Merge pull request #12 from Artemmie/feature", ["ollama.py"],
                        body="feat: local models\n\nAdds Ollama.")
        self.assertEqual(next_version.next_version("1.0.0", [merged]), "1.1.0")

    def test_latest_tag_is_numeric(self):
        self.assertEqual(next_version.select_latest_tag(["v1.0.9", "v1.0.10", "v1.2.0"]), "v1.2.0")
        self.assertEqual(next_version.select_latest_tag(["v1.0.10", "v1.0.9"]), "v1.0.10")
        self.assertEqual(next_version.select_latest_tag(["v1.10.0", "v1.0.10"]), "v1.10.0")
        self.assertIsNone(next_version.select_latest_tag(["video", "v1.0.0-rc1", "v1.0"]))
        self.assertEqual(next_version.select_latest_tag(["video", "v1.0.0-rc1", "v1.0.0"]), "v1.0.0")
        self.assertIsNone(next_version.select_latest_tag([]))


def _git(repo, *args):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
                "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"})
    subprocess.check_call(["git", "-C", repo, *args], env=env, stdout=subprocess.DEVNULL)


class GitPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.check_call(["git", "init", "-q", "-b", "main", self.repo])
        _git(self.repo, "config", "user.email", "dev@example.com")
        _git(self.repo, "config", "user.name", "Dev")

    def _write(self, name, text="x\n"):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        _git(self.repo, "add", name)

    def test_tag_order_and_titles_from_git(self):
        self._write("README.md", "one\n")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "tag", "v1.0.9")
        self._write("chatbox.py", "print(1)\n")
        _git(self.repo, "commit", "-m", "feat: big")
        _git(self.repo, "tag", "v1.0.10")
        self._write("update.py", "print(2)\n")
        _git(self.repo, "commit", "-m", "fix: small")
        self.assertEqual(next_version.plan(self.repo), "1.0.11")

        self._write("docs/screenshots/chat.png", "png")
        _git(self.repo, "commit", "-m", "Add screenshots")
        # The fix is still the only code change after v1.0.10. Screenshots do not raise it.
        self.assertEqual(next_version.plan(self.repo), "1.0.11")

    def test_readme_only_prints_nothing(self):
        self._write("app.py", "print(1)\n")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "tag", "v1.0.0")
        self._write("README.md", "docs\n")
        _git(self.repo, "commit", "-m", "docs: intro")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = next_version.main(["--repo", self.repo])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_merge_message_from_git(self):
        self._write("app.py", "print(1)\n")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "tag", "v1.0.0")
        self._write("ollama.py", "print(2)\n")
        _git(self.repo, "commit", "-m", "Merge pull request #4 from a/b", "-m", "feat: local models")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            next_version.main(["--repo", self.repo])
        self.assertEqual(buf.getvalue().strip(), "1.1.0")


class WorkflowContractTests(unittest.TestCase):
    def test_version_workflow_calls_the_release_build(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, ".github", "workflows", "version.yml"), encoding="utf-8") as handle:
            version = handle.read()
        with open(os.path.join(root, ".github", "workflows", "release.yml"), encoding="utf-8") as handle:
            release = handle.read()
        self.assertIn("branches: [main]", version)
        self.assertIn("scripts/next_version.py", version)
        self.assertIn("github-actions[bot]", version)
        self.assertIn("[skip ci]", version)
        self.assertIn("contents: write", version)
        self.assertIn("actions: write", version)
        self.assertIn('gh workflow run release.yml --ref "v${ver}"', version)
        self.assertIn("workflow_dispatch", release)
        self.assertIn("generate-notes", release)
        self.assertIn("refs/tags/v", release)


if __name__ == "__main__":
    unittest.main()

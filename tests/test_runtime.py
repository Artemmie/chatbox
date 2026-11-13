"""Windows path and process rules, exercised on every operating system."""
import json
import os
import subprocess
import unittest

import runtime
import update


class DataDirTests(unittest.TestCase):
    def test_windows_uses_localappdata(self):
        path = runtime.default_data_dir(
            system="nt",
            environ={"LOCALAPPDATA": "C:/AppData/Local"},
            home="C:/Users/me")
        self.assertEqual(path.replace("\\", "/"), "C:/AppData/Local/Chatbox")

    def test_posix_data_dir_is_unchanged(self):
        path = runtime.default_data_dir(system="posix", environ={}, home="/home/me")
        self.assertEqual(path, "/home/me/.local/share/chatbox")

    def test_env_override_wins(self):
        path = runtime.default_data_dir(system="nt", environ={"CHATBOX_DATA": "D:/box"}, home="C:/Users/me")
        self.assertTrue(path.replace("\\", "/").endswith("D:/box") or path.endswith("D:/box"))


class ToolLookupTests(unittest.TestCase):
    def test_cmd_shim_is_found_in_a_folder(self):
        folder = self._dir()
        open(os.path.join(folder, "claude.cmd"), "w", encoding="utf-8").close()
        found = runtime.find_in_dirs("claude", [folder], (".cmd", ".exe", ""))
        self.assertTrue(found.endswith("claude.cmd"))

    def test_posix_codex_candidates_do_not_grow(self):
        # The Windows-only paths are appended only on Windows.
        candidates = runtime.codex_app_candidates("/home/me", {})
        if os.name != "nt":
            self.assertFalse(any(item.endswith(".exe") for item in candidates))
        self.assertIn("/Applications/ChatGPT.app/Contents/Resources/codex", candidates)

    def _dir(self):
        import tempfile
        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, True))
        return path


class ProcessTests(unittest.TestCase):
    def test_windows_kill_is_taskkill(self):
        seen = []

        def runner(cmd, **kwargs):
            seen.append(list(cmd))

            class Done:
                returncode = 0
            return Done()

        class Proc:
            pid = 99

            def wait(self, timeout=None):
                return 0

        runtime.kill_tree(Proc(), system="nt", runner=runner)
        self.assertEqual(seen[0][:4], ["taskkill", "/F", "/T", "/PID"])
        self.assertEqual(seen[0][4], "99")

    def test_posix_kill_uses_the_process_group(self):
        seen = []

        def killpg(pid, sig):
            seen.append((pid, sig))
            raise ProcessLookupError()

        class Proc:
            pid = 77

            def terminate(self):
                seen.append("term")

            def kill(self):
                seen.append("kill")

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("x", timeout)

        runtime.kill_tree(Proc(), system="posix", killpg=killpg)
        self.assertIn((77, 15), seen)
        self.assertIn((77, 9), seen)


class ReleaseAssetTests(unittest.TestCase):
    def test_asset_names(self):
        self.assertEqual(update.asset_name("Windows", "AMD64"), "Chatbox-Windows-x64-setup.exe")
        self.assertEqual(update.asset_name("Darwin", "arm64"), "Chatbox-macOS-arm64.dmg")
        self.assertEqual(update.asset_name("Linux", "x86_64"), "Chatbox-Linux-x86_64.AppImage")

    def test_packaged_update_links_the_file_and_does_not_apply_it(self):
        def opener(url):
            self.assertTrue(url.endswith("/releases/latest"))
            return json.dumps({"tag_name": "v1.2.0",
                                "html_url": "https://github.com/Artemmie/chatbox/releases/tag/v1.2.0"})

        found = update.check_release("v1.0.0", opener=opener, system="Linux", machine="x86_64")
        self.assertTrue(found["available"])
        self.assertFalse(found["can_apply"])
        self.assertTrue(found["download"])
        self.assertTrue(found["url"].endswith("/releases/download/v1.2.0/Chatbox-Linux-x86_64.AppImage"))

    def test_same_version_is_not_an_update(self):
        found = update.check_release("1.2.0", opener=lambda url: json.dumps({"tag_name": "v1.2.0"}),
                                     system="Windows")
        self.assertFalse(found["available"])

    def test_release_workflow_signs_only_when_secrets_exist(self):
        text = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "release.yml"),
                    encoding="utf-8").read()
        self.assertIn("workflow_dispatch", text)
        self.assertIn("refs/tags/v", text)
        for name in ("MACOS_CERT_P12_BASE64", "MACOS_CERT_PASSWORD", "MACOS_SIGN_IDENTITY",
                     "APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD",
                     "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                     "AZURE_TRUSTED_SIGNING_ENDPOINT", "AZURE_TRUSTED_SIGNING_ACCOUNT",
                     "AZURE_TRUSTED_SIGNING_PROFILE", "WINDOWS_CERT_PFX_BASE64", "WINDOWS_CERT_PASSWORD"):
            self.assertIn(name, text)
        self.assertIn("Building unsigned", text)
        script = open(os.path.join(os.path.dirname(__file__), "..", "packaging", "sign-macos.sh"),
                      encoding="utf-8").read()
        self.assertIn("No Mac signing secrets", script)
        self.assertIn("exit 0", script)


if __name__ == "__main__":
    unittest.main()

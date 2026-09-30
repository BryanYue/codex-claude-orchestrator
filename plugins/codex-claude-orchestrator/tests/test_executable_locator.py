"""Missing-CLI guidance must match what a Git or ZIP install can actually do."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import executable_locator  # noqa: E402


class MissingCliGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.empty = self.base / "empty-path"; self.empty.mkdir()
        self.env = {"HOME": str(self.base / "home"), "PATH": str(self.empty),
                    "CODEX_HOME": str(self.base / "codex")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_cli_action_does_not_recommend_the_zip_installer_unconditionally(self):
        found = executable_locator.discover_external_claude(environ=self.env)
        action = found["action"]
        self.assertIsNone(found["path"])
        self.assertIsNone(found["source"])
        self.assertTrue(action.startswith(executable_locator.NVM_ACTION))
        self.assertIn("set CLAUDE_BIN", action)
        self.assertNotIn("Persist it with Install.command", action)
        installer = action.index("Install.command --configure-claude-bin")
        qualifier = action.rfind("ZIP distribution", 0, installer)
        self.assertGreaterEqual(qualifier, 0, "installer persistence must be qualified as ZIP-only")
        self.assertIn("Git marketplace checkout uses CLAUDE_BIN", action)

    def test_git_checkout_has_no_zip_manifest_so_the_installer_path_is_not_offered_as_its_fix(self):
        repository = ROOT.parents[1]
        if (repository / "FILE-SHA256.json").exists():
            self.skipTest("running from an extracted ZIP distribution")
        action = executable_locator.discover_external_claude(environ=self.env)["action"]
        self.assertIn("FILE-SHA256.json", action)

    def test_guidance_change_does_not_alter_selection_order(self):
        tool = self.base / "bin" / "claude"; tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n"); tool.chmod(0o755)
        env = dict(self.env, PATH=str(tool.parent))
        found = executable_locator.discover_external_claude(environ=env)
        self.assertEqual((found["path"], found["source"], found["action"]), (str(tool), "PATH", None))
        env["CLAUDE_BIN"] = str(self.base / "missing-claude")
        explicit = executable_locator.discover_external_claude(environ=env)
        self.assertEqual((explicit["path"], explicit["source"]), (None, "CLAUDE_BIN"))

    def test_stale_saved_nvm_path_stays_selected_and_explains_its_recovery(self):
        version_root = self.base / "home/.nvm/versions/node/v22.0.0"
        saved = version_root / "bin" / "claude"; saved.parent.mkdir(parents=True)
        saved.write_text("#!/bin/sh\n"); saved.chmod(0o755)
        on_path = self.base / "bin" / "claude"; on_path.parent.mkdir()
        on_path.write_text("#!/bin/sh\n"); on_path.chmod(0o755)
        env = dict(self.env, PATH=str(on_path.parent))
        settings = executable_locator.configure_claude_bin(str(saved), env)
        self.assertEqual(executable_locator.discover_external_claude(environ=env)["path"], str(saved))
        shutil.rmtree(version_root)
        before = settings.read_bytes()
        found = executable_locator.discover_external_claude(environ=env)
        self.assertEqual((found["path"], found["source"], found["configured"], found["candidate"]),
                         (None, "plugin_settings", True, str(saved)), "a stale saved path must not fall back to PATH")
        action = found["action"]
        self.assertIn(str(settings), action)
        self.assertIn("set CLAUDE_BIN", action)
        self.assertIn("change only the claude_bin value", action)
        self.assertNotRegex(action, r"(?i)\b(delete|remove)\b", "recovery must not suggest discarding the settings file")
        installer = action.index("Install.command --configure-claude-bin")
        self.assertGreaterEqual(action.rfind("ZIP distribution", 0, installer), 0, "installer advice must stay ZIP-only")
        self.assertIn("Git marketplace checkout uses CLAUDE_BIN", action)
        self.assertEqual(settings.read_bytes(), before, "discovery must not rewrite the settings file")
        override = executable_locator.discover_external_claude(environ=dict(env, CLAUDE_BIN=str(on_path)))
        self.assertEqual((override["path"], override["source"]), (str(on_path), "CLAUDE_BIN"))


if __name__ == "__main__":
    unittest.main()

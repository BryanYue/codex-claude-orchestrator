"""Launcher and CLI discovery fixtures use a deliberately restricted PATH."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from executable_locator import NVM_ACTION, cli_environment, configure_claude_bin, discover_system_claude, locate_claude, settings_path  # noqa: E402
import cli_store  # noqa: E402
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge  # noqa: E402
import executable_locator  # noqa: E402


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"; self.home.mkdir()
        self.bin = self.home / ".local/bin"; self.bin.mkdir(parents=True)
        self.nvm_claude = self.home / ".nvm/versions/node/v22.0.0/bin/claude"
        self.nvm_claude.parent.mkdir(parents=True)
        self.nvm_target = self.home / ".nvm/versions/node/v22.0.0/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        self.nvm_target.parent.mkdir(parents=True)
        self.nvm_target.write_text("#!/usr/bin/env node\n// nvm fixture\n")
        self.nvm_target.chmod(0o755)
        self.nvm_claude.symlink_to(self.nvm_target)
        node = self.nvm_claude.parent / "node"
        node.write_text("""#!/bin/sh
shift
case "$1" in
  --version) echo 9.9.999 ;;
  --help) echo '-p --model --effort --output-format --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands' ;;
  auth) echo '{"loggedIn":true}' ;;
  *) echo "nvm-node-ok $1" ;;
esac
""")
        node.chmod(0o755)
        uv = self.bin / "uv"
        uv.write_text("#!/bin/sh\nprintf '{\\\"claude_bin\\\":\\\"%s\\\",\\\"uv_env\\\":\\\"%s\\\",\\\"path\\\":\\\"%s\\\"}\\n' \"$CLAUDE_BIN\" \"$UV_PROJECT_ENVIRONMENT\" \"$PATH\"\n")
        uv.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **overrides):
        value = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        value.update(overrides)
        return value

    def test_nvm_cli_needs_an_explicit_path_when_not_in_mcp_path(self):
        missing = locate_claude(self.env())
        self.assertIsNone(missing["path"])
        self.assertTrue(missing["action"].startswith(NVM_ACTION))
        found = locate_claude(self.env(CLAUDE_BIN=str(self.nvm_claude)))
        self.assertEqual(found["path"], str(self.nvm_claude))
        self.assertEqual(found["source"], "CLAUDE_BIN")
        invoked = subprocess.run([found["path"], "--version"], text=True, capture_output=True,
                                 env=cli_environment(found, self.env()), check=True)
        self.assertEqual(invoked.stdout.strip(), "9.9.999")

    def test_fixture_home_does_not_read_host_plugin_settings(self):
        host_home = self.base / "host-home"; host_home.mkdir()
        host_cli = host_home / "bin/claude"; host_cli.parent.mkdir()
        host_cli.write_text("#!/bin/sh\nexit 0\n"); host_cli.chmod(0o755)
        host_settings = host_home / ".codex/claude-orchestrator/settings.json"
        host_settings.parent.mkdir(parents=True)
        host_settings.write_text(json.dumps({"claude_bin": str(host_cli)}))
        fixture_environment = self.env()
        with patch.dict(os.environ, {"HOME": str(host_home)}, clear=False):
            found = locate_claude(fixture_environment)
        self.assertIsNone(found["path"])
        self.assertEqual(found["settings_path"], str(self.home / ".codex/claude-orchestrator/settings.json"))
        self.assertEqual(settings_path(self.env(CODEX_HOME="~/.fixture-codex")), self.home / ".fixture-codex/claude-orchestrator/settings.json")
        self.assertEqual(settings_path(self.env(CLAUDE_ORCHESTRATOR_SETTINGS_PATH="~/.fixture-settings.json")), self.home / ".fixture-settings.json")

    def test_nvm_cli_can_be_persisted_without_putting_nvm_on_path(self):
        environment = self.env(CODEX_HOME=str(self.base / ".codex"))
        saved = configure_claude_bin(str(self.nvm_claude), environment)
        self.assertEqual(saved, settings_path(environment))
        self.assertEqual(cli_store.get_selection(environment)["mode"], "external")
        found = locate_claude(environment)
        self.assertEqual(found["path"], str(self.nvm_claude))
        self.assertEqual(found["source"], "plugin_settings")

    def test_bridge_uses_selected_nvm_symlink_parent_for_every_preflight_command(self):
        environment = self.env(CLAUDE_BIN=str(self.nvm_claude))
        profiles = {"9.9.999": {"tested": True, "capabilities": {}}}
        with patch.dict(os.environ, environment, clear=True):
            report = bridge.check_environment(self.base, profiles=profiles)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["cli"]["path"], str(self.nvm_claude))
        self.assertEqual(report["cli"]["source"], "CLAUDE_BIN")

    def test_bridge_preflight_reads_persisted_nvm_symlink_without_process_claude_bin(self):
        environment = self.env(CODEX_HOME=str(self.base / ".codex"))
        configure_claude_bin(str(self.nvm_claude), environment)
        profiles = {"9.9.999": {"tested": True, "capabilities": {}}}
        with patch.dict(os.environ, environment, clear=True):
            report = bridge.check_environment(self.base, profiles=profiles)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["cli"]["path"], str(self.nvm_claude))
        self.assertEqual(report["cli"]["source"], "plugin_settings")

    def test_launch_preserves_explicit_nvm_path_without_shell_initialization(self):
        proc = subprocess.run(["bash", str(ROOT / "scripts/launch.sh")], text=True, capture_output=True,
                              env=self.env(CLAUDE_BIN=str(self.nvm_claude)), check=True)
        observed = json.loads(proc.stdout)
        self.assertEqual(observed["claude_bin"], str(self.nvm_claude))
        self.assertTrue(observed["path"].startswith(str(self.nvm_claude.parent) + ":"))
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertTrue(observed["uv_env"].endswith("/venvs/" + manifest["version"]))

    def test_install_command_uses_uv_and_the_complete_manifest_version(self):
        proc = subprocess.run(["bash", str(ROOT.parent.parent / "Install.command"), "--check"], text=True,
                              capture_output=True, env=self.env(), check=True)
        observed = json.loads(proc.stdout)
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertTrue(observed["uv_env"].endswith("/venvs/" + manifest["version"]))
        self.assertNotIn("python3", (ROOT.parent.parent / "Install.command").read_text())

    def test_managed_selection_wins_over_mutable_claude_bin_and_disables_only_its_updater(self):
        managed = {"id": "darwin-arm64-2.1.278-deadbeef", "path": str(self.base / "private/claude"),
                   "version": "2.1.278", "sha256": "d" * 64, "platform": "darwin", "machine": "arm64",
                   "source": "/Users/example/.local/share/claude/versions/2.1.278", "created_at": 1.0,
                   "generation": 4, "selection_reason": "active", "qualification": {"source": "local_qualification"}}
        with patch.object(executable_locator.cli_store, "get_selection", return_value={"mode": "managed", "active": managed["id"], "generation": 4}), \
                patch.object(executable_locator, "_bridge_contract_id", return_value="bridge-contract-test"), \
                patch.object(executable_locator.cli_store, "select", return_value=managed):
            found = executable_locator.locate_claude(self.env(CLAUDE_BIN=str(self.nvm_claude)))
        self.assertEqual(found["source"], "managed_native")
        self.assertEqual(found["path"], managed["path"])
        self.assertEqual(found["identity"]["id"], managed["id"])
        child = cli_environment(found, self.env(CLAUDE_BIN=str(self.nvm_claude)))
        self.assertEqual(child["DISABLE_AUTOUPDATER"], "1")

    def test_unqualified_managed_selection_does_not_fall_back_to_path_or_claude_bin(self):
        identity_id = "darwin-arm64-2.1.279-deadbeef"
        with patch.object(executable_locator.cli_store, "get_selection", return_value={"mode": "managed", "active": identity_id, "generation": 5}), \
                patch.object(executable_locator, "_bridge_contract_id", return_value="bridge-contract-test"), \
                patch.object(executable_locator.cli_store, "select", return_value=None):
            found = executable_locator.locate_claude(self.env(CLAUDE_BIN=str(self.nvm_claude)))
        self.assertEqual(found["source"], "managed_native")
        self.assertIsNone(found["path"])
        self.assertEqual(found["candidate"], identity_id)
        self.assertIn("no retained qualification", found["action"])

    def test_system_discovery_ignores_plugin_settings_and_managed_selection(self):
        system = self.base / "system-bin/claude"; system.parent.mkdir()
        system.write_text("#!/bin/sh\nexit 0\n"); system.chmod(0o755)
        environment = self.env(CODEX_HOME=str(self.base / ".codex"), PATH=str(system.parent))
        configure_claude_bin(str(self.nvm_claude), environment)
        found = discover_system_claude(environment)
        self.assertEqual(found["source"], "PATH")
        self.assertEqual(found["path"], str(system))

    def test_qualification_child_disables_auto_update_only_when_identity_is_bound(self):
        descriptor = {"source": "qualification", "path": str(self.nvm_claude), "identity": {"id": "candidate"}}
        qualified = cli_environment(descriptor, self.env())
        self.assertEqual(qualified["DISABLE_AUTOUPDATER"], "1")
        unbound = cli_environment({"source": "qualification", "path": str(self.nvm_claude)}, self.env())
        self.assertNotIn("DISABLE_AUTOUPDATER", unbound)


if __name__ == "__main__":
    unittest.main()

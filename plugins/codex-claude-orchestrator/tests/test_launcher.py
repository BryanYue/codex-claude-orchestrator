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
from executable_locator import NVM_ACTION, cli_environment, configure_claude_bin, locate_claude, settings_path  # noqa: E402
import cli_store  # noqa: E402
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge  # noqa: E402


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
  --help) echo '-p --model --effort --output-format --verbose --json-schema --session-id --resume --permission-mode --tools --allowedTools --disallowedTools --settings --strict-mcp-config --mcp-config --disable-slash-commands --no-session-persistence' ;;
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
        self.assertFalse(cli_store.store_root(environment).exists(),
                         "configuring the local CLI must not create or change retired managed state")
        found = locate_claude(environment)
        self.assertEqual(found["path"], str(self.nvm_claude))
        self.assertEqual(found["source"], "plugin_settings")

    def test_bridge_uses_selected_nvm_symlink_parent_for_every_preflight_command(self):
        environment = self.env(CLAUDE_BIN=str(self.nvm_claude))
        with patch.dict(os.environ, environment, clear=True):
            report = bridge.check_environment(self.base)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["cli"]["version"], "9.9.999")
        self.assertEqual(report["cli"]["path"], str(self.nvm_claude))
        self.assertEqual(report["cli"]["source"], "CLAUDE_BIN")

    def test_bridge_preflight_reads_persisted_nvm_symlink_without_process_claude_bin(self):
        environment = self.env(CODEX_HOME=str(self.base / ".codex"))
        configure_claude_bin(str(self.nvm_claude), environment)
        with patch.dict(os.environ, environment, clear=True):
            report = bridge.check_environment(self.base)
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

    def test_launch_keeps_caller_path_ahead_of_fallback_directories(self):
        caller = self.base / "caller-bin"; caller.mkdir()
        for directory, label in ((caller, "caller"), (self.bin, "fallback")):
            claude = directory / "claude"
            claude.write_text(f"#!/bin/sh\necho {label}\n"); claude.chmod(0o755)
        uv = caller / "uv"
        uv.write_text("#!/bin/sh\nprintf '{\"uv\":\"caller\",\"claude\":\"%s\",\"path\":\"%s\"}\\n' \"$(claude)\" \"$PATH\"\n")
        uv.chmod(0o755)
        caller_path = f"{caller}:/usr/bin:/bin"
        proc = subprocess.run(["bash", str(ROOT / "scripts/launch.sh")], text=True, capture_output=True,
                              env=self.env(PATH=caller_path), check=True)
        observed = json.loads(proc.stdout)
        self.assertEqual(observed["uv"], "caller")
        self.assertEqual(observed["claude"], "caller")
        self.assertTrue(observed["path"].startswith(caller_path + ":"))
        self.assertIn(str(self.bin), observed["path"].split(":"))

    def test_install_command_uses_uv_and_the_complete_manifest_version(self):
        proc = subprocess.run(["bash", str(ROOT.parent.parent / "Install.command"), "--check"], text=True,
                              capture_output=True, env=self.env(), check=True)
        observed = json.loads(proc.stdout)
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertTrue(observed["uv_env"].endswith("/venvs/" + manifest["version"]))
        self.assertNotIn("python3", (ROOT.parent.parent / "Install.command").read_text())

    def _write_fake_uv(self, script: str) -> Path:
        # The fixture PATH is /usr/bin:/bin, so launch.sh reaches this uv through
        # its $HOME/.local/bin fallback before any Homebrew or /usr/local copy.
        uv = self.bin / "uv"
        uv.write_text(script)
        uv.chmod(0o755)
        return uv

    def test_launch_prepare_dependencies_runs_uv_sync_without_starting_server(self):
        call_log = self.base / "uv-calls.log"
        self._write_fake_uv(
            "#!/bin/sh\n"
            f"echo \"$@\" >> {str(call_log)!r}\n"
            "if [ \"$1\" = \"sync\" ]; then exit 0; fi\n"
            "echo 'server should not have started' >&2\n"
            "exit 1\n"
        )
        proc = subprocess.run(["bash", str(ROOT / "scripts/launch.sh"), "--prepare-dependencies"],
                              text=True, capture_output=True, env=self.env(), check=True)
        observed = json.loads(proc.stdout.strip())
        self.assertEqual(observed["status"], "ready")
        calls = call_log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("sync --project"))
        self.assertIn("--frozen", calls[0])
        self.assertIn("--no-dev", calls[0])

    def test_launch_prepare_dependencies_propagates_sync_failure_without_ready_status(self):
        self._write_fake_uv("#!/bin/sh\nif [ \"$1\" = \"sync\" ]; then exit 42; fi\nexit 1\n")
        proc = subprocess.run(["bash", str(ROOT / "scripts/launch.sh"), "--prepare-dependencies"],
                              text=True, capture_output=True, env=self.env())
        self.assertEqual(proc.returncode, 42)
        self.assertNotIn('"status"', proc.stdout)

    def test_launch_without_prepare_flag_still_starts_server_via_uv_run(self):
        self._write_fake_uv(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"sync\" ]; then echo 'sync should not run for a normal launch' >&2; exit 1; fi\n"
            "printf '{\"argv1\":\"%s\",\"uv_env\":\"%s\"}\\n' \"$1\" \"$UV_PROJECT_ENVIRONMENT\"\n"
        )
        proc = subprocess.run(["bash", str(ROOT / "scripts/launch.sh")],
                              text=True, capture_output=True, env=self.env(), check=True)
        observed = json.loads(proc.stdout.strip())
        self.assertEqual(observed["argv1"], "run")

    def legacy_managed_store(self) -> Path:
        store = self.base / "legacy-cli-store"
        private = store / "versions" / "darwin-arm64-2.1.278-deadbeefdeadbeefdead"
        private.mkdir(parents=True)
        (private / "claude").write_text("#!/bin/sh\necho private-copy\n")
        (private / "claude").chmod(0o700)
        (store / "selection.json").write_text(json.dumps({
            "schema_version": 1, "generation": 4, "active": private.name, "previous": None,
            "history": [private.name], "mode": "managed"}))
        return store

    def test_legacy_managed_selection_never_overrides_the_local_cli(self):
        store = self.legacy_managed_store()
        environment = self.env(CLAUDE_BIN=str(self.nvm_claude), CLAUDE_ORCHESTRATOR_CLI_ROOT=str(store))
        found = locate_claude(environment)
        self.assertEqual(found["source"], "CLAUDE_BIN")
        self.assertEqual(found["path"], str(self.nvm_claude))
        self.assertNotIn("identity", found)
        self.assertTrue((store / "selection.json").is_file(), "history is preserved, not deleted")

    def test_legacy_managed_selection_does_not_block_or_replace_missing_local_cli(self):
        store = self.legacy_managed_store()
        found = locate_claude(self.env(CLAUDE_ORCHESTRATOR_CLI_ROOT=str(store)))
        self.assertIsNone(found["path"])
        self.assertIsNone(found["source"])
        self.assertTrue(found["action"].startswith(NVM_ACTION))

    def test_discovery_precedence_is_claude_bin_then_settings_then_path(self):
        system = self.base / "system-bin/claude"; system.parent.mkdir()
        system.write_text("#!/bin/sh\nexit 0\n"); system.chmod(0o755)
        environment = self.env(CODEX_HOME=str(self.base / ".codex"), PATH=str(system.parent))
        self.assertEqual(locate_claude(environment)["path"], str(system))
        configure_claude_bin(str(self.nvm_claude), environment)
        self.assertEqual(locate_claude(environment)["source"], "plugin_settings")
        override = {**environment, "CLAUDE_BIN": str(system)}
        self.assertEqual(locate_claude(override)["source"], "CLAUDE_BIN")
        self.assertEqual(locate_claude(override)["path"], str(system))

    def test_child_environment_never_changes_the_users_update_setting(self):
        for decision in ({"source": "CLAUDE_BIN", "path": str(self.nvm_claude)},
                         {"source": "managed_native", "path": str(self.nvm_claude), "identity": {"id": "legacy"}},
                         {"source": "qualification", "path": str(self.nvm_claude), "identity": {"id": "candidate"}}):
            with self.subTest(source=decision["source"]):
                child = cli_environment(decision, self.env())
                self.assertNotIn("DISABLE_AUTOUPDATER", child)
                self.assertTrue(child["PATH"].startswith(str(self.nvm_claude.parent) + ":"))


if __name__ == "__main__":
    unittest.main()

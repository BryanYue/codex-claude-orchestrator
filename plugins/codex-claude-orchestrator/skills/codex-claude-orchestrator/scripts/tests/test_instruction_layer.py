import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import instruction_layer  # noqa: E402


class InstructionLayerTests(unittest.TestCase):
    def test_lists_user_project_ancestor_files_and_hooks_without_reading_into_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "config"
            (config / "rules").mkdir(parents=True)
            (config / "CLAUDE.md").write_text("全局规则")
            (config / "rules" / "git.md").write_text("提交规范")
            (config / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "~/.claude/hooks/stop-gate.sh"}]}]}}))
            project = root / "work" / "repo"
            (project / ".claude").mkdir(parents=True)
            (root / "work" / "CLAUDE.md").write_text("上级目录规则")
            (project / "CLAUDE.md").write_text("项目规则")
            (project / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": "guard.sh"}]}]}}))
            layer = instruction_layer.scan(project, {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(root / "home")})
        scopes = {Path(entry["path"]).relative_to(root).as_posix(): entry["scope"] for entry in layer["files"]}
        self.assertEqual(scopes, {"config/CLAUDE.md": "user", "config/rules/git.md": "user_rules",
                                  "work/repo/CLAUDE.md": "project", "work/CLAUDE.md": "ancestor"})
        self.assertTrue(all(len(entry["sha256"]) == 64 for entry in layer["files"]))
        hooks = {(hook["source"], hook["event"], hook["command"]) for hook in layer["hooks"]}
        self.assertEqual(hooks, {("user", "Stop", "~/.claude/hooks/stop-gate.sh"), ("project_local", "PreToolUse", "guard.sh")})


if __name__ == "__main__":
    unittest.main()

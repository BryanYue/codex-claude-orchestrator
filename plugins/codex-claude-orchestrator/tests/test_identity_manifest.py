"""One declaration drives separate contract and startup identities."""
import ast
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(PLUGIN / "skills/codex-claude-orchestrator/scripts"))
sys.path.insert(0, str(REPO / "tools"))
import identity_manifest
import startup_protocol
import compatibility
import build_distribution as builder


class IdentityManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "plugin"
        self.root.mkdir()
        self.declaration = json.loads((PLUGIN / identity_manifest.MANIFEST_FILE).read_bytes())
        for relative in set().union(*(identity_manifest.declared_paths(json.dumps(self.declaration).encode(), purpose)
                              for purpose in identity_manifest.PURPOSES)):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PLUGIN / relative, target)

    def blobs(self):
        return {builder.PLUGIN_RELATIVE / relative: (self.root / relative).read_bytes()
                for relative in set().union(*(identity_manifest.declared_paths(json.dumps(self.declaration).encode(), purpose)
                              for purpose in identity_manifest.PURPOSES))}

    def contract(self):
        return identity_manifest.digest(identity_manifest.file_hashes(self.root, "contract"), "contract")

    def rewrite(self):
        (self.root / identity_manifest.MANIFEST_FILE).write_text(json.dumps(self.declaration))

    def test_builder_and_runtime_use_the_same_contract_declaration_and_algorithm(self):
        self.assertEqual(builder.contract_digest(self.blobs()), self.contract())
        self.assertEqual(compatibility.bridge_contract_id(), self.contract())
        startup = startup_protocol.code_identity(self.root)
        self.assertEqual(startup["value"], identity_manifest.digest(identity_manifest.file_hashes(self.root, "startup"), "startup"))
        self.assertNotEqual(startup["value"], self.contract(), "the persisted purpose formats remain separate")

    def test_missing_declared_module_fails_in_builder_contract_and_startup(self):
        relative = "skills/codex-claude-orchestrator/scripts/protocol.py"
        blobs = self.blobs()
        del blobs[builder.PLUGIN_RELATIVE / relative]
        with self.assertRaises(builder.BuildError):
            builder.contract_digest(blobs)
        (self.root / relative).unlink()
        with self.assertRaises(FileNotFoundError):
            self.contract()
        startup = startup_protocol.code_identity(self.root)
        self.assertIsNone(startup["value"])
        self.assertIn(relative, startup["error"])

    def test_added_dependency_and_declaration_change_both_purpose_digests(self):
        before_contract = self.contract()
        before_startup = startup_protocol.code_identity(self.root)["value"]
        relative = "scripts/new_dependency.py"
        (self.root / relative).write_bytes(b"# new dependency\n")
        self.declaration["purposes"]["contract"].append(relative)
        self.rewrite()
        after_contract = self.contract()
        after_startup = startup_protocol.code_identity(self.root)["value"]
        self.assertNotEqual(after_contract, before_contract)
        self.assertNotEqual(after_startup, before_startup)
        self.assertEqual(builder.contract_digest(self.blobs()), after_contract)
        (self.root / relative).write_bytes(b"# updated dependency\n")
        self.assertNotEqual(self.contract(), after_contract)
        self.assertNotEqual(startup_protocol.code_identity(self.root)["value"], after_startup)
        self.assertEqual(builder.contract_digest(self.blobs()), self.contract())

    def test_startup_only_dependency_stays_outside_contract_but_is_required_by_builder(self):
        relative = "scripts/startup_only.py"
        self.declaration["purposes"]["startup"] = [*self.declaration["purposes"]["contract"], relative]
        self.rewrite()
        dependency = self.root / relative
        dependency.write_bytes(b"# first startup implementation\n")
        contract = self.contract()
        startup = startup_protocol.code_identity(self.root)["value"]
        dependency.write_bytes(b"# changed startup implementation\n")
        self.assertEqual(self.contract(), contract)
        self.assertNotEqual(startup_protocol.code_identity(self.root)["value"], startup)
        blobs = self.blobs()
        del blobs[builder.PLUGIN_RELATIVE / relative]
        with self.assertRaises(builder.BuildError):
            builder.contract_digest(blobs)
        dependency.unlink()
        self.assertIsNone(startup_protocol.code_identity(self.root)["value"])

    def test_manifest_bytes_are_hashed_even_when_paths_do_not_change(self):
        contract = self.contract()
        startup = startup_protocol.code_identity(self.root)["value"]
        self.declaration["note"] = "declaration changed"
        self.rewrite()
        self.assertNotEqual(self.contract(), contract)
        self.assertNotEqual(startup_protocol.code_identity(self.root)["value"], startup)

    def test_declaration_cannot_omit_itself_or_its_reader_or_escape_the_package(self):
        for relative in ("code-identity.json", "scripts/identity_manifest.py"):
            declaration = json.loads(json.dumps(self.declaration))
            declaration["purposes"]["contract"].remove(relative)
            with self.assertRaises(ValueError):
                identity_manifest.declared_paths(json.dumps(declaration).encode(), "contract")
            blobs = self.blobs()
            blobs[builder.CODE_IDENTITY_RELATIVE] = json.dumps(declaration).encode()
            with self.assertRaises(builder.BuildError):
                builder.contract_digest(blobs)
        for bad in ("../outside.py", "/outside.py", "scripts/../outside.py", "scripts//outside.py"):
            declaration = json.loads(json.dumps(self.declaration))
            declaration["purposes"]["contract"].append(bad)
            with self.assertRaises(ValueError):
                identity_manifest.declared_paths(json.dumps(declaration).encode(), "contract")

    def test_alias_uses_same_paths_and_rejects_cycles_and_unknown_targets(self):
        raw = json.dumps(self.declaration).encode()
        self.assertEqual(identity_manifest.declared_paths(raw, "startup"), identity_manifest.declared_paths(raw, "contract"))
        for aliases in ({"contract": "startup", "startup": "contract"}, {"contract": "contract"},
                        {"startup": "unrecognized", "contract": self.declaration["purposes"]["contract"]}):
            raw = json.dumps({"schema_version": 1, "purposes": aliases}).encode()
            with self.assertRaises(ValueError):
                identity_manifest.declared_paths(raw, "startup")
            blobs = self.blobs()
            blobs[builder.CODE_IDENTITY_RELATIVE] = raw
            with self.assertRaises(builder.BuildError):
                builder.contract_digest(blobs)

    def test_imported_plugin_dependencies_are_declared_for_each_purpose(self):
        modules = {path.stem: path.relative_to(PLUGIN).as_posix()
                   for folder in (PLUGIN / "scripts", PLUGIN / "skills/codex-claude-orchestrator/scripts")
                   for path in folder.glob("*.py")}
        for purpose in ("contract", "startup"):
            paths = set(identity_manifest.paths(PLUGIN, purpose))
            for relative in paths:
                if not relative.endswith(".py"):
                    continue
                tree = ast.parse((PLUGIN / relative).read_bytes())
                for node in ast.walk(tree):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                        [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                    for name in names:
                        module = name.split(".")[0]
                        if module in modules:
                            with self.subTest(purpose=purpose, source=relative, imported=module):
                                self.assertIn(modules[module], paths)


if __name__ == "__main__":
    unittest.main()

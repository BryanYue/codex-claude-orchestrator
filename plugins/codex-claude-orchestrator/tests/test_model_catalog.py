import json
import os
import signal
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_catalog


class ModelCatalogTests(unittest.TestCase):
    def fixture(self, root, behavior):
        path = root / "claude"
        path.write_text("#!/usr/bin/env python3\nimport sys,json,time\n" + behavior)
        path.chmod(0o700)
        return {"path": str(path), "source": "managed_native", "identity": {"id": "fixture", "version": "9.0.0"}}

    def test_dynamic_new_category_no_user_prompt_or_account_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = self.fixture(root, """
r=json.loads(sys.stdin.read())
assert r['type']=='control_request' and r['request']['subtype']=='initialize'
assert '--no-session-persistence' in sys.argv and sys.argv[sys.argv.index('--tools')+1]==''
assert '--model' not in sys.argv and '--dangerously-skip-permissions' not in sys.argv
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':r['request_id'],'response':{'account':{'secret':'PRIVATE_ACCOUNT'},'models':[{'value':'future-family','resolvedModel':'provider-future-99','displayName':'Future','secret':'PRIVATE_MODEL','supportedEffortLevels':['low','future-effort']}]}}}))
""")
            with patch.object(model_catalog, "locate_claude", return_value=decision):
                result = model_catalog.collect(str(root))
            self.assertEqual(result["status"], "advertised")
            self.assertEqual(result["models"][0]["resolvedModel"], "provider-future-99")
            self.assertEqual(result["models"][0]["supportedEffortLevels"], ["low", "future-effort"])
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertFalse(result["remote_invocation_verified"])
            self.assertFalse(result["model_request_sent"])

    def test_wrong_response_and_cli_error_do_not_invent_catalog(self):
        for script in ["print('{}')", "sys.stderr.write('PRIVATE_ACCOUNT');sys.exit(1)"]:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                decision = self.fixture(root, script)
                with patch.object(model_catalog, "locate_claude", return_value=decision):
                    result = model_catalog.collect(str(root))
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["models"], [])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_timeout_cleans_up_owned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = self.fixture(root, "import os,pathlib\npathlib.Path('pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
            with patch.object(model_catalog, "locate_claude", return_value=decision):
                result = model_catalog.collect(str(root), timeout=0.5)
            self.assertEqual(result["reason"], "model_catalog_timeout")
            with self.assertRaises(ProcessLookupError):
                os.kill(int((root / "pid").read_text()), 0)

    def test_malformed_metadata_is_not_accepted(self):
        for value in [None, [], [{"value": ""}], [{"value": "opus"}, {"value": "opus"}]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                model_catalog._models(value)

    def test_cleanup_permission_error_is_structured_and_does_not_claim_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = self.fixture(root, "time.sleep(60)")
            proc = Mock(pid=12345, args=["fixture"])
            proc.poll.return_value = None
            with patch.object(model_catalog, "locate_claude", return_value=decision), \
                 patch.object(model_catalog.subprocess, "Popen", return_value=proc), \
                 patch.object(model_catalog, "_exchange", side_effect=subprocess.TimeoutExpired("fixture", 1)), \
                 patch.object(model_catalog.os, "killpg", side_effect=PermissionError("PRIVATE_ACCOUNT")):
                result = model_catalog.collect(str(root), timeout=1)
            self.assertEqual(result["reason"], "model_catalog_timeout")
            self.assertEqual(result["cleanup_status"], "unconfirmed")
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_both_output_streams_are_bounded_before_process_exit(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                decision = self.fixture(root, f"import os,pathlib\npathlib.Path('pid').write_text(str(os.getpid()))\nwhile True: sys.{stream}.write('X'*65536); sys.{stream}.flush()\n")
                with patch.object(model_catalog, "locate_claude", return_value=decision):
                    started = time.monotonic()
                    result = model_catalog.collect(str(root), timeout=10)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(result["reason"], "model_catalog_unavailable")
                self.assertEqual(result["cleanup_status"], "confirmed")
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((root / "pid").read_text()), 0)

    def test_timeout_after_parent_exit_still_stops_descendant_holding_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = self.fixture(root, "import subprocess,os,pathlib\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\npathlib.Path('child-pid').write_text(str(child.pid))\ntime.sleep(.1)\n")
            child_pid = None
            try:
                with patch.object(model_catalog, "locate_claude", return_value=decision):
                    result = model_catalog.collect(str(root), timeout=0.5)
                child_pid = int((root / "child-pid").read_text())
                self.assertEqual(result["reason"], "model_catalog_timeout")
                for _ in range(50):
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        child_pid = None
                        break
                    time.sleep(.02)
                else:
                    self.fail("catalog descendant survived after its parent exited")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()

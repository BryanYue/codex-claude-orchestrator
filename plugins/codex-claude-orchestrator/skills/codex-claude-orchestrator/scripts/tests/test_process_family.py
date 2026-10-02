"""Reproduce detached writers and verify that stale PID evidence cannot signal."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import process_family as family  # noqa: E402


class ProcessFamilyTests(unittest.TestCase):
    def new_family(self):
        with patch.object(family, "_pids", return_value=[]):
            return family.Family("fixture-unique-marker")

    def test_reused_pid_is_never_signalled_using_prior_birth(self):
        tracked = self.new_family()
        previous = family.Identity(12345, (100, 2), os.getuid())
        replacement = family.Identity(12345, (101, 1), os.getuid())
        tracked._tracked[previous.pid] = previous
        tracked._live[previous.pid] = previous
        with patch.object(family, "_identity", return_value=replacement), patch.object(family.os, "kill") as kill:
            tracked._signal(signal.SIGKILL)
        kill.assert_not_called()

    def test_observed_identity_survives_marker_removal_without_adopting_unrelated(self):
        tracked = self.new_family()
        owned = family.Identity(12345, (100, 2), os.getuid())
        unrelated = family.Identity(12346, (100, 3), os.getuid())
        identities = {owned.pid: owned, unrelated.pid: unrelated}
        with patch.object(family, "_pids", return_value=list(identities)), \
             patch.object(family, "_identity", side_effect=identities.get), \
             patch.object(family, "_environment", side_effect=lambda pid: [tracked._entry] if pid == owned.pid else []):
            self.assertEqual(tracked.observe()["remaining_pids"], [owned.pid])
        with patch.object(family, "_pids", return_value=list(identities)), \
             patch.object(family, "_identity", side_effect=identities.get), \
             patch.object(family, "_environment", return_value=[]), patch.object(family.os, "kill") as kill:
            self.assertEqual(tracked.observe()["remaining_pids"], [owned.pid])
            tracked._signal(signal.SIGTERM)
        kill.assert_called_once_with(owned.pid, signal.SIGTERM)

    def test_uninspectable_tracked_process_remains_unknown_and_cannot_signal(self):
        tracked = self.new_family()
        owned = family.Identity(12345, (100, 2), os.getuid())
        tracked._tracked[owned.pid] = owned
        with patch.object(family, "_pids", return_value=[owned.pid]), \
             patch.object(family, "_identity", side_effect=family.InspectionError("identity_unavailable")), \
             patch.object(family.os, "kill") as kill:
            result = tracked.finish(grace_seconds=0)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("tracked_identity_unavailable", result["blockers"])
        self.assertEqual(result["live_count"], 1)
        kill.assert_not_called()

    def test_unidentified_inaccessible_process_is_never_selected_and_limits_evidence(self):
        tracked = self.new_family()
        unrelated = family.Identity(12345, (100, 2), os.getuid())
        with patch.object(family, "_pids", return_value=[unrelated.pid]), \
             patch.object(family, "_identity", return_value=unrelated), \
             patch.object(family, "_environment", side_effect=family.InspectionError("environment_unavailable")), \
             patch.object(family.os, "kill") as kill:
            result = tracked.finish(grace_seconds=0)
        self.assertEqual(result["inspection_unavailable_count"], 1)
        self.assertEqual(result["tracked_count"], 0)
        self.assertIn("inaccessible unidentified", result["limitation"])
        kill.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "native process table required")
    def test_reopened_marker_observes_already_running_child_without_stopping_it(self):
        marker = f"late-fixture-{os.getpid()}-{time.monotonic_ns()}"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                 env={**os.environ, family.ENVIRONMENT_KEY: marker}, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            reopened = family.Family(marker=marker)
            observed = reopened.observe()
            self.assertIn(child.pid, observed["remaining_pids"], observed)
            self.assertIsNone(child.poll(), "read-only observation stopped the existing writer")
            stopped = reopened.finish(grace_seconds=.1)
            self.assertEqual(stopped["state"], "stopped", stopped)
            child.wait(timeout=4)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=4)

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "native process table required")
    def test_fork_setsid_closed_pipes_writer_is_found_and_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "detached.pid"
            tracked = family.Family()
            script = """import os,signal,sys,time
pid=os.fork()
if pid:
    raise SystemExit(0)
os.setsid()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], 'w') as stream:
    stream.write(str(os.getpid()))
for descriptor in (0,1,2):
    os.close(descriptor)
time.sleep(20)
"""
            leader = subprocess.Popen([sys.executable, "-c", script, str(pidfile)],
                                      env={**os.environ, **tracked.environment}, start_new_session=True,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child_identity = None
            try:
                leader.communicate(timeout=4)
                deadline = time.monotonic() + 4
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(pidfile.exists(), "detached child did not publish its fixture PID")
                child = int(pidfile.read_text())
                child_identity = family._identity(child)
                self.assertIsNotNone(child_identity)
                self.assertNotEqual(os.getpgid(child), leader.pid)
                observed = tracked.observe()
                self.assertIn(child, observed["remaining_pids"], observed)
                stopped = tracked.finish(grace_seconds=.1)
                self.assertEqual(stopped["state"], "stopped", stopped)
                current = family._identity(child)
                self.assertTrue(current is None or current.zombie, "detached writer is still live")
            finally:
                if leader.poll() is None:
                    leader.kill()
                    leader.communicate(timeout=4)
                if child_identity is not None:
                    current = family._identity(child_identity.pid)
                    if current is not None and current.birth == child_identity.birth and not current.zombie:
                        os.kill(current.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()

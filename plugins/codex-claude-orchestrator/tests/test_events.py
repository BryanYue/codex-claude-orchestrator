import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "codex-claude-orchestrator" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import events  # noqa: E402


class EventIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "run"

    def tearDown(self):
        self.temp.cleanup()

    def test_child_log_links_never_redirect_supervisor_write(self):
        self.run.mkdir()
        victim = self.run.parent / "victim"
        victim.write_bytes(b"original source")
        log = self.run / "activity.jsonl"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    log.symlink_to(victim)
                elif kind == "hardlink":
                    os.link(victim, log)
                else:
                    os.mkfifo(log)
                try:
                    with self.assertRaises(OSError):
                        events.append(self.run, "decision", "do not redirect")
                    self.assertEqual(victim.read_bytes(), b"original source")
                finally:
                    log.unlink()

    def test_meaningful_activity_survives_heartbeats_and_legacy_index_read_only(self):
        milestone = events.append(self.run, "tool", "tool path permitted", tool="Read", file="README.md")
        events.append(self.run, "provider", "thinking_tokens", status="thinking_tokens")
        index_path = self.run / events.INDEX_NAME
        legacy = json.loads(index_path.read_text())
        legacy["schema"] = 1
        legacy.pop("last_meaningful_event")
        index_path.write_text(json.dumps(legacy))
        evidence = index_path.read_bytes()
        with patch.object(events, "_scan", wraps=events._scan) as scan:
            self.assertEqual(events.latest_meaningful(self.run), milestone)
            self.assertEqual(events.statistics(self.run)[0], 2)
            self.assertEqual(scan.call_count, 1)
        self.assertEqual(index_path.read_bytes(), evidence)
        self.assertEqual(len(events.read(self.run)[0]), 2)
        decision = events.append(self.run, "decision", "coordinator decision recorded", status="returned")
        self.assertEqual(events.latest_meaningful(self.run), decision)

    def test_checkpointed_cursor_pages_every_event_once(self):
        for number in range(300):
            self.assertEqual(events.append(self.run, "test", f"event {number}")["seq"], number + 1)
        cursor, seen = 0, []
        while True:
            page, cursor, more = events.read(self.run, after=cursor, limit=37)
            seen.extend(event["seq"] for event in page)
            if not more:
                break
        self.assertEqual(seen, list(range(1, 301)))
        self.assertEqual(events.statistics(self.run)[0], 300)
        self.assertEqual(events.latest(self.run), 300)
        self.assertEqual(events.read(self.run, after=300, limit=20), ([], 300, False))

    def test_missing_index_and_partial_crash_tail_rebuild_without_losing_complete_events(self):
        first = events.append(self.run, "test", "first")
        with (self.run / "activity.jsonl").open("ab") as handle:
            handle.write(b'{"seq":2')
        (self.run / "activity.index.json").unlink()
        second = events.append(self.run, "test", "second")
        page, cursor, more = events.read(self.run, after=0, limit=10)
        self.assertEqual([event["seq"] for event in page], [first["seq"], second["seq"]])
        self.assertEqual((cursor, more), (2, False))
        state = json.loads((self.run / "activity.index.json").read_text())
        self.assertEqual((state["count"], state["last_seq"]), (2, 2))

    def test_malformed_last_event_index_is_rebuilt(self):
        first = events.append(self.run, "test", "first")
        index_path = self.run / "activity.index.json"
        state = json.loads(index_path.read_text())
        state["last_event"] = {"seq": first["seq"]}
        index_path.write_text(json.dumps(state), encoding="utf-8")

        second = events.append(self.run, "test", "second")
        page, _, more = events.read(self.run, after=0, limit=10)
        rebuilt = json.loads(index_path.read_text())

        self.assertEqual([event["seq"] for event in page], [1, 2])
        self.assertFalse(more)
        self.assertEqual(rebuilt["last_event"], second)
        self.assertEqual((rebuilt["count"], rebuilt["last_seq"]), (2, 2))

    def test_unterminated_valid_json_tail_is_invisible_then_repaired_by_append(self):
        first = events.append(self.run, "test", "first")
        uncommitted = {"seq": 2, "received_at": 1.0, "kind": "test", "summary": "uncommitted"}
        with (self.run / "activity.jsonl").open("ab") as handle:
            handle.write(json.dumps(uncommitted, separators=(",", ":")).encode("utf-8"))

        page, cursor, more = events.read(self.run, after=0, limit=10)
        self.assertEqual([event["seq"] for event in page], [first["seq"]])
        self.assertEqual((cursor, more), (1, False))
        self.assertEqual(events.statistics(self.run)[0], 1)

        second = events.append(self.run, "test", "second")
        page, cursor, more = events.read(self.run, after=0, limit=10)
        self.assertEqual([event["summary"] for event in page], ["first", "second"])
        self.assertEqual((cursor, more), (2, False))
        self.assertEqual(second["seq"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)

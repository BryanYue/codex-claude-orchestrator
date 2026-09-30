"""Scope separation for CLI usage: final main-agent usage vs cumulative per-model session estimates."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402
import usage  # noqa: E402

MODEL_USAGE = {
    "claude-opus-5-5": {"inputTokens": 12, "outputTokens": 2413, "cacheReadInputTokens": 88420,
                        "cacheCreationInputTokens": 18049, "webSearchRequests": 0, "costUSD": 0.21038400000000002,
                        "thinkingTokens": 98, "costBasis": "list"},
    "claude-sonnet-5-5": {"inputTokens": 12, "outputTokens": 2879, "cacheReadInputTokens": 99890,
                          "cacheCreationInputTokens": 26186, "webSearchRequests": 0, "costUSD": 0.114257,
                          "thinkingTokens": 372, "costBasis": "list"},
}
FIRST = {"type": "result", "subtype": "success", "num_turns": 4,
         "usage": {"input_tokens": 8, "cache_creation_input_tokens": 14533, "cache_read_input_tokens": 62416, "output_tokens": 869},
         "modelUsage": MODEL_USAGE, "total_cost_usd": 0.32464100000000007}
FINAL = {"type": "result", "subtype": "success", "num_turns": 2,
         "usage": {"input_tokens": 4, "cache_creation_input_tokens": 3516, "cache_read_input_tokens": 26004, "output_tokens": 1544},
         "modelUsage": MODEL_USAGE, "total_cost_usd": 0.32464100000000007}


class UsageReportTests(unittest.TestCase):
    def test_public_counterexample_keeps_scopes_apart(self):
        report = usage.usage_report(FINAL, result_events=2, resumed=False)
        cli = report["cli_session"]
        self.assertEqual([model["model"] for model in cli["models"]], ["claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual(cli["totals"], {"input_tokens": 24, "output_tokens": 5292,
                                         "cache_read_input_tokens": 188310, "cache_creation_input_tokens": 44235})
        self.assertAlmostEqual(cli["total_cost_usd"], 0.324641, places=6)
        self.assertTrue(cli["includes_subagents"])
        self.assertTrue(cli["complete"])
        self.assertFalse(cli["may_include_prior_turns"])
        main = report["main_agent_final"]
        self.assertEqual(main["tokens"], {"input_tokens": 4, "output_tokens": 1544,
                                          "cache_read_input_tokens": 26004, "cache_creation_input_tokens": 3516})
        self.assertFalse(main["includes_subagents"])
        self.assertEqual(report["result_events"], 2)
        # Neither the two cumulative results nor the two scopes are added together.
        self.assertNotAlmostEqual(cli["total_cost_usd"], 2 * 0.324641, places=3)
        self.assertNotEqual(cli["totals"]["output_tokens"], 5292 + 1544)

    def test_missing_malformed_and_zero_values(self):
        final = {"usage": {"input_tokens": 0, "output_tokens": True, "cache_read_input_tokens": -1},
                 "modelUsage": {"m1": {"inputTokens": 0, "outputTokens": 3, "cacheReadInputTokens": 0,
                                       "cacheCreationInputTokens": 0, "costUSD": 0},
                                "m2": {"inputTokens": 1.5, "outputTokens": 2, "costUSD": float("nan")},
                                "m3": "not-an-object"},
                 "total_cost_usd": "0.5"}
        report = usage.usage_report(final, result_events=1, resumed=True)
        main = report["main_agent_final"]["tokens"]
        self.assertEqual(main["input_tokens"], 0, "zero is a real value")
        self.assertIsNone(main["output_tokens"], "bool is not a token count")
        self.assertIsNone(main["cache_read_input_tokens"])
        self.assertIsNone(main["cache_creation_input_tokens"], "a missing field stays unknown, not 0")
        models = {model["model"]: model for model in report["cli_session"]["models"]}
        self.assertEqual(models["m1"]["tokens"]["input_tokens"], 0)
        self.assertEqual(models["m1"]["cost_usd"], 0.0)
        self.assertTrue(models["m1"]["complete"])
        self.assertIsNone(models["m2"]["tokens"]["input_tokens"])
        self.assertIsNone(models["m2"]["cost_usd"])
        self.assertFalse(models["m3"]["complete"])
        totals = report["cli_session"]["totals"]
        self.assertIsNone(totals["input_tokens"], "a total with any unknown part is unknown")
        self.assertIsNone(totals["cache_read_input_tokens"])
        self.assertIsNone(report["cli_session"]["total_cost_usd"])
        self.assertFalse(report["cli_session"]["complete"])
        self.assertTrue(report["cli_session"]["may_include_prior_turns"])

    def test_result_without_model_usage_does_not_invent_a_task_total(self):
        report = usage.usage_report({"usage": {"input_tokens": 3}, "total_cost_usd": 0.01}, result_events=1, resumed=False)
        self.assertEqual(report["cli_session"]["model_usage_state"], "missing")
        self.assertEqual(report["cli_session"]["models"], [])
        self.assertIsNone(report["cli_session"]["totals"])
        self.assertEqual(report["cli_session"]["total_cost_usd"], 0.01)
        self.assertEqual(report["main_agent_final"]["tokens"]["input_tokens"], 3)
        self.assertIsNone(usage.usage_report(None, result_events=0, resumed=False))
        self.assertEqual(usage.usage_report({"modelUsage": []}, result_events=1, resumed=False)["cli_session"]["model_usage_state"], "malformed")

    def test_parse_stream_keeps_only_the_final_cumulative_result(self):
        session = "11111111-1111-4111-8111-111111111111"
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "stream.jsonl"
            lines = [{"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5"},
                     {**FIRST, "session_id": session}, {**FINAL, "session_id": session}]
            stream.write_text("".join(json.dumps(line) + "\n" for line in lines))
            final, meta = bridge.parse_stream(stream, session)
        self.assertEqual(meta["result_event_count"], 2)
        self.assertEqual(final["num_turns"], 2)
        report = usage.usage_report(final, result_events=meta["result_event_count"], resumed=False)
        self.assertEqual(report["main_agent_final"]["tokens"]["output_tokens"], 1544)
        self.assertEqual(report["cli_session"]["totals"]["output_tokens"], 5292)
        self.assertAlmostEqual(report["cli_session"]["total_cost_usd"], 0.324641, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)

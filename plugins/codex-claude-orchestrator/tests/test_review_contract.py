"""Original request fidelity, review coverage, findings and explicit adjudication contracts.

Validator tests perform no provider execution. Runtime tests use the existing
fake CLI and exercise persistence, without using isolated/sandbox execution.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-claude-orchestrator/scripts"))
import bridge
from bridge_errors import BridgeError
from result_schema import RESULT_SCHEMA, finding_decisions, result_payload
import test_runtime as fixtures

REQUEST = "  请完整 review：保留正常流程；核查缺陷、架构与文档。\r\n不要把范围缩成只查 bug。  "


def report(**extra):
    return {"status": "completed", "summary": "Reviewed the declared source and its limitations",
            "evidence": ["base.txt:1"], "checks": [], "unresolved": [], **extra}


def finding(fid="F-1", category="defect", confidence="code_confirmed", evidence=None):
    return {"id": fid, "category": category, "confidence": confidence,
            "summary": "The documented fallback differs from the implementation",
            "evidence": ["base.txt:1"] if evidence is None else evidence}


class ReviewPacketContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "requirement.md"
        self.source.write_text("Preserve the public fallback contract\n")
        self.packet = {"task_id": "review-contract", "revision": 1, "role": "review", "cwd": str(self.root),
                       "objective": "An operational summary, not the user's original words",
                       "requirement_sources": [str(self.source)], "constraints": ["Preserve requirements"],
                       "acceptance": ["Verify claims against source"], "owned_files": [], "protected_files": [],
                       "model": "sonnet", "effort": "medium"}

    def validate(self, **extra):
        return bridge.validate_packet({**deepcopy(self.packet), **extra})

    def test_original_request_is_verbatim_and_first_in_prompt_separate_from_summary(self):
        packet = self.validate(user_request=REQUEST)
        self.assertEqual(packet["user_request"], REQUEST)
        self.assertEqual(packet["request_provenance"], "user_request")
        prompt = json.loads(bridge.prompt(packet))
        self.assertEqual(next(iter(prompt)), "user_request")
        self.assertEqual(prompt["user_request"], REQUEST)
        self.assertEqual(prompt["packet"]["user_request"], REQUEST)
        self.assertEqual(prompt["packet"]["objective"], self.packet["objective"])
        self.assertEqual(prompt["review"]["scope"], "full")
        self.assertIn("architecture", prompt["review"]["dimensions"])
        self.assertIn("correctness", prompt["review"]["dimensions"])

    def test_legacy_packet_remains_strict_without_fabricating_original_words(self):
        packet = self.validate()
        self.assertEqual(packet["review_mode"], "strict")
        self.assertEqual(packet["review_scope"], "full")
        self.assertEqual(packet["request_provenance"], "legacy_unspecified")
        self.assertNotIn("user_request", packet)
        self.assertIsNone(json.loads(bridge.prompt(packet))["user_request"])
        self.assertNotIn("user_request", self.packet, "validation must not mutate the caller's input")

    def test_scope_is_independent_of_execution_mode_and_controls_dimensions(self):
        for scope, present, absent in (("defects", "correctness", "architecture"),
                                       ("quality", "architecture", "correctness"),
                                       ("full", "correctness", None)):
            with self.subTest(scope=scope):
                packet = self.validate(review_scope=scope, review_mode="strict", user_request=REQUEST)
                prompt = json.loads(bridge.prompt(packet))
                self.assertEqual(packet["review_mode"], "strict")
                self.assertIn(present, prompt["review"]["dimensions"])
                if absent:
                    self.assertNotIn(absent, prompt["review"]["dimensions"])
                else:
                    self.assertIn("architecture", prompt["review"]["dimensions"])

    def test_scope_and_mode_reject_unknown_and_non_string_json_values(self):
        for field, values in (("review_scope", ["bugs", "", None, 1, [], {}]),
                              ("review_mode", ["full", "", None, 1, [], {}])):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(BridgeError):
                    self.validate(**{field: value})

    def test_original_request_rejects_blank_or_non_string_values(self):
        for value in ("", " \r\n", 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(BridgeError, "user_request"):
                self.validate(user_request=value)

    def test_explicit_isolated_requires_git_review_and_original_request(self):
        valid = self.validate(review_mode="isolated", user_request=REQUEST)
        self.assertEqual(valid["review_mode"], "isolated")
        for extra in ({}, {"role": "implement", "owned_files": ["candidate.py"]},
                      {"role": "workflow_review"}, {"workspace_kind": "artifacts", "input_files": ["requirement.md"]}):
            packet = {**self.packet, "review_mode": "isolated", **extra}
            if extra:
                packet["user_request"] = REQUEST
            with self.subTest(extra=extra), self.assertRaisesRegex(BridgeError, "isolated mode"):
                bridge.validate_packet(packet)

    def previous(self):
        previous = self.root / "prior"
        previous.mkdir()
        packet = self.validate(user_request=REQUEST)
        bridge.dump(previous / "packet.json", packet)
        bridge.dump(previous / "result.json", {"provider_subtype": "success", "actual_session_id": "session-original"})
        bridge.dump(previous / "state.json", {"status": "completed"})
        current = {**packet, "revision": 2, "correction": {"finding_id": "F-1", "kind": "local", "reason": "Check omitted branch", "attempt": 1}}
        return previous, current

    def test_resume_allows_operational_correction_but_keeps_original_scope_and_mode(self):
        previous, packet = self.previous()
        packet["objective"] = "Recheck the omitted branch against the same original request"
        packet["effort"] = "high"
        self.assertEqual(bridge.validate_resume(bridge.validate_packet(packet), previous), "session-original")
        self.assertEqual(json.loads(bridge.prompt(packet))["user_request"], REQUEST)

    def test_resume_rejects_rewritten_request_scope_or_mode(self):
        previous, packet = self.previous()
        for field, value in (("user_request", "Only inspect defects"), ("review_scope", "defects"),
                             ("review_mode", "isolated")):
            with self.subTest(field=field), self.assertRaisesRegex(BridgeError, "frozen task contract"):
                bridge.validate_resume(bridge.validate_packet({**packet, field: value}), previous)


class ReviewResultContractTests(unittest.TestCase):
    def validate(self, **extra):
        return result_payload({"structured_output": report(**extra)})

    def test_legacy_and_empty_findings_reports_remain_valid_without_invented_findings(self):
        self.assertNotIn("findings", self.validate())
        self.assertEqual(self.validate(findings=[], coverage=["Correctness reviewed; no issue found"])["findings"], [])

    def test_design_and_probable_findings_need_no_invented_trigger_or_confirmation(self):
        for category in ("design", "maintainability", "test", "documentation"):
            for confidence in ("subjective", "probable"):
                item = finding(category=category, confidence=confidence, evidence=[])
                with self.subTest(category=category, confidence=confidence):
                    result = self.validate(findings=[item])
                    self.assertEqual(result["findings"], [item])
                    self.assertNotIn("trigger", result["findings"][0])

    def test_confirmed_findings_require_nonblank_evidence(self):
        for confidence in ("reproduced", "code_confirmed"):
            self.validate(findings=[finding(confidence=confidence)])
            for evidence in ([], [" "], [None], "source.py:1"):
                with self.subTest(confidence=confidence, evidence=evidence), self.assertRaises(BridgeError):
                    self.validate(findings=[finding(confidence=confidence, evidence=evidence)])

    def test_cli_json_schema_matches_confirmed_evidence_and_subjective_shape_rules(self):
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(RESULT_SCHEMA)
        validator = jsonschema.Draft202012Validator(RESULT_SCHEMA)
        for item in (finding(category="design", confidence="subjective", evidence=[]), finding(confidence="reproduced")):
            validator.validate(report(findings=[item]))
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(report(findings=[finding(confidence="reproduced", evidence=[])]))
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(report(findings=[finding(category="unknown")]))

    def test_ids_are_required_nonblank_and_unique(self):
        for value in ("", " ", None, 2, [], {}):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                self.validate(findings=[finding(fid=value)])
        with self.assertRaisesRegex(BridgeError, "unique"):
            self.validate(findings=[finding(), finding(category="design", confidence="subjective")])

    def test_categories_confidences_and_finding_container_reject_malformed_json(self):
        for field in ("category", "confidence"):
            for value in ("invented", "", None, 1, [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(BridgeError):
                    self.validate(findings=[{**finding(), field: value}])
        for value in ({}, None, "findings", [None]):
            with self.subTest(findings=value), self.assertRaises(BridgeError):
                self.validate(findings=value)

    def test_coverage_rejects_blank_or_non_string_items(self):
        for value in ([" "], [None], "full", {}):
            with self.subTest(coverage=value), self.assertRaises(BridgeError):
                self.validate(coverage=value)


class FindingDecisionContractTests(unittest.TestCase):
    def setUp(self):
        self.findings = [finding(), finding(fid="F-2", category="design", confidence="subjective", evidence=[])]

    def test_every_original_finding_gets_one_decision_without_mutating_evidence(self):
        original = deepcopy(self.findings)
        decisions = [{"finding_id": "F-1", "disposition": "accepted"},
                     {"finding_id": "F-2", "disposition": "downgraded", "reason": "Benefit depends on the caller workload"}]
        self.assertEqual(finding_decisions(self.findings, decisions), decisions)
        self.assertEqual(self.findings, original)

    def test_missing_duplicate_unknown_or_invalid_decisions_are_refused(self):
        accepted = {"finding_id": "F-1", "disposition": "accepted"}
        cases = [None, {}, [], [accepted], [accepted, accepted],
                 [accepted, {"finding_id": "unknown", "disposition": "accepted"}],
                 [accepted, {"finding_id": "F-2", "disposition": "ignored"}]]
        for decisions in cases:
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                finding_decisions(self.findings, decisions)

    def test_downgraded_and_rejected_conclusions_require_a_reason(self):
        for disposition in ("downgraded", "rejected"):
            for reason in (None, "", " ", 2, [], {}):
                decisions = [{"finding_id": "F-1", "disposition": "accepted"},
                             {"finding_id": "F-2", "disposition": disposition, "reason": reason}]
                with self.subTest(disposition=disposition, reason=reason), self.assertRaises(ValueError):
                    finding_decisions(self.findings, decisions)
            decisions[-1]["reason"] = "Contradicted by source and a replay"
            self.assertEqual(finding_decisions(self.findings, decisions), decisions)


class RuntimeFindingDecisionTests(unittest.TestCase):
    tearDown = fixtures.RuntimeTests.tearDown
    packet = fixtures.RuntimeTests.packet
    finish = fixtures.RuntimeTests.finish

    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        self.findings = [finding(), finding(fid="F-2", category="design", confidence="subjective", evidence=[])]
        fake = self.fake.read_text()
        self.fake.write_text(fake.replace("'unresolved':[]", "'unresolved':[], 'findings':" + repr(self.findings)))

    def reported(self):
        packet = {**self.packet(), "user_request": REQUEST, "review_mode": "strict", "review_scope": "full"}
        final = self.finish(self.runtime.start(packet)["run_id"])["snapshot"]
        self.assertEqual(final["status"], "reported", final)
        run_dir = Path(final["run_dir"])
        self.assertEqual(json.loads((run_dir / "result.json").read_text())["structured"]["findings"], self.findings)
        self.assertEqual(json.loads((run_dir / "packet.json").read_text())["user_request"], REQUEST)
        return final, run_dir

    def test_invalid_adjudication_does_not_persist_a_decision_or_rewrite_the_report(self):
        final, run_dir = self.reported()
        original = (run_dir / "result.json").read_bytes()
        for decisions in (None, [], [{"finding_id": "F-1", "disposition": "accepted"}],
                          [{"finding_id": "F-1", "disposition": "accepted"}, {"finding_id": "F-2", "disposition": "rejected"}]):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                self.runtime.record_decision(final["run_id"], "accepted", "Coordinator verified source", ["requirement.md:1"],
                                             finding_decisions=decisions)
            self.assertFalse((run_dir / "decision.json").exists())
            self.assertFalse((run_dir / "decision-history.json").exists())
            self.assertIsNone(self.runtime.snapshot(final["run_id"]).get("decision"))
            self.assertEqual((run_dir / "result.json").read_bytes(), original)

    def test_returned_and_completed_by_codex_preserve_original_finding_evidence(self):
        final, run_dir = self.reported()
        original_result = (run_dir / "result.json").read_bytes()
        decisions = [{"finding_id": "F-1", "disposition": "rejected", "reason": "Fallback is implemented on the next branch"},
                     {"finding_id": "F-2", "disposition": "downgraded", "reason": "No demonstrated benefit for current workload"}]
        returned = self.runtime.record_decision(final["run_id"], "returned", "Claims do not establish required changes", ["base.txt:1"],
                                               finding_decisions=decisions)
        self.assertEqual(returned["decision"]["finding_decisions"], decisions)
        completed = self.runtime.record_decision(final["run_id"], "returned", "Original findings remain unaccepted", ["base.txt:1"],
                                                "completed_by_codex", "Codex independently verified source and closed the review",
                                                finding_decisions=decisions)
        self.assertEqual(completed["decision"]["resolution"], "completed_by_codex")
        self.assertEqual(completed["decision_history"][0], returned["decision"])
        self.assertEqual((run_dir / "result.json").read_bytes(), original_result)
        self.assertEqual(json.loads((run_dir / "decision.json").read_text())["finding_decisions"], decisions)


if __name__ == "__main__":
    unittest.main(verbosity=2)

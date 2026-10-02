"""Check the bundled Workflow program without contacting a model provider."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ASSET = Path(__file__).resolve().parents[1] / "assets/review-workflow.js"
HARNESS = r'''
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8').replace('export const meta', 'const meta');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const input = JSON.parse(process.argv[2]);
const calls = [];
async function agent(prompt, options) {
  calls.push({prompt, options});
  if (options.phase === 'verification') return {status:'blocked', summary:'mock synthesis'};
  if (input.fail && calls.length === 1) throw new Error('dimension unavailable');
  if (input.missing && calls.length === 2) return null;
  return {findings:[], coverage:[options.label]};
}
new AsyncFunction('args', 'agent', source)(input.args, agent).then(
  result => console.log(JSON.stringify({result, calls})),
  error => {console.error(String(error)); process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which("node"), "Node is required for the existing JavaScript test harness")
class BundledReviewWorkflowTests(unittest.TestCase):
    def invoke(self, **changes):
        value = {"args": {"user_request": "  原话\r\n不遗漏证据  ", "report_path": "/owned/copy/report.json",
                          "objective": "review", "review_scope": "full"}, **changes}
        completed = subprocess.run([shutil.which("node"), "-e", HARNESS, str(ASSET), json.dumps(value)],
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return value, json.loads(completed.stdout)

    def test_full_scope_preserves_original_request_and_report_destination(self):
        value, output = self.invoke()
        self.assertEqual(len(output["result"]["dimensions"]), 4)
        labels = " ".join(row["dimension"] for row in output["result"]["dimensions"])
        for dimension in ("security", "architecture", "prompts", "test coverage", "duplication"):
            self.assertIn(dimension, labels)
        for call in output["calls"]:
            self.assertIn(value["args"]["user_request"], call["prompt"])
        self.assertIn(value["args"]["report_path"], output["calls"][-1]["prompt"])
        self.assertEqual(output["calls"][-1]["options"]["phase"], "verification")

    def test_failed_and_missing_dimensions_reach_synthesis(self):
        _, output = self.invoke(fail=True, missing=True)
        rows = output["result"]["dimensions"]
        self.assertEqual([row["state"] for row in rows], ["failed", "missing", "returned", "returned"])
        self.assertIn("dimension unavailable", output["calls"][-1]["prompt"])
        self.assertIn('"state":"missing"', output["calls"][-1]["prompt"])
        self.assertIn("Incomplete required coverage must be blocked", output["calls"][-1]["prompt"])


if __name__ == "__main__":
    unittest.main()

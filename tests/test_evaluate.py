"""Measuring the harness on an eval case: prepared by qk, run as a job, graded by qk, never a pull request."""

import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox  # noqa: E402
from harness import capabilities, evaluate, policy, state  # noqa: E402


class EvaluateTests(Sandbox):
    def measure(self, agent="", case="csv-export"):
        log = self.tmp / "qk-calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent, "FAKE_QK_LOG": str(log)}):
            result = evaluate.run_eval(case, self.qk, "codex:", "claude:")
        return result, [json.loads(line)["argv"] for line in log.read_text().splitlines()]

    def test_a_case_runs_to_the_integrated_branch_and_is_graded_there(self):
        result, calls = self.measure()
        job = state.load(result["job"])
        self.assertEqual((result["stopped"], result["passed"]), ("done", True), result)
        self.assertEqual(result["criteria"], {"built": True, "report": True})
        self.assertEqual(result["graded"], job["integration"]["worktree"])
        self.assertEqual(job["approvals"]["plan"]["by"], "eval")
        self.assertIsNone(job["pr_url"])
        self.assertFalse(self.gh_log.exists())  # no pull request, no push
        grade = [argv for argv in calls if argv[:2] == ["eval", "grade"]][0]
        self.assertEqual(grade[3], job["integration"]["worktree"])
        self.assertIn("--events", grade)
        self.assertGreater(result["totals"]["turns"], 5)
        self.assertEqual(result["totals"]["tokens"], 2 * result["totals"]["turns"])  # the stand-in reports 1 + 1
        saved = json.loads((Path(result["dir"]) / "result.json").read_text())
        self.assertEqual(saved["criteria"], result["criteria"])

    def test_the_report_names_what_was_done_and_what_a_person_decides(self):
        result, _ = self.measure()
        text = (Path(result["dir"]) / "final.md").read_text()
        for label in ("Done: 2/2 tasks built", "Evidence: contract red before build: True",
                      "Not verified:", "Needs your decision: done: an eval job is graded"):
            self.assertIn(label, text)

    def test_a_blocked_job_is_still_graded_on_the_untouched_case(self):
        result, _ = self.measure(agent="outside")
        self.assertEqual((result["stopped"], result["passed"]), ("blocked", False))
        self.assertIn("does not own", result["reason"])
        self.assertTrue(result["graded"].endswith("/repo"))

    def test_review_cases_are_refused(self):
        with self.assertRaisesRegex(evaluate.EvalError, "review case"):
            self.measure(case="bundle-review")

    def test_an_eval_job_never_opens_a_pull_request(self):
        result, _ = self.measure()
        reply = capabilities.run(result["job"], "pr.open", self.qk)
        self.assertEqual((reply["status"], reply["reason"]), ("refused", "an eval job never opens a pull request"))
        self.assertEqual(policy.next_step(state.load(result["job"])),
                         "done: an eval job is graded instead of opening a pull request")

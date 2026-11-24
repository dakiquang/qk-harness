"""Measuring the harness on an eval case: prepared by qk, run as a job, graded by qk, never a pull request."""

import json
import contextlib
import io
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox  # noqa: E402
from harness import __main__ as cli, capabilities, coordinator, evaluate, policy, qk_cli, state  # noqa: E402


def values(value):
    """Every value in a JSON document, the document itself first."""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from values(child)
    elif isinstance(value, list):
        for child in value:
            yield from values(child)


def number(value, expected):
    """A JSON number (int or float, not a bool) equal to expected."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == expected


class EvaluateTests(Sandbox):
    def invoke_eval(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = cli.main(["--lock", str(self.lock), "eval", "csv-export", *args])
        return status, output.getvalue()

    def test_cli_default_creates_one_run_and_invocation_summary(self):
        status, _ = self.invoke_eval()
        self.assertEqual(status, 0)
        summaries = list((self.tmp / "state" / "evals").rglob("summary.json"))
        self.assertEqual(len(summaries), 1)
        results = list(summaries[0].parent.glob("*/result.json"))
        self.assertEqual(len(results), 1)
        self.assertTrue(json.loads(results[0].read_text())["passed"])
        # The pass share is checked where it cannot coincide with a count: 0.5 in the repeat test.

    def test_invocations_started_in_the_same_second_have_distinct_directories(self):
        with mock.patch("harness.evaluate.time.strftime", return_value="same-second"):
            self.invoke_eval()
            self.invoke_eval()
        summaries = list((self.tmp / "state" / "evals").rglob("summary.json"))
        self.assertEqual(len(summaries), 2)
        self.assertNotEqual(summaries[0].parent, summaries[1].parent)
        self.assertTrue(all(len(list(path.parent.glob("*/result.json"))) == 1 for path in summaries))

    def test_repeat_records_distinct_runs_and_all_criteria_pass_share(self):
        original_grade = qk_cli.Qk.eval_grade
        count = 0

        def grade(qk, *args, **kwargs):
            nonlocal count
            result = original_grade(qk, *args, **kwargs)
            count += 1
            if count == 2:
                result.data["criteria"][0]["passed"] = False
                result.data["passed"] = False
            return result

        # A fake clock that only moves while a run's job runs: 7s in the first run, 11s in the second,
        # however often the implementation reads it.
        now, steps = [100.0], iter([7, 11])
        original_prepare, original_run = qk_cli.Qk.eval_prepare, coordinator.run
        pending = [0]

        def prepare(qk, *args, **kwargs):
            pending[0] = next(steps)
            return original_prepare(qk, *args, **kwargs)

        def run(*args, **kwargs):
            now[0] += pending[0]
            pending[0] = 0
            return original_run(*args, **kwargs)

        with mock.patch.object(qk_cli.Qk, "eval_grade", grade), \
                mock.patch.object(qk_cli.Qk, "eval_prepare", prepare), \
                mock.patch.object(coordinator, "run", run), \
                mock.patch.object(evaluate, "time", wraps=evaluate.time) as clock:
            clock.monotonic.side_effect = lambda: now[0]
            self.invoke_eval("--repeat", "2")
        self.assertEqual(count, 2)
        summaries = list((self.tmp / "state" / "evals").rglob("summary.json"))
        self.assertEqual(len(summaries), 1)
        invocation = summaries[0].parent
        paths = list(invocation.glob("*/result.json"))
        self.assertEqual(len(paths), 2)
        results = [json.loads(path.read_text()) for path in paths]
        self.assertEqual(sorted(result["passed"] for result in results), [False, True])
        summary = json.loads(summaries[0].read_text())

        def has_directory(record, directory):
            return any(isinstance(value, str) and
                       (value in (str(directory), directory.name) or invocation / value == directory
                        or invocation.parent / value == directory)
                       for value in values(record))

        def has_criteria(record, expected):
            for value in values(record):
                if value == expected:
                    return True
                if isinstance(value, list) and all(isinstance(item, dict) and
                        "name" in item and "passed" in item for item in value):
                    if {item["name"]: item["passed"] for item in value} == expected:
                        return True
            return False

        # The root can contain parallel lists, so it cannot establish a per-run association.
        records = [value for value in values(summary) if isinstance(value, dict) and value is not summary]
        for path, result in zip(paths, results):
            other = next(candidate.parent for candidate in paths if candidate != path)
            matches = [record for record in records if has_criteria(record, result["criteria"])
                       and any(number(value, result["totals"]["tokens"]) for value in values(record))
                       and any(number(value, result["seconds"]) for value in values(record))
                       and has_directory(record, path.parent) and not has_directory(record, other)]
            self.assertTrue(matches, (path, summary))
        self.assertIn(0.5, list(values(summary)))

    def test_a_failed_run_is_an_error_and_no_summary_counts_it_as_completed(self):
        original_grade = qk_cli.Qk.eval_grade
        count = 0

        def grade(qk, *args, **kwargs):
            nonlocal count
            result = original_grade(qk, *args, **kwargs)
            count += 1
            if count == 2:
                result.code = 1
            return result

        with mock.patch.object(qk_cli.Qk, "eval_grade", grade):
            status, _ = self.invoke_eval("--repeat", "2")
        self.assertEqual(count, 2)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(list((self.tmp / "state" / "evals").rglob("result.json"))), 1,
                         "the finished run keeps its result.json")
        for summary in (self.tmp / "state" / "evals").rglob("summary.json"):
            invocation = summary.parent
            listed = {value for value in values(json.loads(summary.read_text())) if isinstance(value, str)}
            for run in (path for path in invocation.iterdir() if path.is_dir()):
                if not (run / "result.json").exists():
                    names = {run.name, str(run), str(run.relative_to(invocation.parent))}
                    self.assertFalse(names & listed, "a summary lists a run that did not finish")

    def test_repeat_rejects_nonpositive_and_noninteger_counts(self):
        evals = self.tmp / "state" / "evals"
        status, _ = self.invoke_eval("--repeat", "1")
        self.assertEqual(status, 0)
        self.assertEqual(len(list(evals.rglob("summary.json"))), 1)
        before = sorted(evals.rglob("*"))
        for count in ("0", "-1", "1.5", "abc"):
            with self.subTest(count=count), contextlib.redirect_stderr(io.StringIO()):
                try:
                    status, _ = self.invoke_eval("--repeat", count)
                except SystemExit as error:
                    status = error.code
                self.assertNotEqual(status, 0)
        self.assertEqual(sorted(evals.rglob("*")), before, "a rejected count must not start a run")

    def test_both_guides_document_repeated_evals(self):
        root = Path(__file__).resolve().parents[1]
        for guide in ("docs/en/guide.md", "docs/vi/guide.md"):
            text = (root / guide).read_text(encoding="utf-8")
            with self.subTest(guide=guide):
                self.assertIn("--repeat", text)
                self.assertIn("summary.json", text)

    def measure(self, agent="", case="csv-export"):
        log = self.tmp / "qk-calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent, "FAKE_QK_LOG": str(log)}):
            result = evaluate.run_eval(case, self.qk, "codex:", "claude:")
        return result, [json.loads(line)["argv"] for line in log.read_text().splitlines()]

    def test_a_case_can_run_without_task_reviews(self):
        log = self.tmp / "qk-calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(log)}):
            result = evaluate.run_eval("csv-export", self.qk, "codex:", "claude:", task_review=False)
        job = state.load(result["job"])
        self.assertEqual((result["stopped"], result["task_review"], job["options"]["task_review"]), ("done", False, False))
        reviews = [json.loads(line)["stdin"] for line in log.read_text().splitlines()
                   if json.loads(line)["argv"][:2] == ["agent", "run"] and "Independent read-only review." in
                   json.loads(line)["stdin"]]  # code reviews, not the contract review
        self.assertTrue(reviews)
        self.assertTrue(all("review-integration.diff" in prompt for prompt in reviews), "only the integrated review")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(evaluate, "run_evals", return_value={"runs": [{"stopped": "done"}]}) as runs:
            cli.main(["--lock", str(self.lock), "eval", "csv-export", "--no-task-review"])
        self.assertIs(runs.call_args.kwargs["task_review"], False)

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

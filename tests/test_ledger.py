"""A stopped job is recorded in qk's ledger once per change; a resume recovers and never repeats a step."""

import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox, sh  # noqa: E402
from harness import __version__, capabilities, coordinator, ledger, policy, state  # noqa: E402

RUNNER_STAGES = {"preflight", "planning", "implementing", "verifying", "reviewing", "fixing", "publishing"}


class LedgerTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "qk-calls.jsonl"
        patch = mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(self.log)})
        patch.start()
        self.addCleanup(patch.stop)

    def logged(self):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [(call["argv"], json.loads(call["stdin"])) for call in calls if call["argv"][:2] == ["log", "run"]]

    def run_job(self, job_id, agent=""):
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent}):
            return coordinator.run(job_id, self.qk)

    def started(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        return job_id

    def test_a_finished_job_is_recorded_once_in_qks_shape(self):
        job_id = self.new()
        self.assertEqual(self.run_job(job_id)["stopped"], "waiting")
        self.assertEqual(self.logged(), [])  # waiting at a gate is not a stop
        capabilities.approve(job_id, "plan")
        reply = self.run_job(job_id)
        self.assertEqual(reply["ledger"], {"id": f"fake-{job_id}", "stage": "done"})
        [(argv, data)] = self.logged()
        self.assertEqual(argv, ["log", "run", "--producer", f"qk-harness {__version__}", "--state-dir",
                                str(state.job_dir(job_id)), "--json"])
        self.assertTrue((state.job_dir(job_id) / "state.json").is_file())
        job = state.load(job_id)
        self.assertEqual((data["id"], data["stage"], data["repo"]), (job_id, "done", str(self.repo)))
        self.assertEqual((data["commit"], data["pr_url"]), (job["integration"]["commit"], job["pr_url"]))
        self.assertEqual(data["verify_results"], [{"exit": 0}])
        self.assertEqual(sorted(data["diff_stat"]["files"]),
                         ["src/api/__init__.py", "src/cli/__init__.py", "tests/test_contract.py"])
        self.assertLessEqual(set(data["usage"]), RUNNER_STAGES)
        self.assertEqual((data["impl"], data["review"]), ("codex:", "claude:"))
        self.assertEqual(data["ledger_revision"], 1)
        self.run_job(job_id)
        self.assertEqual(len(self.logged()), 1)  # nothing changed, nothing recorded again

    def test_a_blocked_job_is_recorded_with_its_stage(self):
        job_id = self.started()
        reply = self.run_job(job_id, "outside")
        self.assertEqual(reply["stopped"], "blocked")
        [(_, data)] = self.logged()
        self.assertEqual((data["stage"], data["blocked_at"]), ("blocked", "implementing"))

    def test_an_abort_is_recorded(self):
        job_id = self.started()
        reply = coordinator.finish(job_id, self.qk, coordinator.abort(job_id, "not needed"))
        self.assertEqual(reply["ledger"]["stage"], "aborted")

    def test_eval_jobs_stay_out_of_the_ledger(self):
        job_id = self.new()
        with state.locked(job_id) as job:
            job["eval"] = {"case": "x"}
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.assertEqual(self.run_job(job_id)["stopped"], "done")
        self.assertEqual(self.logged(), [])

    def test_resume_clears_a_task_left_running_by_a_dead_process(self):
        job_id = self.started()
        self.advance(job_id, until=f"build.run {job_id} api")
        with state.locked(job_id) as job:
            job["tasks"]["api"]["running"] = {"capability": "build.run", "since": "earlier", "pid": 999999}
        reply = self.run_job(job_id)
        self.assertEqual((reply["stopped"], reply["recovered"]), ("done", ["api"]))
        self.assertTrue(any(entry["capability"] == "resume" for entry in state.load(job_id)["log"]))

    def test_resume_does_not_build_over_an_unrecorded_commit(self):
        job_id = self.started()
        self.advance(job_id, until=f"build.run {job_id} cli")
        worktree = Path(state.load(job_id)["tasks"]["cli"]["worktree"])
        if not worktree.exists():
            capabilities.integration_worktree(state.load(job_id))
            sh(self.repo, "git", "worktree", "add", "-q", "-b", state.load(job_id)["tasks"]["cli"]["branch"],
               str(worktree), state.load(job_id)["contract"]["commit"])
        (worktree / "partial.txt").write_text("written before an interruption\n")
        sh(worktree, "git", "add", "-A")
        sh(worktree, "git", "commit", "-qm", "unrecorded")
        reply = self.run_job(job_id)
        self.assertEqual((reply["stopped"], reply["capability"]), ("blocked", "build.run"))
        self.assertIn("a person looks before anything is built again", reply["reason"])
        self.assertEqual(sh(worktree, "git", "log", "--format=%s", "-1"), "unrecorded")

    def test_resume_after_a_lost_pull_request_url_reuses_the_pull_request(self):
        job_id = self.started()
        self.run_job(job_id)
        with state.locked(job_id) as job:  # as if the process died after creating the PR, before saving
            job["pr_url"] = None
        reply = self.run_job(job_id)
        self.assertEqual(reply["stopped"], "done")
        self.assertEqual(state.load(job_id)["pr_url"], "https://github.com/example/project/pull/7")
        self.assertEqual(self.gh_log.read_text().count("pr create"), 1)

    def test_stage_names_follow_qk_runner_stages(self):
        self.assertLessEqual(set(ledger.STAGES.values()), RUNNER_STAGES)
        self.assertEqual(set(ledger.STAGES), set(policy.CAPABILITIES))

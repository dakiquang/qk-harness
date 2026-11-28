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
        seen = []
        original = type(self.qk).log_run

        def log_run(qk, data, producer, state_dir):
            # What qk brief would read the moment the record exists.
            seen.append(json.loads((Path(state_dir) / "state.json").read_text()).get("reason"))
            return original(qk, data, producer, state_dir)

        with mock.patch.object(type(self.qk), "log_run", log_run):
            reply = self.run_job(job_id, "outside")
        self.assertTrue(seen and seen[-1])
        self.assertEqual(reply["stopped"], "blocked")
        [(_, data)] = self.logged()
        self.assertEqual((data["stage"], data["blocked_at"]), ("blocked", "implementing"))
        # qk brief reads why from the state.json the record points to; the ledger keeps no reason text.
        saved = json.loads((state.job_dir(job_id) / "state.json").read_text())
        blocked = [entry for entry in saved["log"] if entry["status"] == "blocked"][-1]
        self.assertTrue(saved["reason"])
        self.assertEqual(saved["reason"], blocked["reason"])

    def test_an_abort_is_recorded(self):
        job_id = self.started()
        self.run_job(job_id, "outside")
        self.assertIn("reason", state.load(job_id))
        reply = coordinator.finish(job_id, self.qk, coordinator.abort(job_id, "not needed"))
        self.assertEqual(reply["ledger"]["stage"], "aborted")
        self.assertNotIn("reason", state.load(job_id))  # an old blocked reason does not outlive the stop

    def test_eval_jobs_stay_out_of_the_ledger(self):
        job_id = self.new()
        with state.locked(job_id) as job:
            job["eval"] = {"case": "x"}
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.assertEqual(self.run_job(job_id)["stopped"], "done")
        self.assertEqual(self.logged(), [])

    def test_resume_blocks_a_dead_task_without_terminal_step_evidence(self):
        job_id = self.started()
        self.advance(job_id, until=f"build.run {job_id} api")
        with state.locked(job_id) as job:
            job["tasks"]["api"]["running"] = {"capability": "build.run", "since": "earlier", "pid": 999999}
        with mock.patch.object(self.qk, "agent_run") as agent:
            reply = self.run_job(job_id)
        self.assertEqual(reply["stopped"], "blocked")
        self.assertIn("inspect", reply["reason"])
        self.assertIn("running", state.load(job_id)["tasks"]["api"])
        agent.assert_not_called()

    def test_resume_recovers_a_persisted_completed_step_without_replaying_its_build(self):
        job_id = self.started()
        self.advance(job_id, until=f"review.run {job_id} api")
        with state.locked(job_id) as job:
            receipt = next(turn for turn in reversed(job["usage"]) if turn["capability"] == "build.run")
            job["tasks"]["api"]["running"] = {"capability": "build.run", "pid": 999999,
                                               "events": receipt["events"], "completed": True}
            commit = job["tasks"]["api"]["commit"]
        self.assertEqual(ledger.recover(job_id), ["api"])
        self.assertNotIn("running", state.load(job_id)["tasks"]["api"])
        self.assertEqual(state.load(job_id)["tasks"]["api"]["commit"], commit)

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

    def test_legacy_roles_use_write_access_and_capabilities_keep_their_roles(self):
        job = state.load(self.new())
        job["usage"] = [
            {"spec": "legacy-writer", "write": True},
            {"spec": "legacy-reader", "write": False},
            {"spec": "cap-writer", "capability": "build.run", "write": False},
            {"spec": "cap-reviewer", "capability": "review.run", "write": True},
            {"spec": "unknown-cap", "capability": "future.step", "write": False},
        ]
        data = ledger.run_state(job, "done")
        self.assertEqual(set(data["impl"].split(", ")), {"legacy-writer", "cap-writer", "unknown-cap"})
        self.assertEqual(set(data["review"].split(", ")), {"legacy-reader", "cap-reviewer"})


class ReviewFollowUpTests(Sandbox):
    """Problems found when reviewing the integration fix round, the ledger and abort."""

    logged, run_job, started = LedgerTests.logged, LedgerTests.run_job, LedgerTests.started

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "qk-calls.jsonl"
        patch = mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(self.log)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_integration_fix_starts_only_from_the_checked_commit(self):
        job_id = self.started()
        with mock.patch.dict(os.environ, {"FAKE_AGENT": "integration-findings"}):
            self.advance(job_id, until=f"build.run {job_id} integration --fix", agent="integration-findings")
        worktree = Path(state.load(job_id)["integration"]["worktree"])
        (worktree / "src" / "api" / "late.py").write_text("LATE = 1\n")
        sh(worktree, "git", "add", "-A")
        sh(worktree, "git", "commit", "-qm", "unrecorded")
        reply = capabilities.run(job_id, "build.run", self.qk, "integration", fix=True)
        self.assertEqual(reply["status"], "blocked")
        self.assertIn("not the checked commit", reply["reason"])
        self.assertEqual(state.load(job_id)["integration"].get("fix_rounds", 0), 0)

    def test_a_task_blocked_in_a_parallel_batch_is_recorded(self):
        job_id = self.started()
        reply = self.run_job(job_id, "outside-cli")
        self.assertEqual(reply["stopped"], "blocked")
        self.assertEqual(reply["ledger"]["stage"], "blocked")
        self.assertEqual(self.logged()[-1][1]["stage"], "blocked")

    def test_labels_name_only_the_models_that_ran(self):
        job_id = self.new()
        reply = self.run_job(job_id, "no-verify")  # stops at the plan: only the implementer ran
        self.assertEqual(reply["stopped"], "blocked")
        data = self.logged()[-1][1]
        self.assertEqual((data["impl"], data["review"]), ("codex:", ""))

    def test_a_resume_that_changes_nothing_records_nothing_new(self):
        job_id = self.started()
        coordinator.finish(job_id, self.qk, coordinator.abort(job_id, "stop"))
        with state.locked(job_id) as job:
            job["tasks"]["api"]["running"] = {"capability": "build.run", "since": "earlier", "pid": 999999}
        self.run_job(job_id)
        self.assertEqual(len(self.logged()), 1)

    def test_abort_stands_when_qk_cannot_record_it(self):
        import contextlib, io
        from harness import __main__ as cli
        job_id = self.started()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(["abort", job_id, "--reason", "stop", "--lock", str(self.tmp / "missing.lock")])
        reply = json.loads(output.getvalue())
        self.assertEqual((status, reply["status"]), (0, "done"))
        self.assertIn("cannot read", reply["ledger"]["error"])
        self.assertIn("left", reply)
        self.assertTrue(state.load(job_id)["aborted"])

    def test_abort_stands_when_the_qk_checkout_cannot_be_created(self):
        import contextlib, io
        from harness import __main__ as cli, qk_cli
        job_id = self.started()
        output = io.StringIO()
        with mock.patch.object(qk_cli, "fetch", side_effect=PermissionError("read-only state directory")), \
                contextlib.redirect_stdout(output):
            status = cli.main(["--lock", str(self.lock), "abort", job_id])
        reply = json.loads(output.getvalue())
        self.assertEqual((status, reply["ledger"]["error"]), (0, "read-only state directory"))

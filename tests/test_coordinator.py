"""The coordinator only follows `next`: it stops at every gate, at a blocked step and at the end."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox  # noqa: E402
from harness import __main__ as cli, capabilities, coordinator, state  # noqa: E402


class CoordinatorTests(Sandbox):
    def run_job(self, job_id, agent=""):
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent}):
            return coordinator.run(job_id, self.qk)

    def test_medium_job_stops_once_for_the_plan_then_runs_to_the_pull_request(self):
        job_id = self.new()
        first = self.run_job(job_id)
        self.assertEqual((first["stopped"], first["next"]), ("waiting", f"approve {job_id} plan"))
        capabilities.approve(job_id, "plan")
        last = self.run_job(job_id)
        self.assertEqual(last["stopped"], "done")
        self.assertEqual(state.load(job_id)["pr_url"], "https://github.com/example/project/pull/7")

    def test_large_job_stops_at_each_gate(self):
        job_id = self.new("large")
        stops = []
        for stage in ("plan", "contract", "build", "integrate"):
            reply = self.run_job(job_id)
            stops.append((reply["stopped"], reply["next"]))
            capabilities.approve(job_id, stage)
        self.assertEqual(stops, [("waiting", f"approve {job_id} {stage}")
                                 for stage in ("plan", "contract", "build", "integrate")])
        self.assertEqual(self.run_job(job_id)["stopped"], "done")

    def test_a_blocked_step_stops_the_loop_with_its_reason(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        reply = self.run_job(job_id, agent="outside")
        self.assertEqual((reply["stopped"], reply["capability"]), ("blocked", "build.run"))
        self.assertIn("does not own: README.md", reply["reason"])

    def test_the_fix_round_runs_on_its_own(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        reply = self.run_job(job_id, agent="broken")  # the first api build fails its check; the fix passes
        self.assertEqual(reply["stopped"], "done")
        tasks = state.load(job_id)["tasks"]
        self.assertEqual((tasks["api"]["fix_rounds"], tasks["cli"]["fix_rounds"]), (1, 0))

    def test_a_failed_contract_review_gets_one_revision_then_a_person_decides(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.assertEqual(self.run_job(job_id, agent="contract-once")["stopped"], "done")
        contract = state.load(job_id)["contract"]
        self.assertEqual((contract["revisions"], len(contract["earlier_reviews"])), (1, 1))
        self.assertIn("test: revise the contract", self.sh_log(job_id))
        other = self.new()
        self.run_job(other)
        capabilities.approve(other, "plan")
        reply = self.run_job(other, agent="contract-twice")
        self.assertEqual(reply["stopped"], "waiting")
        self.assertIn("did not pass after its revision", reply["next"])
        again = capabilities.run(other, "contract.write", self.qk, fix=True)
        self.assertEqual((again["status"], again["reason"]), ("refused", "the contract has no revision round left"))

    def test_the_contract_revision_needs_a_failed_review(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"contract.review {job_id}")
        reply = capabilities.run(job_id, "contract.write", self.qk, fix=True)
        self.assertEqual((reply["status"], reply["reason"]), ("refused", "the contract has no failed review to fix"))

    def sh_log(self, job_id):
        from test_capabilities import sh
        return sh(self.repo, "git", "log", "--format=%s", state.load(job_id)["integration"]["branch"])

    def test_an_aborted_job_refuses_every_step(self):
        job_id = self.new()
        self.run_job(job_id)
        reply = coordinator.abort(job_id, "not needed")
        self.assertEqual(reply["status"], "done")
        self.assertEqual(reply["left"], {"worktrees": [], "branches": []})  # nothing was built yet
        self.assertEqual(reply["next"], "aborted: not needed")
        self.assertEqual(self.run_job(job_id)["stopped"], "aborted")
        self.assertEqual(capabilities.approve(job_id, "plan")["reason"], "the job is aborted")
        self.assertEqual(capabilities.run(job_id, "contract.write", self.qk)["reason"], "the job is aborted")
        self.assertEqual(coordinator.abort(job_id, "again")["status"], "refused")

    def test_abort_lists_the_branches_and_worktrees_that_exist(self):
        job_id = self.new()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"build.run {job_id} cli")  # api is built, cli is not
        job = state.load(job_id)
        left = coordinator.abort(job_id, "stop")["left"]
        self.assertEqual(left["worktrees"], [job["integration"]["worktree"], job["tasks"]["api"]["worktree"]])
        self.assertEqual(left["branches"], [job["integration"]["branch"], job["tasks"]["api"]["branch"]])

    def test_cli_run_and_resume_follow_the_same_loop(self):
        job_id = self.new()
        for command in ("run", "resume"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(["--lock", str(self.lock), command, job_id])
            reply = json.loads(output.getvalue())
            self.assertEqual((status, reply["stopped"], reply["next"]), (0, "waiting", f"approve {job_id} plan"))

    def test_parse_step(self):
        self.assertEqual(coordinator.parse_step("build.run J api --fix"), ("build.run", "api", False, True))
        self.assertEqual(coordinator.parse_step("verify.run J integration --expect-red"),
                         ("verify.run", "integration", True, False))
        self.assertEqual(coordinator.parse_step("integrate.run J"), ("integrate.run", None, False, False))
        self.assertIsNone(coordinator.parse_step("approve J plan"))
        self.assertIsNone(coordinator.parse_step("a person decides: x"))

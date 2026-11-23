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
from harness import __main__ as cli, capabilities, coordinator, policy, state  # noqa: E402


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


class NoContractTests(Sandbox):
    def new_plain(self, size="medium"):
        return state.new_job(self.repo, "Add a greeting API and CLI", size, "main", "codex:", "claude:",
                             contract=False)["id"]

    def run_job(self, job_id, agent=""):
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent}):
            return coordinator.run(job_id, self.qk)

    def test_a_job_without_a_contract_skips_that_stage_and_still_reaches_the_pull_request(self):
        job_id = self.new_plain()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.assertEqual(self.run_job(job_id)["stopped"], "done")
        job = state.load(job_id)
        self.assertIsNone(job["contract"])
        self.assertFalse([entry for entry in job["log"] if entry["capability"].startswith("contract")])
        api = job["tasks"]["api"]
        from test_capabilities import sh
        self.assertEqual(sh(self.repo, "git", "merge-base", api["commit"], job["base_sha"]), job["base_sha"])
        self.assertEqual(sh(self.repo, "git", "rev-parse", f"{api['commit']}^"), job["base_sha"])

    def test_without_a_contract_the_plan_gives_each_task_its_own_tests(self):
        job_id = self.new_plain()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        self.assertEqual(self.run_job(job_id)["stopped"], "done")
        job = state.load(job_id)
        self.assertEqual(job["plan"]["contract"], [])
        self.assertIn("tests/test_api.py", job["tasks"]["api"]["owns"])
        from test_capabilities import sh
        files = sh(self.repo, "git", "show", "--name-only", "--format=", job["tasks"]["api"]["commit"]).split()
        self.assertEqual(sorted(files), ["src/api/__init__.py", "tests/test_api.py"])

    def test_a_contract_job_still_keeps_tests_out_of_tasks(self):
        job_id = self.new()
        self.run_job(job_id)
        job = state.load(job_id)
        self.assertEqual(job["plan"]["contract"], ["tests/"])
        self.assertNotIn("tests/test_api.py", job["tasks"]["api"]["owns"])

    def test_contract_steps_are_refused_without_a_contract(self):
        job_id = self.new_plain()
        self.run_job(job_id)
        capabilities.approve(job_id, "plan")
        for capability, target, red in (("contract.write", None, False), ("contract.review", None, False),
                                        ("verify.run", "integration", True)):
            reply = capabilities.run(job_id, capability, self.qk, target, expect_red=red)
            self.assertEqual((reply["status"], reply["reason"]),
                             ("refused", "this job was started without a contract"), capability)

    def test_a_large_job_without_a_contract_has_three_gates(self):
        job_id = self.new_plain("large")
        stops = []
        for stage in ("plan", "build", "integrate"):
            stops.append(self.run_job(job_id)["next"])
            capabilities.approve(job_id, stage)
        self.assertEqual(stops, [f"approve {job_id} {stage}" for stage in ("plan", "build", "integrate")])
        self.assertIn("has no contract gate", capabilities.approve(job_id, "contract")["reason"])
        self.assertEqual(self.run_job(job_id)["stopped"], "done")

    def test_jobs_saved_before_the_option_keep_their_contract(self):
        job_id = self.new()
        with state.locked(job_id) as job:
            del job["options"]
        self.assertEqual(policy.next_step(state.load(job_id)), f"plan.write {job_id}")
        self.assertTrue(policy.contract_on(state.load(job_id)))


class ParallelTests(Sandbox):
    def job(self, parallel=2):
        job_id = state.new_job(self.repo, "Add a greeting API and CLI", "medium", "main", "codex:", "claude:",
                               parallel=parallel)["id"]
        coordinator.run(job_id, self.qk)
        capabilities.approve(job_id, "plan")
        return job_id

    def run_with_barrier(self, job_id, wait):
        barrier = self.tmp / f"barrier-{job_id}"
        barrier.mkdir()
        with mock.patch.dict(os.environ, {"FAKE_AGENT": "", "FAKE_BARRIER": str(barrier), "FAKE_BARRIER_WAIT": wait}):
            reply = coordinator.run(job_id, self.qk)
        return reply, {path.stem: path.read_text() for path in barrier.glob("*.together")}

    def test_two_tasks_build_at_the_same_time_and_reach_the_pull_request(self):
        job_id = self.job()
        reply, together = self.run_with_barrier(job_id, "10")
        self.assertEqual(reply["stopped"], "done")
        self.assertEqual(together, {"api": "True", "cli": "True"})
        job = state.load(job_id)
        self.assertTrue(all(task["commit"] and "running" not in task for task in job["tasks"].values()))
        builds = [entry for entry in job["log"] if entry["capability"] == "build.run"]
        self.assertEqual([entry["status"] for entry in builds], ["done", "done"])

    def test_sequential_jobs_build_one_task_at_a_time(self):
        job_id = self.job(parallel=1)
        reply, together = self.run_with_barrier(job_id, "1")
        self.assertEqual(reply["stopped"], "done")
        self.assertEqual(together, {"api": "False", "cli": "False"})

    def test_a_running_task_is_not_started_twice(self):
        job_id = self.job()
        self.advance(job_id, until=f"build.run {job_id} api")
        with state.locked(job_id) as job:
            job["tasks"]["api"]["running"] = {"capability": "build.run", "since": "now", "pid": 1}
        reply = capabilities.run(job_id, "build.run", self.qk, "api")
        self.assertEqual(reply["status"], "refused")
        self.assertIn("task api is already running build.run", reply["reason"])
        self.assertEqual(policy.task_steps(state.load(job_id)), [f"build.run {job_id} cli"])
        self.assertEqual(capabilities.run(job_id, "integrate.run", self.qk)["status"], "refused")

    def test_tasks_can_name_their_own_roles(self):
        log = self.tmp / "qk-calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_PLAN_ROLES": json.dumps({"api": {"impl": "codex:alt"}}),
                                          "FAKE_QK_LOG": str(log)}):
            job_id = self.job()
            self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "done")
        task = state.load(job_id)["tasks"]["api"]
        self.assertEqual((task["impl_spec"], task["review_spec"]), ("codex:alt", "claude:"))
        specs = [argv[argv.index("--spec") + 1] for argv in (json.loads(line)["argv"]
                                                             for line in log.read_text().splitlines())
                 if argv[:2] == ["agent", "run"] and "--write" in argv]
        self.assertIn("codex:alt", specs)

    def test_role_families_are_checked_before_any_worker_runs(self):
        for roles, reason in (({"api": {"review": "codex:"}}, "task api: reviewer codex: is the same model family"),
                              ({"api": {"impl": "claude:", "review": "codex:"}},
                               "the integration reviewer claude: is the same model family as a task implementer")):
            with self.subTest(roles=roles), mock.patch.dict(os.environ, {"FAKE_PLAN_ROLES": json.dumps(roles)}):
                job_id = state.new_job(self.repo, "x", "medium", "main", "codex:", "claude:")["id"]
                reply = coordinator.run(job_id, self.qk)
                self.assertEqual(reply["stopped"], "blocked")
                self.assertIn(reason, reply["reason"])
                self.assertIsNone(state.load(job_id)["plan"])

    def test_cli_start_can_ask_for_one_task_at_a_time(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["--lock", str(self.lock), "start", "--repo", str(self.repo), "--task", "x", "--sequential"])
        job_id = json.loads(output.getvalue())["id"]
        self.assertEqual(state.load(job_id)["options"]["parallel"], 1)


class IntegrationTests(Sandbox):
    def ready(self, **options):
        job_id = state.new_job(self.repo, "Add a greeting API and CLI", "medium", "main", "codex:", "claude:",
                               **options)["id"]
        coordinator.run(job_id, self.qk)
        capabilities.approve(job_id, "plan")
        return job_id

    def run_job(self, job_id, agent=""):
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent}):
            return coordinator.run(job_id, self.qk)

    def test_the_integrated_branch_gets_one_fix_round_for_located_findings(self):
        job_id = self.ready()
        self.assertEqual(self.run_job(job_id, "integration-findings")["stopped"], "done")
        integration = state.load(job_id)["integration"]
        self.assertEqual(integration["fix_rounds"], 1)
        from test_capabilities import sh
        self.assertIn(f"fix: integration of {state.load(job_id)['slug']}",
                      sh(self.remote, "git", "log", "--format=%s", integration["branch"]))
        reply = capabilities.run(job_id, "build.run", self.qk, "integration", fix=True)
        self.assertEqual(reply["status"], "refused")

    def test_findings_without_a_file_and_line_wait_for_a_person(self):
        job_id = self.ready()
        reply = self.run_job(job_id, "unlocated")
        self.assertEqual(reply["stopped"], "waiting")
        self.assertIn("serious review findings that a fix round cannot take", reply["next"])
        self.assertEqual(state.load(job_id)["tasks"]["api"]["fix_rounds"], 0)

    def test_task_reviews_can_be_switched_off(self):
        job_id = self.ready(task_review=False)
        self.assertEqual(self.run_job(job_id)["stopped"], "done")
        job = state.load(job_id)
        reviews = [entry for entry in job["log"] if entry["capability"] == "review.run"]
        self.assertEqual(len(reviews), 1)  # the integrated branch only
        self.assertIsNotNone(job["integration"]["review"])
        self.assertTrue(all(task["review"] is None for task in job["tasks"].values()))
        self.assertEqual(capabilities.run(job_id, "review.run", self.qk, "api")["reason"],
                         "this job was started without task reviews")

    def test_pr_open_needs_push_permission(self):
        job_id = self.ready()
        with mock.patch.dict(os.environ, {"FAKE_GH_PERMISSION": "READ"}):
            reply = self.run_job(job_id)
        self.assertEqual((reply["stopped"], reply["capability"]), ("blocked", "pr.open"))
        self.assertIn("cannot push to this repository (viewerPermission=READ)", reply["reason"])
        from test_capabilities import sh
        self.assertEqual(sh(self.remote, "git", "branch", "--list"), "")  # nothing was pushed
        self.assertIsNone(state.load(job_id)["pr_url"])

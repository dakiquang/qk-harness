"""The eight capabilities on a sample project, with a stand-in qk and a scripted agent: order, gates and limits."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fake_qk  # noqa: E402
from harness import __main__ as cli, capabilities, coordinator, gitops, policy, qk_cli, state  # noqa: E402


AGENT = Path(__file__).resolve().parent / "fake_agent.py"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid"}
GH = '''#!/bin/sh
echo "$@" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr list") grep -q "^pr create" "$FAKE_GH_LOG" 2>/dev/null && echo "https://github.com/example/project/pull/7"; exit 0 ;;
  "pr create") echo "https://github.com/example/project/pull/7" ;;
  "repo view") echo "${FAKE_GH_PERMISSION:-WRITE}" ;;
esac
'''


def sh(cwd, *args):
    return subprocess.run(list(args), cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """A sample project with a bare remote, a stand-in qk and gh, and helpers; no tests of its own."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        upstream = self.tmp / "qk"
        sha = fake_qk.make_repo(upstream)
        self.lock = self.tmp / "qk.lock"
        self.lock.write_text(json.dumps({"repo": str(upstream), "tag": "v9.9.9", "sha": sha, "contract": 1}))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(GH)
        (bin_dir / "gh").chmod(0o755)
        self.gh_log = self.tmp / "gh.log"
        settings = mock.patch.dict(os.environ, dict(
            IDENTITY, QK_HARNESS_STATE_DIR=str(self.tmp / "state"), FAKE_QK_AGENT=str(AGENT), FAKE_AGENT="",
            FAKE_GH_LOG=str(self.gh_log), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"]))
        settings.start()
        self.addCleanup(settings.stop)
        self.remote = self.tmp / "remote.git"
        sh(self.tmp, "git", "init", "-q", "--bare", str(self.remote))
        self.repo = self.tmp / "project"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "README.md").write_text("project\n")
        (self.repo / "tests" / "test_base.py").write_text(
            "import unittest\n\n\nclass Base(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "base"),
                     ("remote", "add", "origin", str(self.remote))):
            sh(self.repo, "git", *args)
        self.qk = qk_cli.open_locked(self.lock)

    def new(self, size="medium"):
        return state.new_job(self.repo, "Add a greeting API and CLI", size, "main", "codex:", "claude:")["id"]

    def run_cap(self, job_id, capability, target=None, **flags):
        return capabilities.run(job_id, capability, self.qk, target, **flags)

    def advance(self, job_id, until=None, agent=""):
        """Run `next` until a person must act, `until` matches, or the job is done; return the last reply."""
        reply = {}
        with mock.patch.dict(os.environ, {"FAKE_AGENT": agent}):
            for _ in range(40):
                step = policy.next_step(state.load(job_id))
                if until and step.startswith(until) or not step.split()[0] in policy.CAPABILITIES:
                    return step, reply
                words = step.split()
                target = next((word for word in words[2:] if not word.startswith("--")), None)
                reply = self.run_cap(job_id, words[0], target, expect_red="--expect-red" in words,
                                     fix="--fix" in words)
                if reply["status"] != "done":
                    return step, reply
        self.fail("the job did not settle")


class CapabilityTests(Sandbox):
    # --- end to end

    def test_medium_job_runs_to_one_pull_request_with_one_gate(self):
        job_id = self.new()
        step, _ = self.advance(job_id)
        self.assertEqual(step, f"approve {job_id} plan")
        self.assertEqual(capabilities.approve(job_id, "plan")["status"], "done")
        step, reply = self.advance(job_id)
        self.assertTrue(step.startswith("done: a person reviews and merges"), (step, reply))
        job = state.load(job_id)
        self.assertEqual(job["pr_url"], "https://github.com/example/project/pull/7")
        self.assertTrue(job["contract"]["red"])
        self.assertEqual(sorted(job["tasks"]), ["api", "cli"])
        pushed = sh(self.remote, "git", "log", "--format=%s", f"qk/{job['slug']}").splitlines()
        self.assertEqual(pushed[:2], ["merge task cli", "merge task api"])
        self.assertEqual(sorted(pushed[2:]), sorted([f"feat: api for {job['slug']}", f"feat: cli for {job['slug']}",
                                                     f"test: contract for {job['slug']}", "base"]))
        self.assertIn("pr create --base main --head qk/" + job["slug"], self.gh_log.read_text())
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")  # the user's checkout is untouched
        self.assertEqual(sh(self.repo, "git", "rev-parse", "--abbrev-ref", "HEAD"), "main")

    # --- order and refusals (A6)

    def test_every_capability_called_too_early_is_refused_with_the_next_step(self):
        job_id = self.new()
        for capability, target in (("contract.write", None), ("contract.review", None), ("build.run", "api"),
                                   ("verify.run", "integration"), ("review.run", "integration"),
                                   ("integrate.run", None), ("pr.open", None)):
            with self.subTest(capability=capability):
                reply = self.run_cap(job_id, capability, target)
                self.assertEqual((reply["status"], reply["next"]), ("refused", f"plan.write {job_id}"))
                self.assertTrue(reply["reason"])
        self.run_cap(job_id, "plan.write")
        reply = self.run_cap(job_id, "contract.write")
        self.assertEqual((reply["status"], reply["reason"], reply["next"]),
                         ("refused", "the plan is not approved", f"approve {job_id} plan"))
        refused = [entry for entry in state.load(job_id)["log"] if entry["status"] == "refused"]
        self.assertEqual(len(refused), 8)
        self.assertEqual(state.load(job_id)["contract"], None)  # nothing ran

    def test_a_manual_call_gets_the_same_refusal_as_the_coordinator(self):
        job_id = self.new()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(["--lock", str(self.lock), "build.run", job_id, "api"])
        reply = json.loads(output.getvalue())
        self.assertEqual((status, reply["status"], reply["next"]), (2, "refused", f"plan.write {job_id}"))

    def test_build_is_refused_until_the_contract_is_reviewed_and_red(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"contract.review")
        reply = self.run_cap(job_id, "build.run", "api")
        self.assertEqual((reply["status"], reply["reason"]), ("refused", "the contract has not passed its review"))
        self.run_cap(job_id, "contract.review")
        reply = self.run_cap(job_id, "build.run", "api")
        self.assertEqual(reply["reason"], "the contract tests were not shown to fail before the build")
        self.assertEqual(reply["next"], f"verify.run {job_id} integration --expect-red")

    # --- gates (A12)

    def test_large_job_stops_after_each_stage_and_pr_needs_the_integration_approval(self):
        job_id = self.new("large")
        stops = []
        for stage in ("plan", "contract", "build", "integrate"):
            step, _ = self.advance(job_id)
            stops.append(step)
            if stage == "integrate":
                reply = self.run_cap(job_id, "pr.open")
                self.assertEqual((reply["status"], reply["reason"]),
                                 ("refused", "the integration stage is not approved"))
            self.assertEqual(capabilities.approve(job_id, stage)["status"], "done")
        self.assertEqual(stops, [f"approve {job_id} {stage}" for stage in ("plan", "contract", "build", "integrate")])
        step, _ = self.advance(job_id)
        self.assertTrue(step.startswith("done:"), step)

    def test_large_build_waits_for_the_contract_approval(self):
        job_id = self.new("large")
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id)
        reply = self.run_cap(job_id, "build.run", "api")
        self.assertEqual((reply["status"], reply["reason"], reply["next"]),
                         ("refused", "the contract stage is not approved", f"approve {job_id} contract"))

    def test_approvals_follow_the_job_size_and_readiness(self):
        job_id = self.new()
        self.assertEqual(capabilities.approve(job_id, "plan")["reason"], "there is no plan to approve yet")
        self.advance(job_id)
        reply = capabilities.approve(job_id, "contract")
        self.assertEqual(reply["status"], "refused")
        self.assertIn("has no contract gate", reply["reason"])
        large = self.new("large")
        self.advance(large)
        self.assertIn("contract must pass its review", capabilities.approve(large, "contract")["reason"])

    def test_an_approval_holds_only_for_the_plan_it_was_given_on(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        plan = Path(state.load(job_id)["plan"]["file"])
        plan.write_text(plan.read_text() + "\nOne more requirement.\n")
        reply = self.run_cap(job_id, "contract.write")
        self.assertEqual(reply["status"], "refused")
        self.assertIn("changed after it was written", reply["reason"])

    # --- limits enforced in code

    def test_a_contract_that_already_passes_stops_the_job(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        step, reply = self.advance(job_id, agent="weak-contract")
        self.assertEqual(reply["red"], False)
        self.assertIn("already pass before any build", step)
        self.assertEqual(self.run_cap(job_id, "build.run", "api")["status"], "refused")

    def test_workers_may_not_leave_their_paths_or_commit(self):
        for deviation, reason in (("outside", "does not own: README.md"), ("commit", "only the harness commits")):
            with self.subTest(deviation=deviation):
                job_id = self.new()
                self.advance(job_id)
                capabilities.approve(job_id, "plan")
                step, reply = self.advance(job_id, agent=deviation)
                self.assertEqual(reply["status"], "blocked")
                self.assertIn(reason, reply["reason"])
                self.assertIsNone(state.load(job_id)["tasks"]["api"]["commit"])

    def test_one_fix_round_then_a_person_decides(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        step, _ = self.advance(job_id, until=f"build.run {job_id} api --fix", agent="broken")
        self.assertEqual(step, f"build.run {job_id} api --fix")
        step, reply = self.advance(job_id, agent="findings")
        self.assertEqual(step, "a person decides: task api has serious review findings that a fix round cannot take")
        task = state.load(job_id)["tasks"]["api"]
        self.assertEqual(task["fix_rounds"], 1)
        self.assertEqual(self.run_cap(job_id, "build.run", "api", fix=True)["reason"],
                         "task api has no fix round left or nothing to fix")

    def test_a_person_can_grant_one_more_fix_round(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"build.run {job_id} api --fix", agent="broken")
        step, _ = self.advance(job_id, agent="findings")
        self.assertTrue(step.startswith("a person decides: task api"), step)
        for target, reason in (("nope", "no task 'nope' in the plan"),
                               ("cli", "task cli has nothing a fix round can take"),
                               ("integration", "the integrated branch has nothing a fix round can take")):
            with self.subTest(target=target):
                reply = capabilities.grant_fix(job_id, target)
                self.assertEqual((reply["status"], reply["reason"]), ("refused", reason))
        reply = capabilities.grant_fix(job_id, "api", reason="the findings are worth one more try")
        self.assertEqual((reply["status"], reply["next"]), ("done", f"build.run {job_id} api --fix"))
        job = state.load(job_id)
        self.assertEqual(job["log"][-1]["capability"], "grant-fix api")
        self.assertEqual(job["log"][-1]["reason"], "the findings are worth one more try")
        self.assertEqual(capabilities.grant_fix(job_id, "api")["reason"], "task api can still take a fix round")
        step, reply = self.advance(job_id, agent="refix")
        self.assertNotIn("a person decides", step, reply)
        task = state.load(job_id)["tasks"]["api"]
        self.assertEqual((task["fix_rounds"], task["granted_rounds"]), (2, 1))
        self.assertTrue(policy.review_ok(task["review"]))

    def test_grant_fix_is_refused_when_the_job_cannot_use_it(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"build.run {job_id} api --fix", agent="broken")
        self.advance(job_id, agent="findings")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["grant-fix", job_id, "cli"]), 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "refused")
        plan = Path(state.load(job_id)["plan"]["file"])
        text = plan.read_text()
        plan.write_text(text + "\nchanged\n")
        self.assertEqual(capabilities.grant_fix(job_id, "api")["reason"],
                         "the plan changed after it was written; restore it first")
        plan.write_text(text)
        with state.locked(job_id) as job:
            job["integration"]["merged"] = True
        self.assertEqual(capabilities.grant_fix(job_id, "api")["reason"],
                         "the tasks are already integrated; only the integrated branch can take a fix round now")
        with state.locked(job_id) as job:  # a round granted before integration cannot be spent after it
            job["tasks"]["api"]["granted_rounds"] = 1
            self.assertFalse(policy.next_step(job).startswith(f"build.run {job_id} api"), policy.next_step(job))
        for capability in ("build.run", "verify.run", "review.run"):
            self.assertEqual(self.run_cap(job_id, capability, "api", fix=capability == "build.run")["reason"],
                             "the tasks are already integrated; work on the integrated branch")
        coordinator.abort(job_id, "enough")
        self.assertEqual(capabilities.grant_fix(job_id, "api")["reason"], "the job is aborted")

    def test_plans_must_be_checkable_and_split_cleanly(self):
        for deviation, reason in (("no-verify", "no non-empty ```qk-verify``` block"),
                                  ("overlap", "tasks own overlapping paths: api and cli")):
            with self.subTest(deviation=deviation):
                job_id = self.new()
                _, reply = self.advance(job_id, agent=deviation)
                self.assertEqual(reply["status"], "blocked")
                self.assertIn(reason, reply["reason"])
                self.assertIsNone(state.load(job_id)["plan"])

    def test_agent_turns_never_get_a_directory_that_holds_worktrees(self):
        log = self.tmp / "qk-calls.jsonl"
        job_id = self.new()
        with mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(log)}):
            self.advance(job_id)
            capabilities.approve(job_id, "plan")
            self.advance(job_id)
        job = state.load(job_id)
        worktrees = [Path(job["integration"]["worktree"])] + [Path(t["worktree"]) for t in job["tasks"].values()]
        turns = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        added = [Path(argv[i + 1]) for argv in turns if argv[:2] == ["agent", "run"]
                 for i, word in enumerate(argv) if word == "--add-dir"]
        self.assertTrue(added)
        for directory in added:
            for worktree in worktrees:
                self.assertNotIn(directory, [worktree, *worktree.parents], (directory, worktree))

    def test_build_prompts_name_the_tasks_own_checks(self):
        log = self.tmp / "qk-calls.jsonl"
        job_id = self.new()
        with mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(log)}):
            self.advance(job_id)
            capabilities.approve(job_id, "plan")
            self.advance(job_id)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        tasks = state.load(job_id)["tasks"]
        for name, task in tasks.items():
            with self.subTest(task=name):
                prompts = [call["stdin"] for call in calls if call["argv"][:2] == ["agent", "run"]
                           and f"to build task `{name}`" in call["stdin"]]
                self.assertTrue(prompts)
                self.assertTrue(task["checks"])
                for prompt in prompts:
                    self.assertIn(f"This task is checked only by: {'; '.join(task['checks'])}.", prompt)
                    self.assertIn("other tasks may fail in this worktree", prompt)
                    self.assertIn("name it in your summary instead", prompt)

    def test_contract_writes_only_under_its_paths(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        _, reply = self.advance(job_id, agent="contract-outside")
        self.assertEqual(reply["status"], "blocked")
        self.assertIn("outside its paths: src/helper.py", reply["reason"])
        self.assertIsNone(state.load(job_id)["contract"])

    def test_expect_red_is_only_for_the_contract_on_the_integration_branch(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"verify.run {job_id} api")
        reply = self.run_cap(job_id, "verify.run", "api", expect_red=True)
        self.assertEqual(reply["status"], "refused")
        self.assertIn("--expect-red only checks the contract", reply["reason"])
        self.assertTrue(state.load(job_id)["contract"]["red"])

    def test_integrate_merges_only_the_reviewed_commit(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"integrate.run {job_id}")
        worktree = Path(state.load(job_id)["tasks"]["api"]["worktree"])
        (worktree / "src" / "api" / "late.py").write_text("LATE = 1\n")
        sh(worktree, "git", "add", "-A")
        sh(worktree, "git", "commit", "-qm", "after review")
        reply = self.run_cap(job_id, "integrate.run")
        self.assertEqual(reply["status"], "blocked")
        self.assertIn("not the commit", reply["reason"])
        self.assertFalse(state.load(job_id)["integration"]["merged"])

    def test_changed_paths_names_both_sides_of_a_rename(self):
        (self.repo / "old.txt").write_text("x\n")
        sh(self.repo, "git", "add", "old.txt")
        sh(self.repo, "git", "commit", "-qm", "old")
        (self.repo / "old.txt").rename(self.repo / "src.txt")
        sh(self.repo, "git", "add", "-N", "src.txt")  # intent-to-add: the rename shows in the worktree column
        self.assertEqual(gitops.changed_paths(self.repo), ["old.txt", "src.txt"])
        sh(self.repo, "git", "add", "-A")  # staged rename
        self.assertEqual(gitops.changed_paths(self.repo), ["old.txt", "src.txt"])

    def test_owned_paths_must_be_normalised(self):
        self.assertIsNone(capabilities.clean_paths(["src/../README.md"]))
        self.assertIsNone(capabilities.clean_paths(["./src/"]))
        self.assertIsNone(capabilities.clean_paths(["/abs"]))
        self.assertEqual(capabilities.clean_paths(["src/api/", "tests"]), ["src/api/", "tests"])

    def test_plan_write_refuses_a_dirty_checkout(self):
        (self.repo / "README.md").write_text("edited\n")
        reply = self.run_cap(self.new(), "plan.write")
        self.assertEqual(reply["status"], "blocked")
        self.assertIn("uncommitted changes", reply["reason"])

    def test_start_refuses_a_reviewer_from_the_implementer_family(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(["--lock", str(self.lock), "start", "--repo", str(self.repo), "--task", "x",
                               "--impl", "claude:", "--review", "claude:opus"])
        self.assertEqual(status, 2)
        self.assertIn("same model family", errors.getvalue())

    def test_status_reports_the_next_step(self):
        job_id = self.new()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["status", job_id]), 0)
        self.assertEqual(json.loads(output.getvalue())["next"], f"plan.write {job_id}")


if __name__ == "__main__":
    unittest.main()

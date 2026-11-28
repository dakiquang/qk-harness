"""Offline consumer tests: real harness/Git/processes, scripted qk and GitHub transports only."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_capabilities import Sandbox, sh
from harness import capabilities, coordinator, delegation, publication, qk_cli, state


ROOT = Path(__file__).resolve().parents[1]
GH_SCRIPT = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ['FAKE_GH_LOG'])
with log.open('a') as handle:
    handle.write(' '.join(args) + '\\n')
command = ' '.join(args[:2])
if command != 'auth token' and os.environ.get('GH_HOST', 'github.com') != 'github.com':
    print('unexpected GitHub host', file=sys.stderr)
    sys.exit(1)
if command == 'auth token':
    print('fixture-publish-token')
elif command == 'api user':
    print(os.environ.get('FAKE_GH_LOGIN', 'publisher'))
elif args[:1] == ['api'] and '/actions/runs?' in args[1]:
    for index, item in enumerate(filter(None, os.environ.get('FAKE_GH_WORKFLOWS', 'completed').split(','))):
        status, _, conclusion = item.partition(':')
        print(json.dumps({'id': index + 1, 'status': status,
                          'conclusion': conclusion or ('success' if status == 'completed' else None)}))
elif command == 'repo view':
    print(json.dumps({'nameWithOwner': 'example/project', 'viewerPermission': 'WRITE'}))
elif command == 'pr list':
    print(json.dumps([{'url': 'https://github.com/example/project/pull/7', 'state': 'OPEN', 'baseRefName': 'main'}]
                     if 'pr create' in log.read_text() else []))
elif command == 'pr create':
    print('https://github.com/example/project/pull/7')
elif command == 'pr view':
    jobs = sorted((Path(os.environ['QK_HARNESS_STATE_DIR']) / 'jobs').glob('*/state.json'))
    job = json.loads(jobs[-1].read_text())
    result = os.environ.get('FAKE_GH_CI', 'SUCCESS')
    checks = [] if result == 'NONE' else [{'name': 'test', 'status': 'COMPLETED', 'conclusion': result}]
    if result == 'LEGACY':
        checks = [{'context': 'legacy-test', 'state': 'SUCCESS'}]
    print(json.dumps({'state': 'OPEN', 'baseRefName': job['base'], 'headRefName': job['integration']['branch'],
                      'headRefOid': os.environ.get('FAKE_GH_HEAD', job['integration']['commit']),
                      'statusCheckRollup': checks}))
else:
    sys.exit(1)
'''


class DelegationSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        # Only GitHub transport is redirected. All branch/worktree/commit operations use real Git.
        real_git = shutil.which("git")
        git_shim = self.tmp / "bin" / "git"
        git_shim.write_text('''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:3] == ['remote', 'get-url', 'origin']:
    print(os.environ.get('FAKE_PUBLIC_ORIGIN', 'https://github.com/example/project.git'))
    sys.exit(0)
if args[:1] == ['push']:
    os.environ['GIT_CONFIG_VALUE_2'] = os.environ['FAKE_LOCAL_REMOTE']
os.execv(REAL_GIT, [REAL_GIT, *args])
'''.replace("REAL_GIT", repr(real_git)))
        git_shim.chmod(0o755)
        gh = self.tmp / "bin" / "gh"
        gh.write_text(GH_SCRIPT)
        gh.chmod(0o755)
        patch = mock.patch.dict(os.environ, FAKE_LOCAL_REMOTE=str(self.remote), FAKE_QK_COST_USD="0.1")
        patch.start()
        self.addCleanup(patch.stop)

    def grant(self, job_id, budget=2, paths=("src", "tests")):
        return delegation.authorize(job_id, paths, budget, 0.1, "publisher")

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-m", "harness", "--lock", str(self.lock), *args],
                              cwd=ROOT, input="", capture_output=True, text=True, timeout=40,
                              start_new_session=True)


class DelegationTests(DelegationSandbox):

    def test_one_authorization_runs_large_to_published_checked_pr_in_fresh_process(self):
        job_id = self.new("large")
        result = self.invoke("authorize", job_id, "--path", "src", "--path", "tests", "--budget-usd", "2",
                             "--turn-estimate-usd", "0.1", "--gh-user", "publisher")
        self.assertEqual(result.returncode, 0, result.stderr)
        grant = json.loads(result.stdout)["authority"]
        # A separate process writes the plan, then exits. The next process needs no chat/gate approval.
        planned = self.invoke("plan.write", job_id)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        completed = self.invoke("run", job_id)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        reply = json.loads(completed.stdout)
        self.assertEqual((reply["stopped"], reply["ci"]["status"]), ("done", "success"))
        job = state.load(job_id)
        self.assertEqual(set(job["approvals"]), {"plan", "contract", "build", "integrate"})
        self.assertTrue(all(item["actor"] == "delegated" and item["authority"] == grant
                            for item in job["approvals"].values()))
        self.assertTrue(job["contract"]["red"])
        self.assertEqual(len(job["tasks"]), 2)
        pushed = sh(self.remote, "git", "rev-parse", job["integration"]["branch"])
        self.assertEqual(pushed, job["ci"]["head"])
        self.assertAlmostEqual(reply["budget"]["known_usd"], 0.4)
        self.assertEqual(set(reply["budget"]["unknown_specs"]), {"codex:"})
        self.assertNotIn("fixture-publish-token", json.dumps(job))
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.repo, "git", "branch", "--show-current"), "main")

    def test_manual_and_ungranted_delegated_approval_keep_the_gate(self):
        job_id = self.new("large")
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["next"], f"approve {job_id} plan")
        reply = capabilities.approve(job_id, "plan", delegated=True)
        self.assertEqual(reply["status"], "refused")
        self.assertEqual(state.load(job_id)["approvals"], {})

    def test_plan_outside_paths_or_model_specs_stops_before_contract(self):
        for role_change in (False, True):
            with self.subTest(role_change=role_change):
                job_id = self.new("large")
                self.grant(job_id, paths=("src", "tests") if role_change else ("src",))
                with mock.patch.dict(os.environ, {"FAKE_PLAN_ROLES": '{"api":{"impl":"codex:other"}}'
                                                  if role_change else '{}'}):
                    reply = coordinator.run(job_id, self.qk)
                self.assertEqual(reply["stopped"], "blocked", reply)
                job = state.load(job_id)
                self.assertIsNone(job["contract"])
                self.assertEqual(job["approvals"], {})
                self.assertEqual(len(job["usage"]), 1)

    def test_budget_survives_resume_and_explicit_increase_does_not_reset_spend(self):
        job_id = self.new("large")
        self.grant(job_id, budget=0.15)
        reply = coordinator.run(job_id, self.qk)
        self.assertIn("soft budget", reply["next"])
        before = state.load(job_id)
        self.assertAlmostEqual(delegation.costs(before)["known_usd"], 0.1)
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual(len(state.load(job_id)["usage"]), len(before["usage"]))
        self.grant(job_id, budget=2)
        self.assertAlmostEqual(delegation.costs(state.load(job_id))["known_usd"], 0.1)
        completed = coordinator.run(job_id, self.qk)
        self.assertEqual(completed["stopped"], "done", completed)
        self.assertAlmostEqual(completed["budget"]["known_usd"], 0.4)

    def test_parallel_reservations_prevent_oversubscribing_known_estimates(self):
        job_id = self.new("large")
        self.grant(job_id, budget=0.25)
        with state.locked(job_id) as job:
            job["tasks"] = {"api": {"running": {"estimated_usd": 0.2}}}
            with self.assertRaisesRegex(delegation.GrantError, "soft budget"):
                delegation.check_turn(job, "claude:")

    def test_failed_paid_turn_is_charged_before_retry(self):
        job_id = self.new("large")
        self.grant(job_id, budget=0.2)
        response = qk_cli.Result(2, {"contract": 1, "status": "blocked", "usage": {"cost_usd": 0.25}}, "failed")
        with mock.patch.object(self.qk, "agent_run", return_value=response) as run:
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "blocked")
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "refused")
            self.assertEqual(run.call_count, 1)
        self.assertAlmostEqual(delegation.costs(state.load(job_id))["known_usd"], 0.25)

    def test_missing_claude_cost_is_not_zero(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ):
            os.environ.pop("FAKE_QK_COST_USD", None)
            reply = coordinator.run(job_id, self.qk)
        self.assertIn("USD is missing", reply["next"])
        self.assertIn("claude:", reply["budget"]["unknown_specs"])
        self.assertIsNone(state.load(job_id)["pr_url"])

    def test_interrupted_turn_never_clears_its_marker_or_respends(self):
        job_id = self.new("large")
        self.grant(job_id)
        with state.locked(job_id) as job:
            job["usage"] = [{"spec": "codex:", "status": "running", "usage": None, "events": "interrupted"}]
        with mock.patch.object(self.qk, "agent_run") as run:
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked")
        run.assert_not_called()
        self.assertEqual(state.load(job_id)["usage"][0]["status"], "running")

    def test_revoked_or_changed_binding_stops_before_model_work(self):
        for change in ("revoke", "model", "origin"):
            with self.subTest(change=change):
                job_id = self.new("large")
                self.grant(job_id)
                if change == "revoke":
                    delegation.revoke(job_id, "stop")
                elif change == "model":
                    with state.locked(job_id) as job:
                        job["impl"] = "codex:other"
                with mock.patch.dict(os.environ, {"FAKE_PUBLIC_ORIGIN": "https://github.com/other/project.git"}
                                     if change == "origin" else {}), mock.patch.object(self.qk, "agent_run") as run:
                    self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "blocked")
                    run.assert_not_called()

    def test_wrong_publisher_stops_before_push_or_pr(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_LOGIN="other-user"):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked")
        self.assertNotIn("pr create", self.gh_log.read_text())
        self.assertIsNone(state.load(job_id)["pr_url"])
        remote_refs = sh(self.remote, "git", "for-each-ref", "--format=%(refname)")
        self.assertNotIn("refs/heads/qk/", remote_refs)

    def test_ci_failure_or_head_drift_does_not_report_done_or_allow_post_pr_build(self):
        for changed_head in (False, True):
            with self.subTest(changed_head=changed_head):
                job_id = self.new("large")
                self.grant(job_id)
                flags = {"FAKE_GH_HEAD": "f" * 40} if changed_head else {"FAKE_GH_CI": "FAILURE"}
                with mock.patch.dict(os.environ, flags):
                    reply = coordinator.run(job_id, self.qk)
                self.assertEqual(reply["stopped"], "blocked", reply)
                self.assertNotEqual(reply["ci"]["status"], "success")
                self.assertTrue(state.load(job_id)["pr_url"])
                self.assertEqual(self.run_cap(job_id, "build.run", "integration", fix=True)["status"], "refused")

    def test_absent_checks_are_not_success_and_resume_only_rechecks_ci(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_CI="NONE"), mock.patch.object(publication, "CI_TIMEOUT", 0):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked", reply)
        self.assertEqual(reply["ci"]["status"], "pending")
        turns = len(state.load(job_id)["usage"])
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "done", reply)
        self.assertEqual(len(state.load(job_id)["usage"]), turns)
        self.assertEqual(sum(line.startswith("pr create ") for line in self.gh_log.read_text().splitlines()), 1)

    def test_pending_workflow_cannot_complete_from_partial_successful_rollup(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_WORKFLOWS="completed,in_progress"), \
                mock.patch.object(publication, "CI_TIMEOUT", 0):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked", reply)
        self.assertEqual(reply["ci"]["status"], "pending")
        turns = len(state.load(job_id)["usage"])
        completed = coordinator.run(job_id, self.qk)
        self.assertEqual(completed["stopped"], "done", completed)
        self.assertEqual(len(state.load(job_id)["usage"]), turns)

    def test_completed_failed_workflow_blocks_despite_successful_rollup(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_WORKFLOWS="completed,completed:startup_failure"):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked", reply)
        self.assertEqual(reply["ci"]["status"], "failed")
        self.assertIn("a person decides", reply["next"])

    def test_legacy_status_without_action_workflows_can_complete(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_WORKFLOWS="", FAKE_GH_CI="LEGACY"):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual((reply["stopped"], reply["ci"]["status"]), ("done", "success"), reply)

    def test_publisher_preflight_failure_does_not_create_or_replace_authority(self):
        job_id = self.new("large")
        with mock.patch.dict(os.environ, FAKE_GH_LOGIN="other-user"):
            result = self.invoke("authorize", job_id, "--path", "src", "--path", "tests", "--budget-usd", "2",
                                 "--turn-estimate-usd", "0.1", "--gh-user", "publisher")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("delegation", state.load(job_id))
        original = self.grant(job_id)
        with mock.patch.dict(os.environ, FAKE_GH_LOGIN="other-user"), \
                self.assertRaisesRegex(delegation.GrantError, "different GitHub account"):
            self.grant(job_id, budget=3)
        job = state.load(job_id)
        self.assertEqual(job["delegation"]["sha256"], original["authority"])
        self.assertEqual(len(job["delegation_history"]), 1)
        self.assertEqual(job.get("usage", []), [])

    def test_invalid_budget_and_root_scope_do_not_create_authority(self):
        job_id = self.new("large")
        for amount in (0, -1, float("nan"), float("inf")):
            with self.subTest(amount=amount), self.assertRaises(delegation.GrantError):
                self.grant(job_id, budget=amount)
        for path in (".", "/tmp", "src/../other", ".git/config"):
            with self.subTest(path=path), self.assertRaises(delegation.GrantError):
                self.grant(job_id, paths=(path,))
        self.assertNotIn("delegation", state.load(job_id))


    def test_checkout_drift_before_planning_is_refused_without_spending(self):
        job_id = self.new("large")
        self.grant(job_id)
        (self.repo / "README.md").write_text("changed base\n")
        sh(self.repo, "git", "add", "README.md")
        sh(self.repo, "git", "commit", "-qm", "change base")
        with mock.patch.object(self.qk, "agent_run") as run:
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked")
        self.assertIn("base commit", reply["reason"])
        run.assert_not_called()

    def test_killed_runner_preserves_pending_cost_and_cannot_silently_retry(self):
        job_id = self.new("large")
        self.grant(job_id)
        model_pid = self.tmp / "model.pid"
        calls = self.tmp / "calls.jsonl"
        env = dict(os.environ, FAKE_QK_SLEEP="10", FAKE_QK_PID=str(model_pid), FAKE_QK_LOG=str(calls))
        process = subprocess.Popen([sys.executable, "-m", "harness", "--lock", str(self.lock), "run", job_id],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not model_pid.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(str(process.communicate()))
                time.sleep(0.02)
            self.assertTrue(model_pid.exists(), "the model subprocess did not start")
            self.assertEqual(state.load(job_id)["usage"][0]["status"], "running")
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
        resumed = self.invoke("run", job_id)
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["stopped"], "blocked")
        self.assertEqual(state.load(job_id)["usage"][0]["status"], "running")
        entries = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(sum(entry["argv"][:2] == ["agent", "run"] for entry in entries), 1)

    def test_runner_finishes_after_its_launcher_exits(self):
        job_id = self.new("large")
        self.grant(job_id)
        output = self.tmp / "independent.json"
        errors = self.tmp / "independent.err"
        launch = (
            "import subprocess,sys\n"
            "out=open(sys.argv[1],'w'); err=open(sys.argv[2],'w')\n"
            "p=subprocess.Popen(sys.argv[3:],stdin=subprocess.DEVNULL,stdout=out,stderr=err,"
            "start_new_session=True)\n"
            "print(p.pid)\n")
        launcher = subprocess.run([sys.executable, "-c", launch, str(output), str(errors), sys.executable,
                                   "-m", "harness", "--lock", str(self.lock), "run", job_id],
                                  cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        pid = int(launcher.stdout)
        deadline = time.monotonic() + 40
        reply = None
        try:
            while time.monotonic() < deadline:
                try:
                    reply = json.loads(output.read_text())
                    break
                except (FileNotFoundError, ValueError):
                    time.sleep(0.05)
            self.assertIsNotNone(reply, errors.read_text())
            self.assertEqual((reply["stopped"], reply["ci"]["status"]), ("done", "success"), reply)
            self.assertEqual(state.load(job_id)["approvals"]["integrate"]["actor"], "delegated")
        finally:
            if reply is None:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


    def test_ambient_enterprise_host_does_not_redirect_publication(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.dict(os.environ, GH_HOST="enterprise.invalid", GH_TOKEN="caller-token"):
            reply = coordinator.run(job_id, self.qk)
            self.assertEqual(os.environ["GH_HOST"], "enterprise.invalid")
            self.assertEqual(os.environ["GH_TOKEN"], "caller-token")
        self.assertEqual(reply["stopped"], "done", reply)
        self.assertEqual(reply["pr_url"], "https://github.com/example/project/pull/7")


if __name__ == "__main__":
    unittest.main()

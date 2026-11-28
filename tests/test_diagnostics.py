"""Failure evidence survives fixture cleanup without changing unittest's result semantics."""

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from failure_capture import FailureCaptureMixin, FailureCaptureResult  # noqa: E402
from harness import diagnostics  # noqa: E402


def _spawn_import_handshake(connection):
    """A spawn target, not a test runner; importing fixtures must not discover tests."""
    try:
        import failure_capture
        import test_capabilities
        import test_qk_cli
        connection.send((failure_capture.__name__, test_capabilities.__name__, test_qk_cli.__name__))
    finally:
        connection.close()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_projection_is_private_atomic_and_only_selected_metadata(self):
        parent_mode = stat.S_IMODE(self.root.stat().st_mode)
        context = {"job_id": "job-1", "capability": "pr.open", "target": "integration", "operation": "git push",
                   "status": "blocked", "reason": "rejected", "returncode": 128, "signal": None,
                   "timed_out": False, "cleanup": True, "expected_head": "abc", "observed_head": "def",
                   "authority_hash": "frozen", "effect": "unknown", "events": "turns/1.events.jsonl",
                   "request_id": "read-1", "attempt": 2, "env": {"SECRET": "not-for-evidence"},
                   "prompt": "raw prompt", "state": {"task": "raw task"}}
        directory = self.root / "job" / "diagnostics"
        path = diagnostics.capture(directory, context)
        self.assertIsInstance(path, Path)
        data = json.loads(path.read_text())
        self.assertEqual((data["schema"], data["job_id"], data["reason"]), (1, "job-1", "rejected"))
        self.assertEqual((data["returncode"], data["signal"], data["timed_out"], data["cleanup"]),
                         (128, None, False, True))
        self.assertEqual((data["expected_head"], data["observed_head"], data["authority_hash"]),
                         ("abc", "def", "frozen"))
        self.assertEqual((data["effect"], data["request_id"], data["source_attempt"]), ("unknown", "read-1", 2))
        self.assertEqual(data["events"], "turns/1.events.jsonl")
        self.assertIsNone(data["test"])
        for key in ("env", "prompt", "state"):
            self.assertNotIn(key, data)
        self.assertNotIn("raw prompt", path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), parent_mode)
        self.assertEqual(list(directory.iterdir()), [path])
        other = diagnostics.capture(directory, {"reason": "second attempt"})
        self.assertNotEqual(other, path)
        self.assertNotEqual(json.loads(other.read_text())["attempt"], data["attempt"])

    def test_evidence_references_are_job_relative_and_do_not_escape(self):
        directory = self.root / "job" / "diagnostics"
        path = diagnostics.capture(directory, {
            "events": str(directory.parent / "turns" / "attempt.events.jsonl"),
            "evidence": ["job.log", str(directory.parent / "receipt.json"), "../../outside"],
            "artifacts": {"path": str(self.root / "outside"), "events": "turns/run.jsonl"}})
        data = json.loads(path.read_text())
        self.assertEqual(data["events"], "turns/attempt.events.jsonl")
        self.assertEqual(data["evidence"], ["job.log", "receipt.json", None])
        self.assertEqual(data["artifacts"], {"path": None, "events": "turns/run.jsonl"})
        self.assertNotIn(str(self.root), path.read_text())

    def test_redaction_precedes_truncation_and_never_copies_secret_values(self):
        secret = "SENTINEL_PRIVATE_credential"
        context = {"reason": "https://user:pass@example.invalid/r?token=another-secret&ok=yes " + secret,
                   "stdout": "start " + secret + " x" * 10000 + " tail " + secret,
                   "stderr": "Authorization: Bearer abcdef", "cleanup": {"reason": secret, "prompt": secret},
                   "secrets": [secret]}
        path = diagnostics.capture(self.root / "capsules", context, secrets=(secret,))
        text = path.read_text()
        for value in (secret, "user:pass", "another-secret", "abcdef"):
            self.assertNotIn(value, text)
        data = json.loads(text)
        self.assertNotIn("secrets", data)
        self.assertNotIn("prompt", data["cleanup"])
        self.assertIn("[REDACTED]", data["reason"])
        self.assertIn("example.invalid/r", data["reason"])
        self.assertIn("truncated", data["stdout"])
        self.assertTrue(data["stdout"].startswith("start [REDACTED]"))
        self.assertTrue(data["stdout"].endswith("tail [REDACTED]"))

    def test_token_auth_operations_omit_stdout_even_without_known_token(self):
        for operation in ("gh auth token", ["gh", "auth", "token", "--hostname", "example.invalid"]):
            with self.subTest(operation=operation):
                path = diagnostics.capture(self.root / "capsules", {
                    "operation": operation, "stdout": "unregistered-token-value", "stderr": "auth rejected"})
                self.assertNotIn("unregistered-token-value", path.read_text())
                data = json.loads(path.read_text())
                self.assertIsNone(data["stdout"])
                self.assertTrue(data["stdout_omitted"])
                self.assertEqual(data["stderr"], "auth rejected")

    def test_stream_and_json_budgets_include_unicode_and_json_escaping(self):
        stream = "HEAD" + "😀\x00\n" * 30000 + "TAIL"
        context = {key: "\x00😀" * 30000 for key in (
            "job", "job_id", "test", "test_id", "capability", "target", "operation", "status", "reason",
            "code", "head", "authority", "authority_hash", "evidence", "artifacts", "request_id")}
        context.update(stdout=stream, stderr=stream)
        path = diagnostics.capture(self.root / "capsules", context)
        self.assertIsNotNone(path)
        self.assertLessEqual(path.stat().st_size, 65536)
        data = json.loads(path.read_text())
        self.assertTrue(data["truncated"])
        for key in ("stdout", "stderr"):
            self.assertLessEqual(len(data[key].encode("utf-8")), 4096)
            self.assertTrue(data[key].startswith("HEAD"))
            self.assertTrue(data[key].endswith("TAIL"))
            self.assertIn("[truncated]", data[key])
        self.assertEqual(context["stdout"], stream)  # The caller's complete output is never mutated.

    def test_safe_text_has_exact_byte_bound_and_redacts_before_head_tail(self):
        text = "abc" + "é" * 5000 + "xyz"
        bounded = diagnostics.safe_text(text, limit=101)
        self.assertLessEqual(len(bounded.encode("utf-8")), 101)
        self.assertTrue(bounded.startswith("abc"))
        self.assertTrue(bounded.endswith("xyz"))
        for limit in (0, 1, 10, 20):
            self.assertLessEqual(len(diagnostics.safe_text(text, limit=limit).encode("utf-8")), limit)
        self.assertEqual(diagnostics.safe_text("abc-secret", secrets="secret"), "abc-[REDACTED]")
        self.assertEqual(diagnostics.safe_text(b"bad\xff"), "bad�")

    def test_write_failure_returns_no_artifact_or_secret_exception(self):
        directory = self.root / "capsules"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), mock.patch.object(
                diagnostics.os, "replace", side_effect=OSError("disk full SECRET_SENTINEL")):
            result = diagnostics.capture(directory, {"reason": "original failure"})
        self.assertIsNone(result)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertIn("diagnostic_write_failed", errors.getvalue())
        self.assertNotIn("SECRET_SENTINEL", errors.getvalue())

    def test_restrictive_umask_does_not_change_capsule_permissions(self):
        previous = os.umask(0o777)
        try:
            path = diagnostics.capture(self.root / "capsules", {"reason": "original"})
        finally:
            os.umask(previous)
        self.assertIsNotNone(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_existing_public_directory_is_not_chmodded_or_used(self):
        directory = self.root / "public"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertIsNone(diagnostics.capture(directory, {"reason": "original"}))
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertIn("diagnostic_write_failed", errors.getvalue())

    def test_symlink_capsule_directory_is_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(diagnostics.capture(link, {"reason": "original"}))
        self.assertEqual(list(real.iterdir()), [])


class FailureCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.artifacts = self.root / "artifacts"
        settings = mock.patch.dict(os.environ, {"QK_HARNESS_TEST_ARTIFACT_DIR": str(self.artifacts)})
        settings.start()
        self.addCleanup(settings.stop)

    def capsules(self):
        return sorted(self.artifacts.glob("failures/*.json"))

    def run_child(self, child, result=None):
        result = result if result is not None else unittest.TestResult()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            returned = child.run(result)
        self.assertIs(returned, result)
        return result, errors.getvalue()

    def test_failure_is_captured_while_fixture_exists_and_survives_cleanup(self):
        outer = self

        class Child(FailureCaptureMixin, unittest.TestCase):
            def setUp(self):
                directory = tempfile.TemporaryDirectory()
                self.tmp = Path(directory.name)
                self.addCleanup(directory.cleanup)
                self._diagnostic_context = {"job": "fixture-job", "reason": "READ permission refused"}

            def runTest(self):
                (self.tmp / "exists").write_text("evidence")
                outer.fixture = self.tmp
                self.fail("original assertion")

        observed = []
        real_capture = diagnostics.capture

        def capture_while_live(directory, context, **kwargs):
            observed.append((self.fixture / "exists").read_text())
            return real_capture(directory, context, **kwargs)

        with mock.patch.object(diagnostics, "capture", side_effect=capture_while_live):
            result, output = self.run_child(Child())
        self.assertEqual(observed, ["evidence"])
        self.assertFalse(self.fixture.exists())
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 0))
        self.assertIn("original assertion", result.failures[0][1])
        path, = self.capsules()
        data = json.loads(path.read_text())
        self.assertIn("original assertion", data["reason"])
        self.assertIn("READ permission refused", data["reason"])
        self.assertEqual(data["job"], "fixture-job")
        self.assertIn(str(path), output)
        self.assertIn("failure capsule:", output)
        self.assertNotIn(str(self.fixture), str(path))

    def test_error_and_failed_subtests_forward_exact_result_counts(self):
        class Child(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                with self.subTest(case="pass"):
                    self.assertTrue(True)
                with self.subTest(case="assertion"):
                    self.fail("subtest assertion")
                with self.subTest(case="exception"):
                    raise ValueError("subtest error")
                raise RuntimeError("main error")

        result, _ = self.run_child(Child())
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 2))
        self.assertFalse(result.wasSuccessful())
        data = [json.loads(path.read_text()) for path in self.capsules()]
        self.assertEqual(len(data), 3)
        self.assertEqual(sorted(item["reason"] for item in data),
                         ["main error", "subtest assertion", "subtest error"])
        self.assertEqual(sorted(item["status"] for item in data), ["error", "error", "failure"])
        self.assertTrue(any("case='assertion'" in item["test"] for item in data))

    def test_failfast_and_result_attributes_are_forwarded(self):
        reached = []

        class Child(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                with self.subTest(case="stop"):
                    self.fail("stop now")
                reached.append("must not run")

        result = unittest.TestResult()
        result.failfast = True
        result, _ = self.run_child(Child(), result)
        self.assertTrue(result.shouldStop)
        self.assertEqual(reached, [])
        self.assertEqual((result.testsRun, len(result.failures), len(self.capsules())), (1, 1, 1))
        proxy = FailureCaptureResult(result, lambda *args: None)
        proxy.buffer = True
        self.assertTrue(result.buffer)
        self.assertIs(proxy.failures, result.failures)

    def test_success_skip_and_expected_failure_create_no_capsules(self):
        class Child(FailureCaptureMixin, unittest.TestCase):
            def test_success(self):
                with self.subTest(case="pass"):
                    pass

            @unittest.skip("not applicable")
            def test_skip(self):
                pass

            @unittest.expectedFailure
            def test_expected(self):
                self.fail("expected")

        result = unittest.TestResult()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            unittest.defaultTestLoader.loadTestsFromTestCase(Child).run(result)
        self.assertEqual(result.testsRun, 3)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual((len(result.skipped), len(result.expectedFailures)), (1, 1))
        self.assertFalse(self.artifacts.exists())
        self.assertEqual(output.getvalue(), "")

    def test_default_result_lifecycle_is_preserved(self):
        events = []

        class Result(unittest.TestResult):
            def startTestRun(self):
                events.append("start")

            def stopTestRun(self):
                events.append("stop")

        class Child(FailureCaptureMixin, unittest.TestCase):
            def defaultTestResult(self):
                return Result()

            def runTest(self):
                self.fail("default result failure")

        with contextlib.redirect_stderr(io.StringIO()):
            result = Child().run()
        self.assertEqual(events, ["start", "stop"])
        self.assertEqual((result.testsRun, len(result.failures), len(self.capsules())), (1, 1, 1))

    def test_observer_failure_does_not_replace_original_result(self):
        class Child(unittest.TestCase):
            def runTest(self):
                self.fail("original assertion")

        result = unittest.TestResult()
        observer = mock.Mock(side_effect=RuntimeError("observer secret"))
        with contextlib.redirect_stderr(io.StringIO()) as output:
            Child().run(FailureCaptureResult(result, observer))
        self.assertEqual((result.testsRun, len(result.failures)), (1, 1))
        self.assertIn("original assertion", result.failures[0][1])
        self.assertIn("diagnostic_write_failed", output.getvalue())
        self.assertNotIn("observer secret", output.getvalue())

    def test_setup_error_and_cleanup_error_are_not_swallowed(self):
        class SetupError(FailureCaptureMixin, unittest.TestCase):
            def setUp(self):
                raise ValueError("setup failed")

            def runTest(self):
                raise AssertionError("not reached")

        class CleanupError(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                self.addCleanup(self.cleanup_error)

            def cleanup_error(self):
                raise ValueError("cleanup failed")

        result = unittest.TestResult()
        self.run_child(SetupError(), result)
        self.run_child(CleanupError(), result)
        self.assertEqual((result.testsRun, len(result.errors), len(self.capsules())), (2, 2, 2))
        self.assertIn("setup failed", result.errors[0][1])
        self.assertIn("cleanup failed", result.errors[1][1])

    def test_capture_write_failure_preserves_original_assertion(self):
        class Child(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                self.fail("original failure remains")

        with mock.patch.object(diagnostics.os, "replace", side_effect=OSError("full disk private detail")):
            result, output = self.run_child(Child())
        self.assertEqual((result.testsRun, len(result.failures)), (1, 1))
        self.assertIn("original failure remains", result.failures[0][1])
        self.assertIn("diagnostic_write_failed", output)
        self.assertNotIn("failure capsule:", output)
        self.assertNotIn("private detail", output)
        self.assertEqual(self.capsules(), [])

    def test_configured_roots_in_fixture_or_checkout_are_rejected(self):
        outer = self

        class Child(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                self.tmp = outer.root / "fixture"
                self.tmp.mkdir(exist_ok=True)
                self.fail("original failure")

        for root in (self.root / "fixture" / "artifacts", Path(__file__).resolve().parents[1] / "artifacts"):
            with self.subTest(root=root), mock.patch.dict(os.environ, {"QK_HARNESS_TEST_ARTIFACT_DIR": str(root)}):
                result, output = self.run_child(Child())
                self.assertEqual(len(result.failures), 1)
                self.assertIn("diagnostic_write_failed", output)
                self.assertNotIn("failure capsule:", output)
                self.assertFalse(root.exists())

    def test_default_artifacts_survive_temp_fixture_cleanup(self):
        class Child(FailureCaptureMixin, unittest.TestCase):
            def setUp(self):
                directory = tempfile.TemporaryDirectory()
                self.tmp = Path(directory.name)
                self.addCleanup(directory.cleanup)

            def runTest(self):
                self.fail("default lifetime")

        child = Child()
        with mock.patch.dict(os.environ):
            os.environ.pop("QK_HARNESS_TEST_ARTIFACT_DIR", None)
            self.run_child(child)
        directory = child._failure_artifact_directory
        self.addCleanup(directory.rmdir)
        paths = list(directory.glob("*.json"))
        for path in paths:
            self.addCleanup(path.unlink)
        self.assertFalse(child.tmp.exists())
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["reason"], "default lifetime")
        self.assertNotIn(child.tmp, directory.parents)
        self.assertNotIn(Path(__file__).resolve().parents[1], directory.parents)

    def test_known_process_secrets_and_credential_paths_are_redacted_in_report(self):
        secret = "SENTINEL_TEST_TOKEN"
        artifact_root = self.root / secret

        class Child(FailureCaptureMixin, unittest.TestCase):
            def runTest(self):
                self._diagnostic_context = {"stdout": secret, "stderr": "https://name:password@host.invalid"}
                self.fail("refused " + secret + " https://user:pass@host.invalid")

        with mock.patch.dict(os.environ, {"QK_HARNESS_TEST_ARTIFACT_DIR": str(artifact_root), "GH_TOKEN": secret}):
            result, output = self.run_child(Child())
        self.assertEqual(len(result.failures), 1)
        path, = (artifact_root / "failures").glob("*.json")
        for text in (path.read_text(), output):
            for value in (secret, "user:pass", "name:password"):
                self.assertNotIn(value, text)
            self.assertIn("[REDACTED]", text)

    def test_existing_fixture_owners_capture_before_their_cleanup(self):
        from test_capabilities import Sandbox
        from test_qk_cli import QkCliTests

        class CapabilityChild(Sandbox):
            def runTest(self):
                job_id = self.new()
                self.run_cap(job_id, "build.run", "api")
                self.fail("sandbox failure")

        class QkChild(QkCliTests):
            def runTest(self):
                self.fail("qk fixture failure")

        for child in (CapabilityChild(), QkChild("runTest")):
            with self.subTest(fixture=type(child).__name__):
                result, _ = self.run_child(child)
                self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 0))
                self.assertFalse(child.tmp.exists())
        data = [json.loads(path.read_text()) for path in self.capsules()]
        self.assertEqual(len(data), 2)
        sandbox = next(item for item in data if item["capability"] == "build.run")
        self.assertTrue(sandbox["job"])
        self.assertEqual(sandbox["target"], "api")
        self.assertIn("plan", sandbox["reason"])

    def test_spawn_import_handshake_does_not_discover_or_execute_fixtures(self):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_spawn_import_handshake, args=(sender,))
        log = self.root / "unexpected-agent-calls.jsonl"
        state = self.root / "unexpected-state"
        try:
            with mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(log), "QK_HARNESS_STATE_DIR": str(state)}):
                process.start()
            sender.close()
            self.assertTrue(receiver.poll(15), "spawn import handshake timed out")
            self.assertEqual(receiver.recv(), ("failure_capture", "test_capabilities", "test_qk_cli"))
            process.join(15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertFalse(log.exists())
            self.assertFalse(state.exists())
            self.assertFalse(self.artifacts.exists())
        finally:
            sender.close()
            receiver.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                process.close()


if __name__ == "__main__":
    unittest.main()

"""Bounded native commands with real processes; no GitHub or model calls."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from harness import gitops, qk_cli


class NativeCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        patch = mock.patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("TERM_GRACE", "KILL_GRACE"):
            patch = mock.patch.object(gitops, name, 0.2)
            patch.start()
            self.addCleanup(patch.stop)

    def shim(self, program, source):
        path = self.bin / program
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o755)

    def ready_line(self):
        return "from pathlib import Path\nPath(" + repr(str(self.root / "ready")) + ").touch()\n"

    @contextmanager
    def after_ready(self):
        """Start the timeout fault only once the real child installed its behavior."""
        original = subprocess.Popen
        def launch(*args, **kwargs):
            proc = original(*args, **kwargs)
            end = time.monotonic() + 10
            try:
                while not (self.root / "ready").exists():
                    if proc.poll() is not None:
                        out, err = proc.communicate(timeout=1)
                        self.fail("fixture exited before readiness: " + out + err)
                    if time.monotonic() >= end:
                        self.fail("fixture did not reach readiness")
                    time.sleep(0.01)
            except BaseException:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=2)
                raise
            return proc
        with mock.patch.object(gitops.subprocess, "Popen", side_effect=launch):
            yield

    def test_success_output_and_nonzero_completed_process_remain_complete(self):
        self.shim("git", "import sys\nprint('é' * 10000)\nprint('native failure', file=sys.stderr)\nsys.exit(3)\n")
        result = gitops.git(self.root, "status", check=False)
        self.assertEqual((result.returncode, result.stdout), (3, "é" * 10000 + "\n"))
        with self.assertRaises(gitops.GitError) as failed:
            gitops.git(self.root, "status")
        self.assertEqual(failed.exception.details["returncode"], 3)
        self.assertIn("native failure", str(failed.exception))
        self.shim("gh", "print('x' * 20000)\n")
        self.assertEqual(gitops.gh(self.root, "pr", "view"), "x" * 20000)

    def test_read_timeout_kills_owned_process_and_is_not_a_write(self):
        self.shim("git", "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  + self.ready_line() + "time.sleep(20)\n")
        with self.after_ready(), self.assertRaises(gitops.GitError) as failed:
            gitops.git(self.root, "status", timeout=0.2)
        error = failed.exception
        self.assertTrue(error.timed_out)
        self.assertTrue(error.cleanup_known)
        self.assertFalse(error.uncertain)
        self.assertEqual(error.details["signal"], signal.SIGKILL)

    def test_write_timeout_preserves_side_effect_without_retry(self):
        marker = self.root / "writes"
        self.shim("git", "from pathlib import Path\nimport time\n"
                  + "p=Path(" + repr(str(marker)) + ")\np.write_text(p.read_text()+'write\\n' if p.exists() else 'write\\n')\n"
                  + self.ready_line() + "time.sleep(20)\n")
        with self.after_ready(), self.assertRaises(gitops.GitError) as failed:
            gitops.git(self.root, "push", timeout=0.2)
        self.assertTrue(failed.exception.uncertain)
        self.assertEqual(marker.read_text(), "write\n")

    def test_deadline_exhausted_does_not_launch_a_command(self):
        marker = self.root / "launched"
        self.shim("gh", "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
        with self.assertRaises(gitops.GitError) as failed:
            gitops.gh(self.root, "pr", "view", deadline=time.monotonic() - 1)
        self.assertTrue(failed.exception.timed_out)
        self.assertFalse(marker.exists())

    def test_error_redacts_child_token_and_token_lookup_never_captures_stdout(self):
        token = "private-child-credential"
        self.shim("gh", "import os,sys\nprint(os.environ['GH_TOKEN'])\nprint(os.environ['GH_TOKEN'],file=sys.stderr)\nsys.exit(1)\n")
        for args in (("api", "user"), ("auth", "token")):
            with self.subTest(args=args), self.assertRaises(gitops.GitError) as failed:
                gitops.gh(self.root, *args, env=dict(os.environ, GH_TOKEN=token))
            self.assertNotIn(token, str(failed.exception) + str(failed.exception.details))
            if args[0] == "auth":
                self.assertEqual(failed.exception.details["stdout"], "")

    def test_bootstrap_git_timeout_preserves_qk_error_cause(self):
        self.shim("git", "import time\ntime.sleep(20)\n")
        with mock.patch.object(gitops, "READ_TIMEOUT", 0.2), self.assertRaises(qk_cli.QkError) as failed:
            qk_cli._git(self.root, "rev-parse", "HEAD")
        self.assertIsInstance(failed.exception.__cause__, gitops.GitError)
        self.assertTrue(failed.exception.__cause__.timed_out)

    def test_timeout_closes_inherited_pipes_and_stops_child_writer(self):
        child_file = self.root / "child"
        script = "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        script += "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])\n"
        script += "Path(" + repr(str(child_file)) + ").write_text(str(child.pid))\n" + self.ready_line() + "child.wait()\n"
        self.shim("git", script)
        with self.after_ready(), self.assertRaises(gitops.GitError) as failed:
            gitops.git(self.root, "status", timeout=0.3)
        self.assertTrue(failed.exception.timed_out)
        child = int(child_file.read_text())
        end = time.monotonic() + 3
        while True:
            proc = subprocess.run(["ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True)
            if not proc.stdout.strip() or proc.stdout.strip().startswith("Z"):
                break
            if time.monotonic() > end:
                os.kill(child, signal.SIGKILL)
                self.fail("owned child writer survived native cleanup")
            time.sleep(0.02)

    def test_bootstrap_write_timeout_preserves_a_private_partial_not_a_valid_pin(self):
        cache = self.root / "cache"
        self.shim("git", self.ready_line() + "import time\ntime.sleep(20)\n")
        lock = {"repo": str(self.root), "tag": "v1", "sha": "a" * 40, "contract": 1}
        with self.after_ready(), mock.patch.object(gitops, "WRITE_TIMEOUT", 0.2), \
                self.assertRaises(qk_cli.QkError) as failed:
            qk_cli.fetch(lock, cache)
        self.assertIn("preserved", str(failed.exception))
        self.assertFalse((cache / "qk" / lock["sha"]).exists())
        partials = list((cache / "qk").glob(lock["sha"] + ".partial-*"))
        self.assertEqual(len(partials), 1)
        self.assertTrue(list((partials[0] / "diagnostics").glob("*.json")))

    def test_keyboard_interrupt_cleans_owned_session_and_preserves_interruption(self):
        self.shim("git", self.ready_line() + "import time\ntime.sleep(20)\n")
        processes = []
        with self.after_ready():
            original_launch = subprocess.Popen
            def launch(*args, **kwargs):
                proc = original_launch(*args, **kwargs)
                processes.append(proc)
                original_communicate = proc.communicate
                first = [True]
                def communicate(*args, **kwargs):
                    if first[0]:
                        first[0] = False
                        raise KeyboardInterrupt()
                    return original_communicate(*args, **kwargs)
                proc.communicate = communicate
                return proc
            try:
                with mock.patch.object(gitops.subprocess, "Popen", side_effect=launch), \
                        self.assertRaises(KeyboardInterrupt) as interrupted:
                    gitops.git(self.root, "status")
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(interrupted.exception.native_error.cleanup_known)
                self.assertTrue(interrupted.exception.native_error.uncertain)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=2)

    def test_bad_native_encoding_is_blocked_with_confirmed_cleanup_not_a_retryable_timeout(self):
        self.shim("gh", "import os\nos.write(1,b'\\xff')\n")
        with self.assertRaises(gitops.GitError) as failed:
            gitops.gh(self.root, "pr", "view")
        error = failed.exception
        self.assertIsInstance(error.__cause__, UnicodeDecodeError)
        self.assertTrue(error.cleanup_known)
        self.assertTrue(error.uncertain)
        self.assertFalse(error.timed_out)

    def test_credentials_only_in_child_environment_do_not_escape_in_errors(self):
        for key in ("SERVICE_CREDENTIAL", "AUTHORIZATION"):
            with self.subTest(key=key):
                value = "private-child-only-sensitive-value"
                self.shim("gh", "import os,sys\nprint(os.environ[" + repr(key) + "],file=sys.stderr)\nsys.exit(1)\n")
                with self.assertRaises(gitops.GitError) as failed:
                    gitops.gh(self.root, "api", "user", env=dict(os.environ, **{key: value}))
                self.assertNotIn(value, str(failed.exception) + str(failed.exception.details))


if __name__ == "__main__":
    unittest.main()

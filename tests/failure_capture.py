"""Capture unittest failures through its public result protocol, before fixture cleanup."""

import os
from pathlib import Path
import sys
import tempfile

from harness import diagnostics


_CHECKOUT = Path(__file__).resolve().parents[1]


def _process_secrets():
    return diagnostics.credential_values(os.environ)


def _inside(path, root):
    return path == root or root in path.parents


def _artifact_directory(test):
    forbidden = [_CHECKOUT]
    for name in ("tmp", "repo", "upstream"):
        value = getattr(test, name, None)
        if isinstance(value, (str, Path)):
            forbidden.append(Path(value).resolve())
    configured = os.environ.get("QK_HARNESS_TEST_ARTIFACT_DIR")
    if configured:
        root = Path(configured).expanduser().resolve()
        if any(_inside(root, path) for path in forbidden):
            raise ValueError("test artifact directory must be outside fixture and checkout")
        # capture creates the root with private permissions, without changing an existing root.
        return root / "failures"
    root = Path(tempfile.mkdtemp(prefix="qk-harness-failures-")).resolve()
    if any(_inside(root, path) for path in forbidden):
        root.rmdir()
        raise ValueError("temporary artifact directory is inside fixture or checkout")
    return root


def _report(message, secrets):
    try:
        print(diagnostics.safe_text(message, secrets).replace("\n", "\\n").replace("\r", "\\r"), file=sys.stderr)
    except Exception:
        pass


class FailureCaptureResult:
    """Transparent public TestResult proxy; only failed events get an additional observer."""

    def __init__(self, result, observer):
        object.__setattr__(self, "_result", result)
        object.__setattr__(self, "_observer", observer)

    def __getattr__(self, name):
        return getattr(self._result, name)

    def __setattr__(self, name, value):
        setattr(self._result, name, value)

    def _observe(self, test, error, status):
        try:
            self._observer(test, error, status)
        except Exception:
            _report("diagnostic_write_failed", ())

    def addFailure(self, test, err):
        self._observe(test, err, "failure")
        return self._result.addFailure(test, err)

    def addError(self, test, err):
        self._observe(test, err, "error")
        return self._result.addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            status = "failure" if issubclass(err[0], test.failureException) else "error"
            self._observe(subtest, err, status)
        return self._result.addSubTest(test, subtest, err)


class FailureCaptureMixin:
    """Opt-in TestCase mixin; no discovery, suite, or process runs at import time."""

    def failure_context(self):
        return dict(getattr(self, "_diagnostic_context", {}))

    def failure_secrets(self):
        return _process_secrets()

    def _capture_failure(self, test, error, status):
        secrets = self.failure_secrets()
        try:
            context = self.failure_context()
            reason = str(error[1])
            if context.get("reason"):
                reason += "\ncontext: " + str(context["reason"])
            context.update(test=test.id(), status=status, reason=reason)
            exception = error[1]
            for key in ("operation", "returncode", "signal", "timed_out", "cleanup", "stdout", "stderr"):
                if hasattr(exception, key):
                    context[key] = getattr(exception, key)
            directory = getattr(self, "_failure_artifact_directory", None)
            if directory is None:
                directory = _artifact_directory(self)
                self._failure_artifact_directory = directory
            path = diagnostics.capture(directory, context, secrets=secrets)
            if path is not None:
                _report("failure capsule: {}: {}: {}".format(test.id(), context["reason"], path), secrets)
        except Exception:
            _report("diagnostic_write_failed", secrets)

    def run(self, result=None):
        own_result = result is None
        if own_result:
            result = self.defaultTestResult()
            start = getattr(result, "startTestRun", None)
            if start is not None:
                start()
        try:
            super().run(FailureCaptureResult(result, self._capture_failure))
        finally:
            if own_result:
                stop = getattr(result, "stopTestRun", None)
                if stop is not None:
                    stop()
        return result

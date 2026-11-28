"""Bounded, private failure capsules; callers supply metadata, never raw job state."""

import datetime
import math
import json
import os
from pathlib import Path
import re
import sys
import uuid


MAX_CAPSULE_BYTES = 64 * 1024
STREAM_BYTES = 4096
_MARKER = "\n...[truncated]...\n"
_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/?#]*@", re.I)
_QUERY_SECRET = re.compile(
    r"([?&](?:access_token|token|api[_-]?key|password|secret|signature|"
    r"x-amz-(?:credential|signature|security-token))=)[^\s&#]*", re.I)
_AUTHORIZATION = re.compile(r"(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+", re.I)
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|(?:^|_)API_KEY$|AUTHORIZATION", re.I)
_FIELDS = (
    "job", "job_id", "test", "test_id", "capability", "target", "operation", "status", "reason", "code",
    "returncode", "signal", "timed_out", "cleanup", "head", "expected_head", "observed_head", "authority",
    "authority_hash", "evidence", "artifacts", "attempts", "effect", "events", "request_id",
    "deadline_monotonic", "remaining_s", "elapsed_s",
)
_NESTED_FIELDS = frozenset((
    "status", "reason", "code", "returncode", "signal", "timed_out", "cleanup", "attempt", "attempt_id",
    "operation", "expected", "observed", "expected_head", "observed_head", "head", "base", "sha256", "hash",
    "confirmed", "complete", "attempted", "state", "stopped", "known", "seconds", "timeout", "grace",
    "path", "ref", "events", "stderr", "diagnostic", "artifact", "authority_hash",
    "deadline_monotonic", "remaining_s", "elapsed_s",
))


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return str(value)
    except Exception:
        return "[unprintable]"

def credential_values(environment):
    """Values known to be sensitive in this invocation's environment; never persist the mapping."""
    return tuple(value for key, value in environment.items() if value and _SECRET_NAME.search(key))



def _known_secrets(values):
    if isinstance(values, (str, bytes)):
        values = (values,)
    ambient = credential_values(os.environ)
    return sorted({_text(value) for value in (*values, *ambient) if value is not None and _text(value)},
                  key=len, reverse=True)


def _redact(value, secrets):
    text = _text(value)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _USERINFO.sub(r"\1[REDACTED]@", text)
    text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
    return _AUTHORIZATION.sub(r"\1[REDACTED]", text)


def _bound(text, limit):
    limit = max(0, int(limit))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    marker = _MARKER.encode("utf-8")
    if limit < len(marker):
        return marker[:limit].decode("utf-8", errors="ignore")
    remaining = limit - len(marker)
    head = (remaining + 1) // 2
    tail = remaining // 2
    return (encoded[:head].decode("utf-8", errors="ignore") + _MARKER
            + (encoded[-tail:].decode("utf-8", errors="ignore") if tail else ""))


def safe_text(value, secrets=(), limit=4096):
    """Redact known credentials before taking a UTF-8 byte-bounded head and tail."""
    return _bound(_redact(value, _known_secrets(secrets)), limit)


def _project(value, secrets, budget, depth=0):
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int and abs(value) < 2 ** 63:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if depth < 2 and isinstance(value, dict):
        items = [(key, item) for key, item in value.items() if key in _NESTED_FIELDS][:8]
        return {key: _project(item, secrets, max(16, budget // max(1, len(items))), depth + 1)
                for key, item in items}
    if depth < 2 and isinstance(value, (tuple, list)):
        items = value[:8]
        return [_project(item, secrets, max(16, budget // max(1, len(items))), depth + 1)
                for item in items]
    if isinstance(value, (dict, list, tuple)):
        return "[omitted]"
    return safe_text(value, secrets, limit=budget)


def _token_operation(operation):
    words = re.findall(r"[a-z0-9]+", _text(operation).lower())
    return "token" in words and "auth" in words


def _reference(value, job_directory):
    if isinstance(value, (str, Path)):
        text = str(value)
        path = Path(text)
        if path.is_absolute():
            try:
                return str(path.resolve().relative_to(job_directory.resolve()))
            except ValueError:
                return None
        if ".." in path.parts:
            return None
        return text
    if isinstance(value, (list, tuple)):
        return [_reference(item, job_directory) for item in value[:8]]
    if isinstance(value, dict):
        return {key: _reference(item, job_directory) if key in
                ("path", "ref", "events", "artifact", "diagnostic") else item
                for key, item in value.items() if key in _NESTED_FIELDS}
    return None


def _truncated(value):
    if isinstance(value, str):
        return _MARKER in value
    if isinstance(value, dict):
        return any(_truncated(item) for item in value.values())
    if isinstance(value, list):
        return any(_truncated(item) for item in value)
    return False


def _payload(context, secrets):
    budget = 2048
    while True:
        data = {"schema": 1, "attempt": uuid.uuid4().hex,
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        data.update({key: _project(context.get(key), secrets, budget) for key in _FIELDS})
        if context.get("attempt") is not None:
            data["source_attempt"] = _project(context["attempt"], secrets, budget)
        omitted = _token_operation(context.get("operation"))
        data["stdout"] = None if omitted else safe_text(context.get("stdout"), secrets, STREAM_BYTES)
        data["stderr"] = safe_text(context.get("stderr"), secrets, STREAM_BYTES)
        data["stdout_omitted"] = omitted
        data["truncated"] = budget < 2048 or _truncated(data)
        encoded = (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) <= MAX_CAPSULE_BYTES:
            return encoded
        budget //= 2
        if budget < 16:
            raise ValueError("diagnostic projection exceeds byte budget")


def _write_failed():
    try:
        print("diagnostic_write_failed", file=sys.stderr)
    except Exception:
        pass


def _private_directory(directory):
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        except FileExistsError:
            pass
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise OSError("diagnostic directory is not private")


def capture(directory, context, *, secrets=()):
    """Atomically write a capsule, or report diagnostic_write_failed without masking the original error."""
    temporary = None
    try:
        values = _known_secrets(secrets)
        directory = Path(directory)
        context = dict(context)
        for key in ("events", "evidence", "artifacts"):
            if context.get(key) is not None:
                context[key] = _reference(context[key], directory.parent)
        payload = _payload(context, values)
        _private_directory(directory)
        name = uuid.uuid4().hex
        path = directory / (name + ".json")
        temporary = directory / ("." + name + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(temporary), flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        return path
    except Exception:
        _write_failed()
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass

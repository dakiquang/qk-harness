"""Git and shell steps the harness itself performs. Agents never run these; only the harness writes git."""

import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import signal
import time

from harness import diagnostics


READ_TIMEOUT = 30
WRITE_TIMEOUT = 120
NETWORK_TIMEOUT = 300
GH_TIMEOUT = 60
TERM_GRACE = 5
KILL_GRACE = 5


class GitError(Exception):
    """A native step failed; only confirmed read timeouts may be retried."""

    def __init__(self, message, *, operation="", returncode=None, timed_out=False,
                 cleanup_known=True, effect="unknown", stdout="", stderr=""):
        super().__init__(message)
        self.timed_out, self.cleanup_known, self.effect = timed_out, cleanup_known, effect
        self.uncertain = not cleanup_known or effect == "unknown" or (timed_out and effect != "read")
        self.details = dict(operation=operation, returncode=returncode, timed_out=timed_out,
                            cleanup=cleanup_known, effect=effect, stdout=stdout, stderr=stderr,
                            signal=-returncode if returncode is not None and returncode < 0 else None)


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop(proc):
    """Bounded cleanup of this invocation's session, including inherited pipes."""
    for sig, grace in ((signal.SIGTERM, TERM_GRACE), (signal.SIGKILL, KILL_GRACE)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=grace)
            if not _group_exists(proc.pid):
                return out, err, True
        except subprocess.TimeoutExpired:
            pass
        except (UnicodeError, ValueError, OSError):
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
            if not _group_exists(proc.pid):
                return "", "", True
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()
    return "", "", False


def _native(cwd, program, args, *, timeout, deadline=None, env=None, effect="read"):
    operation = program + " " + (args[0] if args else "")
    sensitive = diagnostics.credential_values(os.environ if env is None else env)
    token_call = program == "gh" and tuple(args[:2]) == ("auth", "token")

    def error(reason, proc=None, out="", err="", timed_out=False, cleanup=True, unknown=False):
        out = "" if token_call else diagnostics.safe_text(out, secrets=sensitive)
        err = "" if token_call else diagnostics.safe_text(err, secrets=sensitive)
        return GitError(diagnostics.safe_text(f"{operation} {reason}: {err or out}", secrets=sensitive),
                        operation=operation, returncode=proc.returncode if proc else None,
                        timed_out=timed_out, cleanup_known=cleanup, effect="unknown" if unknown else effect,
                        stdout=out, stderr=err)

    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        raise error("deadline exhausted", timed_out=True)
    try:
        proc = subprocess.Popen([program, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    except OSError as exc:
        raise error("could not start", err=str(exc)) from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err, cleanup = _stop(proc)
        raise error("timed out", proc, out, err, True, cleanup) from None
    except BaseException as interruption:
        out, err, cleanup = _stop(proc)
        native_error = error(f"interrupted ({type(interruption).__name__})", proc, out, err,
                             cleanup=cleanup, unknown=True)
        if isinstance(interruption, Exception):
            raise native_error from interruption
        interruption.native_error = native_error
        raise
    if _group_exists(proc.pid):
        out, err, cleanup = _stop(proc)
        raise error("left running descendants", proc, out, err, cleanup=cleanup, unknown=True) from None
    result = subprocess.CompletedProcess([program, *args], proc.returncode, out, err)
    if proc.returncode:
        result.native_error = error("failed", proc, out, err)
    return result


def git(cwd, *args, check=True, env=None, timeout=None, deadline=None):
    command = args[0] if args else ""
    read = command in ("status", "diff", "rev-parse", "show", "log", "ls-files", "check-ref-format") or \
        command == "remote" and args[1:2] == ("get-url",) or \
        command == "symbolic-ref" and args[1:] == ("--short", "HEAD")
    if command in ("fetch", "push", "clone"):
        seconds = NETWORK_TIMEOUT
    elif read:
        seconds = READ_TIMEOUT
    else:
        seconds = WRITE_TIMEOUT
    proc = _native(cwd, "git", args, timeout=seconds if timeout is None else timeout,
                   deadline=deadline, env=env, effect="read" if read else "write")
    if check and proc.returncode:
        raise proc.native_error
    return proc.stdout.strip() if check else proc


def is_clean(path):
    return git(path, "status", "--porcelain") == ""


def changed_paths(worktree):
    """Repository-relative paths changed in the working tree, staged or not, including untracked files."""
    proc = git(worktree, "status", "--porcelain", "-z", "--untracked-files=all", check=False)
    if proc.returncode:
        raise proc.native_error
    raw = proc.stdout
    entries, paths = raw.split("\0"), []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if set(entry[:2]) & {"R", "C"}:  # a rename or copy, staged or not, names its source next
            paths.append(entries[index])
            index += 1
    return sorted(set(paths))


def run_shell(command, cwd, timeout):
    """Exit code and output tail of one verification command; -1 when it timed out."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def gh(cwd, *args, env=None, timeout=None, deadline=None):
    read = args[:2] in (("auth", "token"), ("repo", "view"), ("pr", "view"), ("pr", "list")) or \
        args[:1] == ("api",) and not any(arg in args for arg in ("--input", "-f", "-F", "--field", "--raw-field")) and \
        not any(args[i + 1].upper() != "GET" for i, arg in enumerate(args[:-1]) if arg in ("--method", "-X"))
    proc = _native(cwd, "gh", args, timeout=GH_TIMEOUT if timeout is None else timeout,
                   deadline=deadline, env=env, effect="read" if read else "write")
    if proc.returncode:
        raise proc.native_error
    return proc.stdout.strip()


def discard(worktree):
    """Drop uncommitted changes and untracked files in a harness-owned worktree."""
    git(worktree, "reset", "-q", "--hard", "HEAD")
    git(worktree, "clean", "-fdq")


def notify(title, text):
    """A macOS notification; nothing where osascript is missing. Text is quoted for AppleScript."""
    if not shutil.which("osascript"):
        return
    quoted = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    subprocess.run(["osascript", "-e", f'display notification {quoted} with title "{title}"'],
                   capture_output=True, check=False)


@contextmanager
def file_lock(path):
    """Exclusive lock held for the block; used to serialise jobs and verification runs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def lock_held(path):
    """Probe an existing lock without creating it or waiting."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)

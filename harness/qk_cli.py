"""The only place qk-harness runs qk.

qk is used through its versioned CLI contract only, never imported. The harness runs the qk named in `qk.lock`: a
checkout of the locked tag in the harness state directory, refused when the tag no longer points at the locked
commit, when the checkout moved, or when `qk contract --json` does not offer the contract version and commands
this harness needs.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from harness import diagnostics, gitops


CONTRACT = 1
# Contract commands this harness calls; a qk build without one of them is refused at start-up.
REQUIRED_COMMANDS = ("agent run", "agent family", "log run", "eval prepare", "eval grade", "show")
LOCK_FIELDS = ("repo", "tag", "sha", "contract")
SHA_RE = re.compile(r"[0-9a-f]{40}")
TERM_GRACE_SECONDS = 30  # how long qk gets to stop its agent after SIGTERM
EXTRA_SECONDS = 60  # margin over qk's own agent timeout before the harness stops qk


class QkError(Exception):
    """qk cannot be used as locked. The message says what is wrong and what to do."""


def load_lock(path):
    """The qk.lock file: which qk repository, tag, commit and contract version this harness runs."""
    path = Path(path)
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QkError(f"cannot read {path}: {error}") from None
    if not isinstance(lock, dict) or sorted(lock) != sorted(LOCK_FIELDS):
        raise QkError(f"{path} must hold exactly {', '.join(LOCK_FIELDS)}")
    if not all(isinstance(lock[name], str) and lock[name] for name in ("repo", "tag", "sha")):
        raise QkError(f"{path}: repo, tag and sha must be non-empty strings")
    if not SHA_RE.fullmatch(lock["sha"]):
        raise QkError(f"{path}: sha must be a full 40-character commit id")
    if lock["contract"] != CONTRACT or type(lock["contract"]) is not int:
        raise QkError(f"{path}: this harness speaks qk contract {CONTRACT}, the lock says {lock['contract']!r}")
    return lock


def state_root():
    """Harness state, outside any repository: QK_HARNESS_STATE_DIR, else $XDG_STATE_HOME/qk-harness."""
    base = os.environ.get("QK_HARNESS_STATE_DIR")
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state") / "qk-harness"


def _git(cwd, *args):
    try:
        return gitops.git(cwd, *args)
    except gitops.GitError as error:
        raise QkError(str(error)) from error


def fetch(lock, root=None):
    """Path of a checkout of the locked qk; fetched once by tag, and refused when the tag is not the locked sha."""
    target = Path(root or state_root()) / "qk" / lock["sha"]
    if target.exists():
        head = _git(target, "rev-parse", "HEAD")
        if head != lock["sha"] or _git(target, "status", "--porcelain"):
            raise QkError(f"{target} is at {head[:12]} or has local changes; qk.lock says {lock['sha'][:12]}. "
                          f"Remove {target} and run again")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f"{lock['sha']}.partial-", dir=str(target.parent)))
    cleanup = True
    try:
        _git(partial, "init", "-q")
        tag = f"refs/tags/{lock['tag']}"
        _git(partial, "fetch", "-q", "--depth", "1", "--no-tags", lock["repo"], f"+{tag}:{tag}")
        found = _git(partial, "rev-parse", f"{tag}^{{commit}}")
        if found != lock["sha"]:
            raise QkError(f"tag {lock['tag']} of {lock['repo']} is commit {found[:12]}, but qk.lock says "
                          f"{lock['sha'][:12]}. Do not run; check who moved the tag, then update qk.lock on purpose")
        _git(partial, "checkout", "-q", "--detach", found)
        partial.rename(target)
    except QkError as error:
        cleanup = not isinstance(error.__cause__, gitops.GitError) or not error.__cause__.uncertain
        if not cleanup:
            diagnostics.capture(partial / "diagnostics", dict(error.__cause__.details, capability="bootstrap",
                                                              reason=str(error)))
            raise QkError(f"{error}; partial checkout preserved at {partial}; inspect before retrying") from error
        raise
    finally:
        if cleanup:
            shutil.rmtree(partial, ignore_errors=True)
    return target


class Result:
    """One qk call: exit code, parsed JSON stdout (None when there was none) and stderr."""

    def __init__(self, code, data, stderr):
        self.code, self.data, self.stderr = code, data, stderr

    def __repr__(self):
        return f"Result(code={self.code!r}, data={self.data!r})"


class Qk:
    """A checked qk checkout. Construction fails unless it is at the locked commit and speaks the contract."""

    def __init__(self, root, lock):
        self.root = Path(root)
        self.script = self.root / "scripts" / "qk.py"
        head = _git(self.root, "rev-parse", "HEAD")
        if head != lock["sha"]:
            raise QkError(f"qk at {self.root} is commit {head[:12]}, qk.lock says {lock['sha'][:12]}")
        reply = self.call(["contract", "--json"], timeout=60)
        info = reply.data if isinstance(reply.data, dict) else {}
        if reply.code != 0 or info.get("contract") != CONTRACT:
            raise QkError(f"qk at {self.root} reports contract {info.get('contract')!r}, this harness needs "
                          f"{CONTRACT}: {reply.stderr.strip()[-300:]}")
        commands = info.get("commands")
        missing = [name for name in REQUIRED_COMMANDS if not isinstance(commands, list) or name not in commands]
        if missing:
            raise QkError(f"qk {info.get('qk')!r} lacks contract commands: {', '.join(missing)}")
        self.version = info.get("qk")

    def call(self, argv, stdin=None, timeout=None):
        """Run one qk command. On timeout qk gets SIGTERM, so it stops its agent, then SIGKILL after a grace."""
        proc = subprocess.Popen([sys.executable, str(self.script), *argv], cwd=str(self.root),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=gitops.KILL_GRACE)
                except subprocess.TimeoutExpired:
                    for stream in (proc.stdout, proc.stderr):
                        stream.close()
                    raise QkError(f"qk {' '.join(argv[:2])} cleanup is unconfirmed; inspect before resuming") from None
                raise QkError(f"qk {' '.join(argv[:2])} ignored SIGTERM and was killed") from None
            raise QkError(f"qk {' '.join(argv[:2])} did not finish within {timeout}s and was stopped; "
                          f"its report was: {out.strip()[-300:] or err.strip()[-300:]}") from None
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            data = None
        return Result(proc.returncode, data, err)

    def agent_run(self, spec, cwd, prompt, *, write, add_dirs=(), timeout=1800, read_only_mcp=(),
                  lessons=None, repo=None, events=None, result_contract=True, git_rules=True):
        argv = ["agent", "run", "--spec", spec, "--cwd", str(cwd), "--write" if write else "--read-only",
                "--timeout", str(timeout), "--json"]
        for directory in add_dirs:
            argv += ["--add-dir", str(directory)]
        for name in read_only_mcp:
            argv += ["--read-only-mcp", name]
        if lessons:
            argv += ["--lessons", lessons] + (["--repo", repo] if repo else [])
        if events:
            argv += ["--events", str(events)]
        if not result_contract:
            argv.append("--no-result-contract")
        if not git_rules:
            argv.append("--no-git-rules")
        return self.call(argv, stdin=prompt, timeout=timeout + EXTRA_SECONDS)

    def agent_family(self, spec):
        return self.call(["agent", "family", spec, "--json"], timeout=60)

    def log_run(self, state, producer, state_dir):
        return self.call(["log", "run", "--producer", producer, "--state-dir", str(state_dir), "--json"],
                         stdin=json.dumps(state), timeout=120)

    def eval_prepare(self, case, directory):
        return self.call(["eval", "prepare", case, str(directory), "--json"], timeout=300)

    def eval_grade(self, case, workdir, final=None, events=None):
        argv = ["eval", "grade", case, str(workdir), "--json"]
        if final:
            argv += ["--final", str(final)]
        if events:
            argv += ["--events", str(events)]
        return self.call(argv, timeout=900)

    def show(self, kind=None, record_id=None, repo=None):
        argv = ["show", "--json"]
        for flag, value in (("--kind", kind), ("--id", record_id), ("--repo", repo)):
            if value:
                argv += [flag, value]
        return self.call(argv, timeout=120)


def open_locked(lock_path, root=None):
    """The locked qk, fetched if needed and checked; QkError explains any refusal."""
    lock = load_lock(lock_path)
    return Qk(fetch(lock, root), lock)

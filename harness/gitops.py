"""Git and shell steps the harness itself performs. Agents never run these; only the harness writes git."""

import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess


class GitError(Exception):
    """A git step failed; the message carries git's own error."""


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed in {cwd}: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout.strip() if check else proc


def is_clean(path):
    return git(path, "status", "--porcelain") == ""


def changed_paths(worktree):
    """Repository-relative paths changed in the working tree, staged or not, including untracked files."""
    raw = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(worktree),
                         capture_output=True, text=True, check=True).stdout
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


def gh(cwd, *args):
    proc = subprocess.run(["gh", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"gh {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout.strip()


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

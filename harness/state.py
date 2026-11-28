"""Job state: one JSON file per job in the harness state directory, outside every repository.

State updates hold the job lock and save by atomic replace. Task turns and CI reads execute on snapshots outside
the lock, then merge only the state they own.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import time

from harness import gitops
from harness.qk_cli import state_root


VERSION = 1
SIZES = ("medium", "large")
ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


class JobError(Exception):
    """The job cannot be found or read."""


def jobs_root():
    return state_root() / "jobs"


def job_dir(job_id):
    if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
        raise JobError(f"not a job id: {job_id!r}")
    return jobs_root() / job_id


def slugify(text, limit=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "task"


def new_job(repo, task, size, base, impl, review, contract=True, parallel=2, task_review=True):
    """A new job in its first stage; the repository is not touched. `contract` False skips the contract stage."""
    if size not in SIZES:
        raise JobError(f"size must be one of {', '.join(SIZES)}")
    repo = Path(repo).resolve()
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    slug = f"{slugify(task)}-{job_id[-6:]}"
    job = {
        "version": VERSION, "id": job_id, "repo": str(repo), "base": base,
        "base_sha": gitops.git(repo, "rev-parse", f"refs/heads/{base}"),
        "task": task, "slug": slug, "size": size, "impl": impl, "review": review,
        "options": {"contract": bool(contract), "parallel": max(1, int(parallel)), "task_review": bool(task_review)},
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "plan": None, "approvals": {}, "contract": None, "tasks": {},
        "integration": {"branch": f"qk/{slug}", "worktree": str(job_dir(job_id) / "integration"),
                        "merged": False, "verify": None, "review": None},
        "pr_url": None, "log": [],
    }
    job_dir(job_id).mkdir(parents=True)
    save(job)
    return job


def load(job_id):
    path = job_dir(job_id) / "state.json"
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JobError(f"no job {job_id} under {jobs_root()}") from None
    except ValueError as error:
        raise JobError(f"{path} is not valid JSON: {error}") from None
    if job.get("version") != VERSION:
        raise JobError(f"{path} has state version {job.get('version')!r}; this harness reads {VERSION}")
    return job


def save(job):
    directory = job_dir(job["id"])
    temporary = directory / "state.json.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "state.json")


@contextmanager
def locked(job_id):
    """The job, held exclusively; it is saved when the block ends, whether or not it raised."""
    with gitops.file_lock(job_dir(job_id) / "job.lock"):
        job = load(job_id)
        try:
            yield job
        finally:
            save(job)


def owner_alive(pid):
    try:
        if isinstance(pid, bool) or int(pid) <= 0:
            return False
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False


def recovery_reason(job):
    """A dead PID is not completion evidence. Active parallel tasks may still proceed independently."""
    if job.get("recovery_required"):
        return job["recovery_required"]["reason"]
    if job.get("active_step"):
        return "a step was interrupted or is still running; inspect its evidence before resuming"
    active = set()
    for name, task in job["tasks"].items():
        marker = task.get("running")
        if marker:
            if not owner_alive(marker.get("pid")):
                return f"task {name} stopped without a persisted step result; inspect before resuming"
            active.add(marker.get("events"))
    for entry in job.get("usage", []):
        status = entry.get("status")
        if status == "unknown" or status == "failed" and not entry.get("terminal"):
            return "a model turn has unknown execution/usage; inspect its evidence before resuming"
        if status == "running" and entry.get("events") not in active:
            return "an interrupted model turn may still be running; inspect its evidence before resuming"
    return ""

"""The thin loop: run whatever `next` names until a person must act, a step is blocked, or the job is done.

It holds no rules of its own. Every step goes through the same capability a person could call by hand, which
checks its own preconditions; the loop only reads `policy.next_step` and stops when that is not a capability.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness import capabilities, gitops, ledger, policy, state


MAX_STEPS = 60  # a job with many tasks and one fix round each stays far below this
DEFAULT_PARALLEL = 2  # task turns at once; verification still runs one at a time


def parse_step(step):
    """(capability, target, expect_red, fix) for a capability step, or None for a step a person takes."""
    words = step.split()
    if not words or words[0] not in policy.CAPABILITIES:
        return None
    target = next((word for word in words[2:] if not word.startswith("--")), None)
    return words[0], target, "--expect-red" in words, "--fix" in words


def run_step(job_id, qk, step):
    capability, target, expect_red, fix = parse_step(step)
    return capabilities.run(job_id, capability, qk, target, expect_red=expect_red, fix=fix)


def run(job_id, qk, max_steps=MAX_STEPS):
    """Advance the job; the last capability reply, with `stopped` saying why the loop ended. A job that has
    stopped (done, blocked, aborted, or waiting for a person's decision) is recorded in qk's ledger."""
    cleared = ledger.recover(job_id)
    reply = finish(job_id, qk, loop(job_id, qk, max_steps))
    return dict(reply, recovered=cleared) if cleared else reply


def finish(job_id, qk, reply):
    logged = ledger.record(job_id, qk, reply.get("next") or policy.next_step(state.load(job_id)),
                           stopped=reply.get("stopped"))
    return dict(reply, ledger=logged) if logged else reply


def loop(job_id, qk, max_steps):
    reply = {}
    for _ in range(max_steps):
        job = state.load(job_id)
        width = job.get("options", {}).get("parallel", DEFAULT_PARALLEL)
        batch = policy.task_steps(job)[:width]
        if len(batch) > 1:  # several tasks can move: their turns run at once, each in its own qk process
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                replies = list(pool.map(lambda step: run_step(job_id, qk, step), batch))
            failed = [item for item in replies if item["status"] != "done"]
            if failed:
                return dict(failed[0], stopped=failed[0]["status"], parallel=replies)
            reply = replies[-1]
            continue
        step = policy.next_step(job)
        parsed = parse_step(step)
        if parsed is None:
            stopped = "done" if step.startswith("done") else "aborted" if step.startswith("aborted") else "waiting"
            return dict(reply, stopped=stopped, next=step)
        capability, target, expect_red, fix = parsed
        reply = capabilities.run(job_id, capability, qk, target, expect_red=expect_red, fix=fix)
        if reply["status"] != "done":
            return dict(reply, stopped=reply["status"])
    return dict(reply, stopped="limit", next=policy.next_step(state.load(job_id)),
                reason=f"stopped after {max_steps} steps without reaching a gate; a person looks")


def abort(job_id, reason):
    """Mark the job aborted. Branches and worktrees stay for a person to inspect or remove."""
    with state.locked(job_id) as job:
        if job.get("aborted"):
            return {"status": "refused", "reason": "the job is already aborted", "next": policy.next_step(job)}
        job["aborted"] = {"at": capabilities.now(), "reason": reason}
        capabilities.record(job, "abort", "done", reason)
        places = [(job["integration"]["worktree"], job["integration"]["branch"])] + \
            [(task["worktree"], task["branch"]) for task in job["tasks"].values()]
        # Only what exists: a task that was never built has no worktree or branch to clean up.
        branches = [branch for _, branch in places
                    if gitops.git(job["repo"], "rev-parse", "--verify", "-q", f"refs/heads/{branch}",
                                  check=False).returncode == 0]
        return {"status": "done", "reason": "", "next": policy.next_step(job),
                "left": {"worktrees": [path for path, _ in places if Path(path).exists()], "branches": branches}}

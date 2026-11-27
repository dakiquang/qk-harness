"""The thin loop: run whatever `next` names until a person must act, a step is blocked, or the job is done.

It holds no rules of its own. Every step goes through the same capability a person could call by hand, which
checks its own preconditions; the loop only reads `policy.next_step` and stops when that is not a capability.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time

from harness import capabilities, delegation, gitops, ledger, policy, state


MAX_STEPS = 60  # a job with many tasks and one fix round each stays far below this
DEFAULT_PARALLEL = 2  # task turns at once; verification still runs one at a time
LIMIT_GRACE = timedelta(minutes=2)  # wait a little past the reset the runtime named
LIMIT_RETRY_GAP = timedelta(minutes=30)  # when it named no reset time
LIMIT_WAITS = 3  # waits for usage limits in one run, so a limit that never lifts cannot keep the loop alive


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


def run(job_id, qk, max_steps=MAX_STEPS, *, wait_limits=False, deadline=None):
    """Advance the job; the last capability reply, with `stopped` saying why the loop ended. A job that has
    stopped (done, blocked, aborted, limited, or waiting for a person's decision) is recorded in qk's ledger.

    Running a job retries the runtimes a usage limit stopped. With `wait_limits`, a job stopped by a usage limit
    sleeps until the reset (plus a grace period) and carries on, at most LIMIT_WAITS times and never past
    `deadline` (a local datetime)."""
    with state.locked(job_id) as job:
        if job.get("delegation"):
            try:
                delegation.check(job)
                if delegation.interrupted(job):
                    raise delegation.GrantError("a model turn has unresolved cost or may still be running; inspect it")
            except delegation.GrantError as error:
                capabilities.record(job, "resume", "blocked", str(error))
                reply = {"status": "blocked", "stopped": "blocked", "reason": str(error)}
            else:
                reply = None
                if job["pr_url"]:
                    job["ci"] = None  # an explicit resume rechecks the remote head and CI, not cached success
        else:
            reply = None
    if reply:
        return finish(job_id, qk, reply)
    cleared = ledger.recover(job_id)
    release(job_id)
    reply = loop(job_id, qk, max_steps)
    for _ in range(LIMIT_WAITS if wait_limits else 0):
        if reply.get("stopped") != "limited":
            break
        limits = state.load(job_id).get("limits") or {}
        wake = wake_time(limits)
        names = ", ".join(sorted(limits))
        if deadline is not None and wake >= deadline:
            notify(f"Gave up: the {names} usage limit resets after the {deadline:%H:%M} deadline; "
                   f"run qk-harness run {job_id} after the reset.")
            break
        notify(f"{names} usage limit; job {job_id} waits until {wake:%H:%M}.")
        sleep_until(wake)
        notify(f"Retrying job {job_id} after the {names} usage limit.")
        release(job_id)
        reply = loop(job_id, qk, max_steps)
    else:
        if wait_limits and reply.get("stopped") == "limited":
            notify(f"Gave up on job {job_id}: still stopped by a usage limit after {LIMIT_WAITS} waits.")
    reply = finish(job_id, qk, reply)
    return dict(reply, recovered=cleared) if cleared else reply


def release(job_id):
    """Forget the usage limits a job recorded, so its next steps try those runtimes again."""
    with state.locked(job_id) as job:
        if job.pop("limits", None):
            capabilities.record(job, "resume", "done", "retrying runtimes stopped by a usage limit")


def wake_time(limits):
    """When to retry: the earliest reset plus a grace period; LIMIT_RETRY_GAP from now for a limit with none."""
    now = datetime.now().astimezone()
    times = [policy.reset_time(limit) for limit in limits.values()]
    return min((moment + LIMIT_GRACE if moment else now + LIMIT_RETRY_GAP) for moment in times) if times \
        else now + LIMIT_RETRY_GAP


def sleep_until(moment):
    while True:
        left = (moment - datetime.now().astimezone()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60))


def notify(text):
    """Say it on stderr and, on macOS, as a notification, so a person away from the terminal sees it."""
    print(f"qk-harness: {text}", file=sys.stderr)
    gitops.notify("qk-harness", text)


def deadline_at(clock, now=None):
    """The next local time HH:MM, today or tomorrow, with the UTC offset in force on that day."""
    now = now or datetime.now().astimezone()
    hour, minute = (int(part) for part in clock.split(":"))
    for day in (now.date(), now.date() + timedelta(days=1)):
        for fold in (0, 1):  # a clock time that occurs twice when clocks go back: the first one still ahead
            moment = datetime(day.year, day.month, day.day, hour, minute, fold=fold).astimezone()
            if moment > now:
                return moment
    return moment


def finish(job_id, qk, reply):
    logged = ledger.record(job_id, qk, reply.get("next") or policy.next_step(state.load(job_id)),
                           stopped=reply.get("stopped"))
    result = dict(reply, ledger=logged) if logged else reply
    job = state.load(job_id)
    if job.get("delegation"):
        targets = [("contract", job.get("contract")), *job["tasks"].items(), ("integration", job["integration"])]
        findings = [{"target": name, **finding} for name, target in targets if target
                    for finding in (target.get("review") or {}).get("findings", [])]
        result = dict(result, authority=job["delegation"]["sha256"], approvals=job["approvals"],
                      budget=delegation.costs(job), ci=job.get("ci"), findings=findings)
    return result


def loop(job_id, qk, max_steps):
    reply = {}
    for _ in range(max_steps):
        job = state.load(job_id)
        width = job.get("options", {}).get("parallel", DEFAULT_PARALLEL)
        batch = policy.task_steps(job)[:width]
        if len(batch) > 1:  # several tasks can move: their turns run at once, each in its own qk process
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                replies = list(pool.map(lambda step: run_step(job_id, qk, step), batch))
            failed = [item for item in replies if item["status"] not in ("done", "limited")]
            if failed:
                return dict(failed[0], stopped=failed[0]["status"], parallel=replies)
            reply = replies[-1]  # a task stopped by a usage limit waits while the others go on
            continue
        step = batch[0] if batch else policy.next_step(job)  # one task can move while another waits
        parsed = parse_step(step)
        if parsed is None:
            if step.startswith("approve ") and step.endswith(" --delegated"):
                reply = capabilities.approve(job_id, step.split()[2], delegated=True)
                if reply["status"] != "done":
                    return dict(reply, stopped=reply["status"])
                continue
            stopped = "done" if step.startswith("done") else "aborted" if step.startswith("aborted") else \
                "limited" if policy.limit_wait(step) else "waiting"
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

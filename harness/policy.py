"""Which capability may run now, read from the job state alone.

Every capability checks its own preconditions here before it does anything, whether a coordinator or a person
calls it. A refusal carries the reason and the next command that would move the job forward.
"""

from datetime import datetime
import hashlib
from pathlib import Path

from harness import delegation


CAPABILITIES = {  # name -> what it may change
    "plan.write": "nothing (read-only turn)",
    "contract.write": "the integration worktree",
    "contract.review": "nothing (read-only turn)",
    "build.run": "the task's own worktree",
    "verify.run": "nothing; runs the plan's checks one job at a time",
    "review.run": "nothing (read-only turn)",
    "integrate.run": "the local branch qk/<slug>",
    "pr.open": "the remote: pushes qk/<slug> and opens one pull request",
    "pr.check": "nothing; waits for CI on the delegated PR's exact head",
}
GATES = {"medium": ("plan",), "large": ("plan", "contract", "build", "integrate")}
SERIOUS = ("blocker", "important")
MAX_FIX_ROUNDS = 1


def plan_text(job):
    return Path(job["plan"]["file"]).read_text(encoding="utf-8")


def plan_unchanged(job):
    return hashlib.sha256(plan_text(job).encode("utf-8")).hexdigest() == job["plan"]["sha256"]


def approved(job, stage):
    record = job["approvals"].get(stage)
    return bool(record) and job["plan"] is not None and record.get("plan_sha256") == job["plan"]["sha256"]


def gates(job):
    """The stages a person approves for this job: by size, without the contract gate when there is no contract."""
    return tuple(stage for stage in GATES[job["size"]] if stage != "contract" or contract_on(job))


def gate_open(job, stage):
    """A stage this job does not gate counts as approved."""
    return stage not in gates(job) or approved(job, stage)


def green(verify):
    return verify is not None and all(check["exit"] == 0 for check in verify["results"])


def serious(review):
    return [finding for finding in (review or {}).get("findings", [])
            if str(finding.get("severity", "")).lower() in SERIOUS]


def review_ok(review):
    return review is not None and review.get("status") == "done" and not serious(review)


def contract_on(job):
    """Whether this job writes and checks a contract before building (on unless started without one)."""
    return job.get("options", {}).get("contract", True)


def contract_ok(job):
    if not contract_on(job):
        return True
    contract = job["contract"]
    return contract_passed(contract) and contract.get("red") is True


def contract_passed(contract):
    """The contract's review passed, or a person accepted its findings after the revision round."""
    return contract is not None and (review_ok(contract.get("review")) or bool(contract.get("accepted")))


def task_review_on(job):
    """Whether each task is reviewed on its own before integration (on unless started without it)."""
    return job.get("options", {}).get("task_review", True)


def task_ok(task, job=None):
    reviewed = review_ok(task["review"]) or (job is not None and not task_review_on(job))
    return task["commit"] is not None and green(task["verify"]) and reviewed


def located(findings):
    """Every finding names a file and a line, so a fix round knows exactly where to look."""
    return all(finding.get("file") and isinstance(finding.get("line"), int) for finding in findings)


def fixable(task):
    """One fix round, plus any a person granted, when the checks failed or when every serious review finding names
    a file and a line."""
    if task.get("fix_rounds", 0) >= MAX_FIX_ROUNDS + task.get("granted_rounds", 0) or task.get("commit") is None:
        return False
    if task["verify"] is not None and not green(task["verify"]):
        return True
    findings = serious(task["review"])
    return bool(findings) and located(findings)


# --------------------------------------------------------------- usage limits


def runtime(spec):
    return str(spec or "").split(":", 1)[0]


def step_spec(job, capability, target=None):
    """The runtime:model an agent turn of this step uses, or None for a step without one."""
    task = job["tasks"].get(target) if target not in (None, "integration") else None
    if capability in ("plan.write", "contract.write") or (capability == "build.run" and task is None):
        return job["impl"]
    if capability == "contract.review" or (capability == "review.run" and task is None):
        return job["review"]
    if capability == "build.run":
        return task.get("impl_spec", job["impl"])
    if capability == "review.run":
        return task.get("review_spec", job["review"])
    return None


def held(job, step):
    """(runtime, limit) when the capability step `step` needs a runtime stopped at its usage limit, else None."""
    words = step.split()
    if not words or words[0] not in CAPABILITIES:
        return None
    target = next((word for word in words[2:] if not word.startswith("--")), None)
    name = runtime(step_spec(job, words[0], target))
    limit = (job.get("limits") or {}).get(name)
    return (name, limit) if name and limit else None


def reset_time(limit):
    """The local time the limit resets, or None when the runtime named none."""
    try:
        moment = datetime.fromisoformat(str(limit.get("reset_at")))
    except ValueError:
        return None
    return moment.astimezone()  # a time without an offset is read as local time


def until(limit):
    moment = reset_time(limit)
    if moment is None:
        return "(reset time unknown)"
    today = datetime.now().astimezone().date()
    return f"until {moment:%H:%M}" if moment.date() == today else f"until {moment:%Y-%m-%d %H:%M}"


def limit_wait(step):
    """Whether `step` is a wait for a usage limit rather than for a person or a running task."""
    return step.startswith("wait: ") and " usage limit " in step


def approval_step(job, stage):
    suffix = " --delegated" if job.get("delegation") else ""
    return f"approve {job['id']} {stage}{suffix}"


def next_step(job):
    """The command that moves the job forward, or why a person has to act. A step whose runtime is stopped at its
    usage limit becomes a wait for the reset."""
    step = _next_step(job)
    words = step.split()
    spec = step_spec(job, words[0], words[2] if len(words) > 2 else None)
    if spec and job.get("delegation"):
        try:
            delegation.check_turn(job, spec)
        except delegation.GrantError as error:
            return f"a person decides: {error}"
    hold = held(job, step)
    if hold:
        return f"wait: the {hold[0]} usage limit {until(hold[1])}, then {step}"
    return step


def _next_step(job):
    job_id = job["id"]
    if job.get("aborted"):
        return f"aborted: {job['aborted']['reason'] or 'no reason given'}"
    try:
        delegation.check(job)
    except delegation.GrantError as error:
        return f"a person decides: {error}"
    if job["plan"] is None:
        return f"plan.write {job_id}"
    if not plan_unchanged(job):
        return f"a person decides: {job['plan']['file']} changed after it was written; restore it or abort"
    if not approved(job, "plan"):
        return approval_step(job, "plan")
    contract = job["contract"]
    if not contract_on(job):
        pass
    elif contract is None:
        return f"contract.write {job_id}"
    elif contract.get("review") is None:
        return f"contract.review {job_id}"
    elif not contract_passed(contract):
        if contract.get("revisions", 0) < MAX_FIX_ROUNDS:
            return f"contract.write {job_id} --fix"
        return (f"a person decides: the contract review did not pass after its revision; edit the contract in "
                f"{job['integration']['worktree']} and commit it, then contract.review {job_id}, or accept its "
                f"findings with accept-contract {job_id} --reason TEXT")
    elif contract.get("red") is None:
        return f"verify.run {job_id} integration --expect-red"
    elif contract["red"] is False:
        return "a person decides: the contract tests already pass before any build, so they prove nothing"
    elif not gate_open(job, "contract"):
        return approval_step(job, "contract")
    steps = [step for step in (task_step(job, name, task) for name, task in job["tasks"].items()) if step] \
        if not job["integration"]["merged"] else []  # once integrated, only the integrated branch moves on
    if steps:
        # A usage-limit wait never hides a decision another task needs from a person.
        return next((step for step in steps if step.startswith("a person decides")), steps[0]) \
            if limit_wait(steps[0]) else steps[0]
    if not gate_open(job, "build"):
        return approval_step(job, "build")
    integration = job["integration"]
    if not integration["merged"]:
        return f"integrate.run {job_id}"
    if integration["verify"] is None:
        return f"verify.run {job_id} integration"
    if not green(integration["verify"]):
        return f"build.run {job_id} integration --fix" if fixable(integration) else \
            "a person decides: the integrated branch fails its checks after the fix round"
    if integration["review"] is None:
        return f"review.run {job_id} integration"
    if not review_ok(integration["review"]):
        return f"build.run {job_id} integration --fix" if fixable(integration) else \
            ("a person decides: the integrated branch has serious review findings that a fix round cannot take "
             "(no file and line, or the round is used)")
    if not gate_open(job, "integrate"):
        return approval_step(job, "integrate")
    if job.get("eval"):
        return "done: an eval job is graded instead of opening a pull request"
    if job["pr_url"] is None:
        return f"pr.open {job_id}"
    if job.get("delegation"):
        ci = job.get("ci") or {}
        if ci.get("status") in ("failed", "blocked") or ci.get("reason"):
            return f"a person decides: {ci.get('reason') or 'CI failed for the published head'}"
        if ci.get("head") != job["integration"]["commit"] or ci.get("status") != "success":
            return f"pr.check {job_id}"
    return f"done: a person reviews and merges {job['pr_url']}"


def task_step(job, name, task):
    """What one task needs next, or None when it is built, checked and (unless switched off) reviewed. A task
    whose next turn needs a runtime stopped at its usage limit waits; the other tasks go on."""
    step = _task_step(job, name, task)
    hold = held(job, step) if step else None
    if hold:
        return f"wait: task {name} needs the {hold[0]} usage limit to reset {until(hold[1])}, then {step}"
    return step


def _task_step(job, name, task):
    job_id = job["id"]
    if task.get("running"):
        return f"wait: task {name} is running {task['running']['capability']}"
    if task["commit"] is None:
        return f"build.run {job_id} {name}"
    if task["verify"] is None:
        return f"verify.run {job_id} {name}"
    if not green(task["verify"]):
        return f"build.run {job_id} {name} --fix" if fixable(task) else \
            f"a person decides: task {name} still fails its checks after the fix round"
    if not task_review_on(job):
        return None
    if task["review"] is None:
        return f"review.run {job_id} {name}"
    if not review_ok(task["review"]):
        return f"build.run {job_id} {name} --fix" if fixable(task) else \
            f"a person decides: task {name} has serious review findings that a fix round cannot take"
    return None


def in_task_stage(job, step):
    """Whether the job's next step (`step`, from next_step) belongs to its tasks rather than an earlier stage."""
    words = step.split()
    return (words[0] in ("build.run", "verify.run", "review.run") and len(words) > 2 and words[2] in job["tasks"]
            or step.startswith(("wait: task ", "a person decides: task ")))


def task_steps(job):
    """The capability steps tasks can take now, one per task, when the job is in its task stage."""
    if not in_task_stage(job, next_step(job)):
        return []
    steps = [task_step(job, name, task) for name, task in job["tasks"].items()]
    return [step for step in steps if step and step.split()[0] in CAPABILITIES]


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


class Refused(Exception):
    """A capability may not run now; the message says why."""


def check_grant(job, target):
    """Raise Refused unless a person may give `target` (a task name or "integration") one more fix round: its own
    round is used and a fix round could take what is still wrong."""
    _require(not job.get("aborted"), "the job is aborted")
    _require(not job.get("pr_url"), "the pull request is already open")
    _require(job["plan"] is not None and plan_unchanged(job), "the plan changed after it was written; restore it first")
    _require(target == "integration" or target in job["tasks"], f"no task {target!r} in the plan")
    _require(target == "integration" or not job["integration"]["merged"],
             "the tasks are already integrated; only the integrated branch can take a fix round now")
    item = job["integration"] if target == "integration" else job["tasks"][target]
    label = "the integrated branch" if target == "integration" else f"task {target}"
    _require(not item.get("running"), f"{label} is running")
    _require(not fixable(item), f"{label} can still take a fix round")
    _require(fixable(dict(item, granted_rounds=item.get("granted_rounds", 0) + 1)),
             f"{label} has nothing a fix round can take")


def check_accept(job, reason):
    """Raise Refused unless a person may accept the findings of the contract's failed review: its revision round is
    used and it has not been checked red yet."""
    _require(not job.get("aborted"), "the job is aborted")
    _require(job["plan"] is not None, "there is no plan yet")
    _require(plan_unchanged(job), "the plan changed after it was written; restore it first")
    _require(contract_on(job), "this job was started without a contract")
    contract = job["contract"]
    _require(contract is not None, "there is no contract yet")
    _require(contract.get("review") is not None, "the contract has no review yet")
    _require(not review_ok(contract["review"]), "the contract passed its review; there is nothing to accept")
    _require(contract.get("red") is None, "the contract was already checked red; it can no longer change")
    _require(contract.get("revisions", 0) >= MAX_FIX_ROUNDS,
             f"the contract still has its revision round; run contract.write {job['id']} --fix")
    _require(isinstance(reason, str) and reason.strip(), "--reason must not be blank")


def check(job, capability, target=None, *, expect_red=False, fix=False):
    """Raise Refused unless `capability` may run now on `target` (a task name or "integration")."""
    _require(capability in CAPABILITIES, f"unknown capability {capability!r}")
    _require(not job.get("aborted"), "the job is aborted")
    try:
        delegation.check(job)
        spec = step_spec(job, capability, target)
        if spec:
            delegation.check_turn(job, spec)
    except delegation.GrantError as error:
        raise Refused(str(error)) from error
    _require(not (job.get("delegation") and job["pr_url"] and capability != "pr.check"),
             "the delegated PR is published; no post-PR work is authorized")
    _require(not expect_red or (capability == "verify.run" and target == "integration"),
             "--expect-red only checks the contract on the integration branch before any build")
    if capability == "plan.write":
        _require(not approved(job, "plan"), "the plan is already approved; abort the job to plan again")
        return
    _require(job["plan"] is not None, "there is no plan yet")
    _require(plan_unchanged(job), f"{job['plan']['file']} changed after it was written")
    _require(approved(job, "plan"), "the plan is not approved")
    contract = job["contract"]
    if capability in ("contract.write", "contract.review") or expect_red:
        _require(contract_on(job), "this job was started without a contract")
    if capability == "contract.write":
        if not fix:
            _require(contract is None, "the contract is already written")
            return
        _require(contract is not None and contract.get("review") is not None
                 and not review_ok(contract["review"]), "the contract has no failed review to fix")
        _require(contract.get("revisions", 0) < MAX_FIX_ROUNDS, "the contract has no revision round left")
        _require(contract.get("red") is None, "the contract was already checked red; it can no longer change")
        return
    if capability == "contract.review":
        _require(contract is not None, "there is no contract yet")
        _require(contract.get("red") is None, "the contract was already checked red; it can no longer change")
        return
    if capability == "verify.run" and target == "integration" and expect_red:
        _require(contract is not None, "there is no contract yet")
        _require(contract_passed(contract), "the contract has not passed its review")
        _require(contract.get("red") is None, "the contract was already checked red")
        return
    if contract_on(job):
        _require(contract is not None, "there is no contract yet")
        _require(contract_passed(contract), "the contract has not passed its review")
        _require(contract.get("red") is True, "the contract tests were not shown to fail before the build")
        _require(gate_open(job, "contract"), "the contract stage is not approved")
    if target != "integration" and capability in ("build.run", "verify.run", "review.run"):
        _require(target in job["tasks"], f"no task {target!r} in the plan")
        task = job["tasks"][target]
        running = task.get("running")
        _require(not running, f"task {target} is already running {running and running['capability']} "
                              f"since {running and running['since']}")
        not_merged = (not job["integration"]["merged"],
                      "the tasks are already integrated; work on the integrated branch")
        if capability == "build.run":
            _require(*not_merged)
            _require(task["commit"] is None or fix, f"task {target} is already built")
            if fix:
                _require(fixable(task), f"task {target} has no fix round left or nothing to fix")
            return
        _require(task["commit"] is not None, f"task {target} is not built")
        if capability == "verify.run":
            _require(*not_merged)
            return
        _require(task_review_on(job), "this job was started without task reviews")
        _require(*not_merged)
        _require(green(task["verify"]), f"task {target} has not passed its checks")
        return
    _require(capability != "build.run" or target == "integration", "build.run needs a task name")
    _require(all(task_ok(task, job) for task in job["tasks"].values()),
             "every task must be built, pass its checks and pass its review first")
    _require(gate_open(job, "build"), "the build stage is not approved")
    _require(not any(task.get("running") for task in job["tasks"].values()), "a task is still running")
    integration = job["integration"]
    if capability == "integrate.run":
        _require(not integration["merged"], "the tasks are already integrated")
        return
    _require(integration["merged"], "the tasks are not integrated")
    if capability == "build.run":
        _require(fix, "build.run on the integration branch is only its --fix round")
        _require(fixable(integration), "the integrated branch has no fix round left or nothing a fix round can take")
        return
    if capability == "verify.run":
        return
    _require(green(integration["verify"]), "the integrated branch has not passed its checks")
    if capability == "review.run":
        return
    # pr.open
    _require(not job.get("eval"), "an eval job never opens a pull request")
    _require(review_ok(integration["review"]), "the integrated branch has not passed its review")
    _require(gate_open(job, "integrate"), "the integration stage is not approved")
    if capability == "pr.check":
        _require(bool(job.get("delegation") and job["pr_url"]), "pr.check needs a delegated published job")
        return
    _require(job["pr_url"] is None, f"the pull request already exists: {job['pr_url']}")


def check_approval(job, stage):
    """Raise Refused unless a person may approve `stage` now."""
    _require(not job.get("aborted"), "the job is aborted")
    _require(stage in gates(job), f"this {job['size']} job has no {stage} gate; gates: {', '.join(gates(job))}")
    _require(job["plan"] is not None, "there is no plan to approve yet")
    _require(plan_unchanged(job), f"{job['plan']['file']} changed after it was written")
    _require(not approved(job, stage), f"the {stage} stage is already approved")
    if stage == "contract":
        _require(contract_ok(job), "the contract must pass its review and be shown red first")
    elif stage == "build":
        _require(contract_ok(job) and gate_open(job, "contract") and job["tasks"]
                 and all(task_ok(task, job) for task in job["tasks"].values()),
                 "every task must be built, pass its checks and pass its review first")
    elif stage == "integrate":
        integration = job["integration"]
        _require(integration["merged"] and green(integration["verify"]) and review_ok(integration["review"]),
                 "the integrated branch must pass its checks and its review first")

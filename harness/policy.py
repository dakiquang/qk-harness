"""Which capability may run now, read from the job state alone.

Every capability checks its own preconditions here before it does anything, whether a coordinator or a person
calls it. A refusal carries the reason and the next command that would move the job forward.
"""

import hashlib
from pathlib import Path


CAPABILITIES = {  # name -> what it may change
    "plan.write": "nothing (read-only turn)",
    "contract.write": "the integration worktree",
    "contract.review": "nothing (read-only turn)",
    "build.run": "the task's own worktree",
    "verify.run": "nothing; runs the plan's checks one job at a time",
    "review.run": "nothing (read-only turn)",
    "integrate.run": "the local branch qk/<slug>",
    "pr.open": "the remote: pushes qk/<slug> and opens one pull request",
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


def gate_open(job, stage):
    """A stage this job's size does not gate counts as approved."""
    return stage not in GATES[job["size"]] or approved(job, stage)


def green(verify):
    return verify is not None and all(check["exit"] == 0 for check in verify["results"])


def serious(review):
    return [finding for finding in (review or {}).get("findings", [])
            if str(finding.get("severity", "")).lower() in SERIOUS]


def review_ok(review):
    return review is not None and review.get("status") == "done" and not serious(review)


def contract_ok(job):
    contract = job["contract"]
    return contract is not None and review_ok(contract.get("review")) and contract.get("red") is True


def task_ok(task):
    return task["commit"] is not None and green(task["verify"]) and review_ok(task["review"])


def fixable(task):
    """A task may get its one fix round when its checks failed or its review found serious problems."""
    if task["fix_rounds"] >= MAX_FIX_ROUNDS or task["commit"] is None:
        return False
    return (task["verify"] is not None and not green(task["verify"])) or bool(serious(task["review"]))


def next_step(job):
    """The command that moves the job forward, or why a person has to act."""
    job_id = job["id"]
    if job["plan"] is None:
        return f"plan.write {job_id}"
    if not plan_unchanged(job):
        return f"a person decides: {job['plan']['file']} changed after it was written; restore it or abort"
    if not approved(job, "plan"):
        return f"approve {job_id} plan"
    contract = job["contract"]
    if contract is None:
        return f"contract.write {job_id}"
    if contract.get("review") is None:
        return f"contract.review {job_id}"
    if not review_ok(contract["review"]):
        return (f"a person decides: the contract review did not pass; edit the contract in "
                f"{job['integration']['worktree']} and commit it, then contract.review {job_id}")
    if contract.get("red") is None:
        return f"verify.run {job_id} integration --expect-red"
    if contract["red"] is False:
        return "a person decides: the contract tests already pass before any build, so they prove nothing"
    if not gate_open(job, "contract"):
        return f"approve {job_id} contract"
    for name, task in job["tasks"].items():
        if task["commit"] is None:
            return f"build.run {job_id} {name}"
        if task["verify"] is None:
            return f"verify.run {job_id} {name}"
        if not green(task["verify"]):
            return f"build.run {job_id} {name} --fix" if fixable(task) else \
                f"a person decides: task {name} still fails its checks after the fix round"
        if task["review"] is None:
            return f"review.run {job_id} {name}"
        if not review_ok(task["review"]):
            return f"build.run {job_id} {name} --fix" if fixable(task) else \
                f"a person decides: task {name} still has serious review findings after the fix round"
    if not gate_open(job, "build"):
        return f"approve {job_id} build"
    integration = job["integration"]
    if not integration["merged"]:
        return f"integrate.run {job_id}"
    if integration["verify"] is None:
        return f"verify.run {job_id} integration"
    if not green(integration["verify"]):
        return "a person decides: the integrated branch fails its checks"
    if integration["review"] is None:
        return f"review.run {job_id} integration"
    if not review_ok(integration["review"]):
        return "a person decides: the integrated branch has serious review findings"
    if not gate_open(job, "integrate"):
        return f"approve {job_id} integrate"
    if job["pr_url"] is None:
        return f"pr.open {job_id}"
    return f"done: a person reviews and merges {job['pr_url']}"


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


class Refused(Exception):
    """A capability may not run now; the message says why."""


def check(job, capability, target=None, *, expect_red=False, fix=False):
    """Raise Refused unless `capability` may run now on `target` (a task name or "integration")."""
    _require(capability in CAPABILITIES, f"unknown capability {capability!r}")
    _require(not expect_red or (capability == "verify.run" and target == "integration"),
             "--expect-red only checks the contract on the integration branch before any build")
    if capability == "plan.write":
        _require(not approved(job, "plan"), "the plan is already approved; abort the job to plan again")
        return
    _require(job["plan"] is not None, "there is no plan yet")
    _require(plan_unchanged(job), f"{job['plan']['file']} changed after it was written")
    _require(approved(job, "plan"), "the plan is not approved")
    contract = job["contract"]
    if capability == "contract.write":
        _require(contract is None, "the contract is already written")
        return
    _require(contract is not None, "there is no contract yet")
    if capability == "contract.review":
        _require(contract.get("red") is None, "the contract was already checked red; it can no longer change")
        return
    if capability == "verify.run" and target == "integration" and expect_red:
        _require(review_ok(contract.get("review")), "the contract has not passed its review")
        _require(contract.get("red") is None, "the contract was already checked red")
        return
    _require(review_ok(contract.get("review")), "the contract has not passed its review")
    _require(contract.get("red") is True, "the contract tests were not shown to fail before the build")
    _require(gate_open(job, "contract"), "the contract stage is not approved")
    if target != "integration" and capability in ("build.run", "verify.run", "review.run"):
        _require(target in job["tasks"], f"no task {target!r} in the plan")
        task = job["tasks"][target]
        if capability == "build.run":
            _require(task["commit"] is None or fix, f"task {target} is already built")
            if fix:
                _require(fixable(task), f"task {target} has no fix round left or nothing to fix")
            return
        _require(task["commit"] is not None, f"task {target} is not built")
        if capability == "verify.run":
            return
        _require(green(task["verify"]), f"task {target} has not passed its checks")
        return
    _require(capability != "build.run", "build.run needs a task name")
    _require(all(task_ok(task) for task in job["tasks"].values()),
             "every task must be built, pass its checks and pass its review first")
    _require(gate_open(job, "build"), "the build stage is not approved")
    integration = job["integration"]
    if capability == "integrate.run":
        _require(not integration["merged"], "the tasks are already integrated")
        return
    _require(integration["merged"], "the tasks are not integrated")
    if capability == "verify.run":
        return
    _require(green(integration["verify"]), "the integrated branch has not passed its checks")
    if capability == "review.run":
        return
    # pr.open
    _require(review_ok(integration["review"]), "the integrated branch has not passed its review")
    _require(gate_open(job, "integrate"), "the integration stage is not approved")
    _require(job["pr_url"] is None, f"the pull request already exists: {job['pr_url']}")


def check_approval(job, stage):
    """Raise Refused unless a person may approve `stage` now."""
    _require(stage in GATES[job["size"]], f"a {job['size']} job has no {stage} gate; gates: "
             f"{', '.join(GATES[job['size']])}")
    _require(job["plan"] is not None, "there is no plan to approve yet")
    _require(plan_unchanged(job), f"{job['plan']['file']} changed after it was written")
    _require(not approved(job, stage), f"the {stage} stage is already approved")
    if stage == "contract":
        _require(contract_ok(job), "the contract must pass its review and be shown red first")
    elif stage == "build":
        _require(contract_ok(job) and gate_open(job, "contract") and job["tasks"]
                 and all(task_ok(task) for task in job["tasks"].values()),
                 "every task must be built, pass its checks and pass its review first")
    elif stage == "integrate":
        integration = job["integration"]
        _require(integration["merged"] and green(integration["verify"]) and review_ok(integration["review"]),
                 "the integrated branch must pass its checks and its review first")

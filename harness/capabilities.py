"""The eight capabilities. Each checks its own preconditions, does one step, records it and says what comes next.

Agents run only through qk (`qk agent run`); git and the pull request are the harness's own steps. Worktrees and
branches live under the job: the integration branch `qk/<slug>` and one branch per task, `qk/<slug>--<task>`
(git cannot hold both `qk/<slug>` and `qk/<slug>/<task>`).
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time

from harness import gitops, policy, state


PLAN_RE = re.compile(r"<<<QK-PLAN\s*\n(.*?)\n\s*QK-PLAN>>>", re.S)
VERIFY_RE = re.compile(r"```qk-verify\s*\n(.*?)\n```", re.S)
TASKS_RE = re.compile(r"```qk-harness\s*\n(.*?)\n```", re.S)
TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
VERIFY_TIMEOUT = 1800
NO_GIT = ("Do not run git commit, git push, git checkout, git reset, git stash, git branch or open pull requests. "
          "The harness owns all git and GitHub actions.")


class Blocked(Exception):
    """The step ran but cannot be accepted; the job stays where it was and a person looks."""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def record(job, capability, status, reason=""):
    job["log"].append({"at": now(), "capability": capability, "status": status, "reason": reason})


# --------------------------------------------------------------- agent turns


def turn(qk, job, spec, cwd, prompt, *, write, add_dirs=()):
    """One agent turn through qk; its result block, or Blocked with qk's reason."""
    turns = state.job_dir(job["id"]) / "turns"
    turns.mkdir(exist_ok=True)
    stem = turns / f"{len(list(turns.glob('*.events.jsonl'))) + 1:03d}"
    # Only the shared directory (plan, diffs) is added: the job directory also holds every worktree, and a
    # worker given it could write into another task's worktree, where no ownership check looks.
    reply = qk.agent_run(spec, cwd, prompt, write=write, add_dirs=[shared(job), *add_dirs],
                         events=f"{stem}.events.jsonl")
    data = reply.data if isinstance(reply.data, dict) else {}
    Path(f"{stem}.out.md").write_text(str(data.get("text") or reply.stderr), encoding="utf-8")
    job.setdefault("usage", []).append({"spec": spec, "write": write, "status": data.get("status") or "failed",
                                        "usage": data.get("usage"), "seconds": data.get("seconds"),
                                        "events": f"{stem}.events.jsonl"})
    if reply.code != 0 or data.get("status") != "done" or not isinstance(data.get("result"), dict):
        raise Blocked(f"{spec} turn {data.get('status') or 'failed'}: "
                      f"{data.get('reason') or reply.stderr.strip()[-300:] or 'no result'}")
    return data["result"], data.get("text") or ""


def shared(job):
    """Files agents read besides their worktree: the plan and review diffs. No worktree lives here."""
    directory = state.job_dir(job["id"]) / "shared"
    directory.mkdir(exist_ok=True)
    return directory


def family(qk, spec):
    reply = qk.agent_family(spec)
    if reply.code != 0 or not isinstance(reply.data, dict):
        raise Blocked(f"cannot tell the model family of {spec}: {reply.stderr.strip()[-200:]}")
    return reply.data["family"]


def skill(qk, name):
    return qk.root / "skills" / name / "SKILL.md"


# --------------------------------------------------------------- plan


def clean_paths(paths):
    """Repository-relative, normalised paths, or None."""
    if not isinstance(paths, list) or not paths:
        return None
    for path in paths:
        if (not isinstance(path, str) or not path or "\\" in path or path.startswith("/")
                or any(part in ("", ".", "..") for part in path.rstrip("/").split("/"))
                or str(PurePosixPath(path.rstrip("/"))) != path.rstrip("/")):
            return None
    return paths


def parse_plan(text):
    """(plan text, verify commands, contract paths, tasks) from a planner reply, or Blocked naming what is missing."""
    plans = PLAN_RE.findall(text)
    if not plans:
        raise Blocked("planner returned no <<<QK-PLAN ... QK-PLAN>>> block")
    plan = plans[-1].strip() + "\n"
    verify = VERIFY_RE.findall(plan)
    commands = [line.strip() for line in (verify[-1] if verify else "").splitlines()
                if line.strip() and not line.strip().startswith("#")]
    if not commands:
        raise Blocked("the plan has no non-empty ```qk-verify``` block; the harness only runs work it can check")
    blocks = TASKS_RE.findall(plan)
    try:
        layout = json.loads(blocks[-1]) if blocks else {}
        tasks, contract = layout.get("tasks"), layout.get("contract")
    except (ValueError, AttributeError) as error:
        raise Blocked(f"the ```qk-harness``` block is not valid: {error}") from None
    if clean_paths(contract) is None:
        raise Blocked("the ```qk-harness``` block must name the test paths the contract may write in \"contract\"")
    if not isinstance(tasks, list) or not tasks:
        raise Blocked("the plan has no ```qk-harness``` block listing its tasks")
    seen, checks = {}, {}
    for task in tasks:
        name, owns = (task.get("name"), task.get("owns")) if isinstance(task, dict) else (None, None)
        if not isinstance(name, str) or not TASK_NAME_RE.fullmatch(name) or name == "integration" or name in seen:
            raise Blocked(f"task name {name!r} must be a short unique lowercase name other than 'integration'")
        if clean_paths(owns) is None:
            raise Blocked(f"task {name} must own a non-empty list of normalised repository-relative paths")
        verify = task.get("verify")
        if not isinstance(verify, list) or not verify or not all(isinstance(c, str) and c.strip() for c in verify):
            raise Blocked(f"task {name} must list the commands that check it on its own in \"verify\"")
        seen[name], checks[name] = owns, [command.strip() for command in verify]
    overlaps = [(a, b) for a in seen for b in seen if a < b
                and any(owns_overlap(x, y) for x in seen[a] for y in seen[b])]
    if overlaps:
        raise Blocked("tasks own overlapping paths: " + ", ".join(f"{a} and {b}" for a, b in overlaps))
    shared_paths = [name for name in seen if any(owns_overlap(x, y) for x in seen[name] for y in contract)]
    if shared_paths:
        raise Blocked("the contract paths overlap tasks: " + ", ".join(shared_paths))
    return plan, commands, contract, [{"name": name, "owns": owns, "verify": checks[name]}
                                      for name, owns in seen.items()]


def owns_overlap(a, b):
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def owned(path, owns):
    return any(path == item.rstrip("/") or path.startswith(item.rstrip("/") + "/") for item in owns)


def plan_write(job, qk):
    repo = Path(job["repo"])
    if not gitops.is_clean(repo):
        raise Blocked(f"{repo} has uncommitted changes; commit or stash them first")
    prompt = f"""Read {skill(qk, 'qk-plan')} and follow it in `create` mode, read-only.
Repository checkout: {repo} (base {job['base']}). Do not edit files or run commands that write.
Task: {job['task']}
Instead of saving a file, put the complete plan between a line `<<<QK-PLAN` and a line `QK-PLAN>>>`.
Start the plan with a `# <title>` line of at most 60 characters.
The plan MUST contain a fenced block ```qk-verify``` listing, one per line, the shell commands discovered from this
repository that prove the done criteria; each runs from the repository root and exits 0 on success.
The plan MUST also contain a fenced block ```qk-harness``` with JSON {{"contract": ["tests/..."], "tasks":
[{{"name": "...", "owns": ["dir/", "file.py"], "verify": ["command"], "summary": "..."}}]}}. "contract" lists the
test paths where the tests that define done will be written first. Tasks are what separate workers can build at
the same time.
Each task owns the paths it may change, and no path may belong to two tasks. Each task's "verify" commands check
that task on its own, without the other tasks; the qk-verify commands check the whole result. Task names are short
lowercase words.
Tests that define done are written separately before any task is built; do not give them to a task.
{NO_GIT}"""
    result, text = turn(qk, job, job["impl"], repo, prompt, write=False)
    plan, commands, contract, tasks = parse_plan(text)
    path = shared(job) / "plan.md"
    path.write_text(plan, encoding="utf-8")
    job["plan"] = {"file": str(path), "sha256": hashlib.sha256(plan.encode("utf-8")).hexdigest(),
                   "verify": commands, "contract": contract, "tasks": tasks}
    job["tasks"] = {task["name"]: {"owns": task["owns"], "branch": f"qk/{job['slug']}--{task['name']}",
                                   "worktree": str(state.job_dir(job["id"]) / "tasks" / task["name"]),
                                   "checks": task["verify"],
                                   "commit": None, "verify": None, "review": None, "fix_rounds": 0}
                    for task in tasks}
    job["approvals"] = {}
    return {"plan": str(path), "tasks": [task["name"] for task in tasks], "summary": result.get("summary", "")}


# --------------------------------------------------------------- contract


def commit_all(worktree, message):
    gitops.git(worktree, "add", "-A")
    gitops.git(worktree, "commit", "-q", "-m", message)
    return gitops.git(worktree, "rev-parse", "HEAD")


def contract_write(job, qk):
    integration = job["integration"]
    worktree = Path(integration["worktree"])
    if not worktree.exists():
        gitops.git(job["repo"], "worktree", "add", "-q", "-b", integration["branch"], str(worktree),
                   job["base_sha"])
    allowed = job["plan"]["contract"]
    prompt = f"""Write the contract for the approved plan {job['plan']['file']}: tests, and only tests, that pass when
the plan's done criteria are met and fail now, before any task is built. Work only inside {worktree}, and write
only under these paths: {', '.join(allowed)}. Do not implement the feature.
List the test files you wrote in "files". {NO_GIT}"""
    result, _ = turn(qk, job, job["impl"], worktree, prompt, write=True)
    files = gitops.changed_paths(worktree)
    if not files:
        raise Blocked("the contract turn wrote no files")
    outside = [path for path in files if not owned(path, allowed)]
    if outside:
        raise Blocked(f"the contract changed files outside its paths: {', '.join(outside)}; restore {worktree}")
    commit = commit_all(worktree, f"test: contract for {job['slug']}")
    job["contract"] = {"commit": commit, "files": files, "writer": job["impl"], "review": None, "red": None}
    return {"commit": commit, "files": files, "summary": result.get("summary", "")}


def contract_review(job, qk):
    contract = job["contract"]
    if family(qk, job["review"]) == family(qk, contract["writer"]):
        raise Blocked(f"the reviewer {job['review']} is the same model family as the contract writer")
    worktree = Path(job["integration"]["worktree"])
    head = gitops.git(worktree, "rev-parse", "HEAD")
    prompt = f"""Independent read-only review of a test contract. Do not edit files or run commands that write.
Approved plan: {job['plan']['file']}. Contract files in {worktree}: {', '.join(contract['files'])}.
Check that the tests cover every done criterion in the plan, would fail before the work is done, cannot pass by
accident (for example empty or always-true assertions), and test behaviour rather than implementation details.
Put each problem in "findings" as {{"severity": "blocker|important|nit", "file": "...", "line": N,
"claim": "...", "fix": "..."}}. Use status "done" when the review itself completed, even with findings."""
    result, _ = turn(qk, job, job["review"], worktree, prompt, write=False)
    if gitops.git(worktree, "rev-parse", "HEAD") != head or not gitops.is_clean(worktree):
        raise Blocked(f"the read-only review changed {worktree}")
    contract["review"] = {"spec": job["review"], "status": result["status"],
                          "findings": list(result.get("findings") or []), "commit": head}
    contract["commit"] = head  # a person may have edited and committed the contract after a failed review
    return {"findings": len(contract["review"]["findings"]), "serious": len(policy.serious(contract["review"]))}


# --------------------------------------------------------------- build


def build_run(job, qk, name, fix=False):
    task = job["tasks"][name]
    worktree = Path(task["worktree"])
    if not worktree.exists():
        worktree.parent.mkdir(exist_ok=True)
        gitops.git(job["repo"], "worktree", "add", "-q", "-b", task["branch"], str(worktree),
                   job["contract"]["commit"])
    if not gitops.is_clean(worktree):
        raise Blocked(f"{worktree} has uncommitted changes; restore it before building")
    head = gitops.git(worktree, "rev-parse", "HEAD")
    problems = ""
    if fix:
        failed = [check for check in (task["verify"] or {}).get("results", []) if check["exit"] != 0]
        problems = "Fix these problems and nothing else:\n" + "\n".join(
            [f"- `{check['cmd']}` exited {check['exit']}: {check['tail'][-800:]}" for check in failed]
            + [f"- {item.get('file')}:{item.get('line')} {item.get('claim')}" for item in
               policy.serious(task["review"])]) + "\n"
    prompt = f"""Read {skill(qk, 'qk-cook')} and follow it to build task `{name}` of the approved plan
{job['plan']['file']}. Work only inside {worktree}, and change only these paths: {', '.join(task['owns'])}.
The contract tests ({', '.join(job['contract']['files'])}) define done; do not change them.
If the task needs a change outside your paths, stop and report "blocked" with the reason.
{problems}{NO_GIT}"""
    result, _ = turn(qk, job, job["impl"], worktree, prompt, write=True)
    if gitops.git(worktree, "rev-parse", "HEAD") != head or \
            gitops.git(worktree, "symbolic-ref", "-q", "HEAD") != f"refs/heads/{task['branch']}":
        raise Blocked(f"the worker moved {worktree} (commit or branch); only the harness commits. Inspect it")
    files = gitops.changed_paths(worktree)
    outside = [path for path in files if not owned(path, task["owns"])]
    if outside:
        raise Blocked(f"task {name} changed files it does not own: {', '.join(outside)}; restore {worktree}")
    if not files:
        raise Blocked(f"task {name} produced no changes")
    task["commit"] = commit_all(worktree, f"{'fix' if fix else 'feat'}: {name} for {job['slug']}")
    task["verify"], task["review"] = None, None
    if fix:
        task["fix_rounds"] += 1
    return {"task": name, "commit": task["commit"], "files": files, "summary": result.get("summary", "")}


# --------------------------------------------------------------- verify and review


def verify_run(job, qk, target, expect_red=False):
    worktree = Path(job["integration"]["worktree"] if target == "integration" else job["tasks"][target]["worktree"])
    if not gitops.is_clean(worktree):
        raise Blocked(f"{worktree} has uncommitted changes; restore it before verifying")
    results = []
    with gitops.file_lock(state.jobs_root() / "verify.lock"):  # one verification at a time, across jobs
        commands = job["plan"]["verify"] if target == "integration" else job["tasks"][target]["checks"]
        for command in commands:
            code, tail = gitops.run_shell(command, worktree, VERIFY_TIMEOUT)
            results.append({"cmd": command, "exit": code, "tail": tail})
    leftover = gitops.changed_paths(worktree)
    gitops.git(worktree, "checkout", "-q", "--", ".")
    gitops.git(worktree, "clean", "-qfd")
    verify = {"at": now(), "results": results, "leftover": leftover}
    if expect_red:
        job["contract"]["red"] = not policy.green(verify)
        job["contract"]["red_check"] = verify
        return {"red": job["contract"]["red"], "exits": [check["exit"] for check in results]}
    if target == "integration":
        job["integration"]["verify"] = verify
    else:
        job["tasks"][target]["verify"] = verify
    return {"green": policy.green(verify), "exits": [check["exit"] for check in results]}


def review_run(job, qk, target):
    if target == "integration":
        worktree, base, author = Path(job["integration"]["worktree"]), job["base_sha"], job["impl"]
    else:
        task = job["tasks"][target]
        worktree, base, author = Path(task["worktree"]), job["contract"]["commit"], job["impl"]
    if family(qk, job["review"]) == family(qk, author):
        raise Blocked(f"the reviewer {job['review']} is the same model family as the implementer {author}")
    head = gitops.git(worktree, "rev-parse", "HEAD")
    diff = shared(job) / f"review-{target}.diff"
    diff.write_text(gitops.git(worktree, "diff", f"{base}..HEAD"), encoding="utf-8")
    prompt = f"""Independent read-only review. Do not edit files and do not run commands that write.
Repository: {worktree}. Approved plan: {job['plan']['file']}. Full diff: {diff}.
Check correctness, security, regressions, scope versus the plan, and missing tests. Re-read every line you cite.
Severity: blocker = data loss, a security hole, or the change does not do what the plan requires;
important = a wrong result, or a crash or unhandled error on input the code accepts from outside, or a missing
check or test the plan requires; nit = style, wording, or a minor gap.
Report only verified problems introduced by this diff. Put them in "findings" as {{"severity":
"blocker|important|nit", "class": "...", "file": "<repo-relative path>", "line": N, "claim": "...", "fix": "..."}}.
Use status "done" when the review itself completed, even if there are findings."""
    result, _ = turn(qk, job, job["review"], worktree, prompt, write=False)
    if gitops.git(worktree, "rev-parse", "HEAD") != head or not gitops.is_clean(worktree):
        raise Blocked(f"the read-only review changed {worktree}")
    review = {"spec": job["review"], "status": result["status"], "findings": list(result.get("findings") or []),
              "commit": head}
    if target == "integration":
        job["integration"]["review"] = review
    else:
        job["tasks"][target]["review"] = review
    return {"findings": len(review["findings"]), "serious": len(policy.serious(review))}


# --------------------------------------------------------------- integrate and publish


def integrate_run(job, qk):
    worktree = Path(job["integration"]["worktree"])
    if not gitops.is_clean(worktree):
        raise Blocked(f"{worktree} has uncommitted changes")
    for name, task in job["tasks"].items():
        tip = gitops.git(job["repo"], "rev-parse", f"refs/heads/{task['branch']}")
        if tip != task["commit"]:
            raise Blocked(f"{task['branch']} is at {tip[:12]}, not the commit {task['commit'][:12]} that was "
                          "checked and reviewed; a person looks")
    for name, task in job["tasks"].items():
        merge = gitops.git(worktree, "merge", "-q", "--no-ff", "-m", f"merge task {name}", task["commit"],
                           check=False)
        if merge.returncode != 0:
            gitops.git(worktree, "merge", "--abort", check=False)
            raise Blocked(f"task {name} does not merge cleanly into {job['integration']['branch']}: "
                          f"{(merge.stdout + merge.stderr).strip()[-300:]}; a person resolves it")
    job["integration"]["merged"] = True
    job["integration"]["commit"] = gitops.git(worktree, "rev-parse", "HEAD")
    return {"commit": job["integration"]["commit"], "tasks": list(job["tasks"])}


def pr_open(job, qk):
    worktree = Path(job["integration"]["worktree"])
    branch = job["integration"]["branch"]
    head = gitops.git(worktree, "rev-parse", "HEAD")
    if head != job["integration"]["commit"]:
        raise Blocked(f"{branch} moved since it was integrated and checked; check it again")
    title = next((line[2:].strip() for line in policy.plan_text(job).splitlines() if line.startswith("# ")),
                 job["task"])[:72]
    body = state.job_dir(job["id"]) / "pr-body.md"
    body.write_text(policy.plan_text(job) + "\n---\nTasks: " + ", ".join(job["tasks"]) + "\n", encoding="utf-8")
    gitops.git(worktree, "push", "-q", "-u", "origin", f"{branch}:{branch}")
    existing = gitops.gh(worktree, "pr", "list", "--head", branch, "--state", "all", "--json", "url",
                         "--jq", ".[0].url // empty")
    job["pr_url"] = existing or gitops.gh(worktree, "pr", "create", "--base", job["base"], "--head", branch,
                                          "--title", title, "--body-file", str(body)).splitlines()[-1]
    return {"pr_url": job["pr_url"], "head": head}


ACTIONS = {"plan.write": plan_write, "contract.write": contract_write, "contract.review": contract_review,
           "build.run": build_run, "verify.run": verify_run, "review.run": review_run,
           "integrate.run": integrate_run, "pr.open": pr_open}


def run(job_id, capability, qk, target=None, *, expect_red=False, fix=False):
    """Check, act, record, and report {status, reason, next, ...}. The job lock is held throughout."""
    with state.locked(job_id) as job:
        try:
            policy.check(job, capability, target, expect_red=expect_red, fix=fix)
        except policy.Refused as refused:
            record(job, capability, "refused", str(refused))
            return {"status": "refused", "capability": capability, "reason": str(refused), "next": policy.next_step(job)}
        action = ACTIONS[capability]
        try:
            if capability == "build.run":
                details = action(job, qk, target, fix=fix)
            elif capability == "verify.run":
                details = action(job, qk, target, expect_red=expect_red)
            elif capability == "review.run":
                details = action(job, qk, target)
            else:
                details = action(job, qk)
        except (Blocked, gitops.GitError) as error:
            record(job, capability, "blocked", str(error))
            return {"status": "blocked", "capability": capability, "reason": str(error), "next": policy.next_step(job)}
        record(job, capability, "done")
        return dict(details, status="done", capability=capability, reason="", next=policy.next_step(job))


def approve(job_id, stage):
    """A person's decision, recorded against the plan it was made on."""
    with state.locked(job_id) as job:
        try:
            policy.check_approval(job, stage)
        except policy.Refused as refused:
            return {"status": "refused", "stage": stage, "reason": str(refused), "next": policy.next_step(job)}
        job["approvals"][stage] = {"at": now(), "plan_sha256": job["plan"]["sha256"]}
        record(job, f"approve {stage}", "done")
        return {"status": "done", "stage": stage, "reason": "", "next": policy.next_step(job)}

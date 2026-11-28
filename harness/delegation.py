"""An explicit per-job grant, not a sandbox or a replacement for technical checks."""

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import time

from harness import gitops, state


class GrantError(Exception):
    """The job needs a new human decision before it can continue."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def binding(job):
    return {key: job[key] for key in ("id", "repo", "base", "base_sha", "task", "size", "impl", "review",
                                     "options")} | {"branch": job["integration"]["branch"]}


def positive_money(value):
    if isinstance(value, bool):
        raise GrantError("budget and turn estimate must be finite positive USD amounts")
    try:
        amount = float(value)
    except (ValueError, TypeError):
        raise GrantError("budget and turn estimate must be finite positive USD amounts") from None
    if not math.isfinite(amount) or amount <= 0:
        raise GrantError("budget and turn estimate must be finite positive USD amounts")
    return amount


def scope_paths(paths):
    result = []
    for raw in paths:
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or any(part in ("..", ".git") for part in path.parts) or str(path) == ".":
            raise GrantError("scope paths must be repository-relative, non-root paths outside .git")
        result.append(str(path))
    if not result:
        raise GrantError("authorize needs at least one --path delimiting the task's scope")
    return sorted(set(result))


def inside(path, scopes):
    return any(path == root or path.startswith(root + "/") for root in scopes)


def github_slug(url):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url)
    if not match:
        raise GrantError("delegated publication requires a credential-free github.com origin URL")
    return match.group(1)


def origin(job):
    return gitops.git(job["repo"], "remote", "get-url", "origin")


def authorize(job_id, paths, budget_usd, turn_estimate_usd, gh_user):
    """The caller grants this job its scope, soft budget and permission to publish one branch."""
    paths = scope_paths(paths)
    budget, estimate = positive_money(budget_usd), positive_money(turn_estimate_usd)
    if estimate > budget:
        raise GrantError("the turn estimate exceeds the entire soft budget")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", gh_user or ""):
        raise GrantError("authorize needs the GitHub login allowed to publish (--gh-user)")
    with state.locked(job_id) as job:
        if job.get("aborted") or job.get("pr_url"):
            raise GrantError("an aborted or already published job cannot receive a new grant")
        if job.get("plan") and not job.get("delegation_history"):
            raise GrantError("authorize before planning so the first grant binds the planning checkout")
        if any(task.get("running") for task in job["tasks"].values()):
            raise GrantError("a task is running; do not replace its authority")
        if not job["options"].get("contract", True) or not job["options"].get("task_review", True):
            raise GrantError("delegation retains both the contract and task reviews")
        remote = origin(job)
        grant = {"actor": "user", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "binding": binding(job),
                 "paths": paths, "budget_usd": budget, "turn_estimate_usd": estimate,
                 "origin": remote, "github_repo": github_slug(remote), "gh_user": gh_user}
        grant["sha256"] = digest(grant)
        previous = job.get("delegation")
        job["delegation"] = grant
        try:
            from harness import publication
            check(job)
            publication.environment(job)
        except Exception:
            if previous is None:
                job.pop("delegation", None)
            else:
                job["delegation"] = previous
            raise
        job.setdefault("delegation_history", []).append(grant.copy())
        job["log"].append({"at": grant["at"], "capability": "authorize", "status": "done",
                           "actor": "user", "authority": grant["sha256"], "reason": ""})
        return {"status": "done", "authority": grant["sha256"], "grant": grant, "budget": costs(job)}


def revoke(job_id, reason):
    with state.locked(job_id) as job:
        if not job.get("delegation"):
            raise GrantError("this job has no delegation to revoke")
        job["delegation"]["revoked"] = True
        job["log"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "capability": "revoke",
                           "status": "done", "actor": "user", "reason": reason})
    return {"status": "done", "reason": "delegation revoked; running turns are not cancelled"}


def check(job):
    grant = job.get("delegation")
    if grant is None:
        return
    if grant.get("revoked"):
        raise GrantError("delegation was revoked")
    if grant.get("sha256") != digest({key: value for key, value in grant.items() if key != "sha256"}):
        raise GrantError("the delegation receipt changed")
    if grant["binding"] != binding(job):
        raise GrantError("task/repository/base/models/options/branch changed outside the delegation")
    if origin(job) != grant["origin"]:
        raise GrantError("origin changed outside the delegation")
    if not job.get("plan") and gitops.git(job["repo"], "rev-parse", "HEAD") != job["base_sha"]:
        raise GrantError("the planning checkout is not at the delegated base commit")
    if job.get("plan"):
        scopes = grant["paths"]
        paths = [*job["plan"]["contract"], *(path for task in job["tasks"].values() for path in task["owns"])]
        if any(not inside(str(PurePosixPath(path)), scopes) for path in paths):
            raise GrantError("the plan requests paths outside the delegated scope")
        if any(task.get("impl_spec") != job["impl"] or task.get("review_spec") != job["review"]
               for task in job["tasks"].values()):
            raise GrantError("the plan requests model specs outside the delegation")
        plan = Path(job["plan"]["file"]).read_text(encoding="utf-8")
        if hashlib.sha256(plan.encode()).hexdigest() != job["plan"]["sha256"]:
            raise GrantError("the plan changed after its scope was checked")


def costs(job):
    known, unknown, unresolved = 0.0, [], []
    for turn in job.get("usage", []):
        if turn.get("status") == "running":
            unresolved.append(turn.get("events") or turn["spec"])
            continue
        amount = (turn.get("usage") or {}).get("cost_usd")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and amount >= 0:
            known += amount
        else:
            unknown.append(turn["spec"])
    grant = job.get("delegation") or {}
    reserved = sum(task.get("running", {}).get("estimated_usd", 0) for task in job["tasks"].values())
    return {"known_usd": known, "unknown_specs": unknown, "unresolved_turns": unresolved,
            "reserved_usd": reserved, "limit_usd": grant.get("budget_usd"),
            "turn_estimate_usd": grant.get("turn_estimate_usd"), "hard_cap": False}


def check_turn(job, spec):
    check(job)
    grant = job.get("delegation")
    if not grant:
        return
    spent = costs(job)
    # A running turn can be active in another task. Its reservation is already counted; an orphan is not free.
    active = {task.get("running", {}).get("events") for task in job["tasks"].values()}
    if any(event not in active for event in spent["unresolved_turns"]):
        raise GrantError("an interrupted model turn has unresolved cost; inspect its events before resuming")
    if any(item.split(":", 1)[0] != "codex" for item in spent["unknown_specs"]):
        raise GrantError("reported USD is missing for a non-Codex turn; the soft budget cannot be checked")
    estimate = 0 if spec.split(":", 1)[0] == "codex" else grant["turn_estimate_usd"]
    if spent["known_usd"] >= grant["budget_usd"] or \
            spent["known_usd"] + spent["reserved_usd"] + estimate > grant["budget_usd"]:
        raise GrantError("soft budget cannot fund the next turn; in-flight turns may still exceed it")


def interrupted(job):
    """Never clear an orphaned worker marker and blindly re-spend its missing usage."""
    return any(turn.get("status") == "running" for turn in job.get("usage", []))

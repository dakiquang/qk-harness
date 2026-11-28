"""Delegated publishing credentials live only in Git/gh children, never in the grant or agent environment."""

import json
import os
import time
import secrets

from harness import delegation, diagnostics, gitops, state


CI_TIMEOUT = 1800
CI_INTERVAL = 10


def environment(job, *, deadline=None):
    delegation.check(job, deadline=deadline)
    grant = job["delegation"]
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["GH_HOST"] = "github.com"
    token = gitops.gh(job["repo"], "auth", "token", "--hostname", "github.com", "--user", grant["gh_user"],
                      env=env, deadline=deadline)
    if not token:
        raise delegation.GrantError("no token for the authorized GitHub account")
    env["GH_TOKEN"] = token
    env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="credential.https://github.com.helper", GIT_CONFIG_VALUE_0="",
               GIT_CONFIG_KEY_1="credential.https://github.com.helper", GIT_CONFIG_VALUE_1="!gh auth git-credential",
               GIT_CONFIG_KEY_2="remote.origin.pushurl",
               GIT_CONFIG_VALUE_2=f"https://github.com/{grant['github_repo']}.git")
    login = gitops.gh(job["repo"], "api", "user", "--jq", ".login", env=env, deadline=deadline)
    if login.lower() != grant["gh_user"].lower():
        raise delegation.GrantError("the publishing token belongs to a different GitHub account")
    info = json.loads(gitops.gh(job["repo"], "repo", "view", grant["github_repo"], "--json",
                               "nameWithOwner,viewerPermission", env=env, deadline=deadline))
    if info.get("nameWithOwner", "").lower() != grant["github_repo"].lower() or \
            info.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
        raise delegation.GrantError("the authorized GitHub account cannot publish to the authorized repository")
    return env


def open_pr(job, plan_text):
    integration, grant = job["integration"], job["delegation"]
    worktree, branch = integration["worktree"], integration["branch"]
    if gitops.git(worktree, "rev-parse", "HEAD") != integration["commit"] or \
            gitops.git(worktree, "symbolic-ref", "--short", "HEAD") != branch:
        raise delegation.GrantError("the integration branch moved after verification/review")
    if gitops.git(worktree, "remote", "get-url", "origin") != grant["origin"]:
        raise delegation.GrantError("the publishing worktree has a different origin")
    env = environment(job)
    existing = json.loads(gitops.gh(worktree, "pr", "list", "--repo", grant["github_repo"], "--head", branch,
                                   "--state", "all", "--json", "url,state,baseRefName", env=env))
    if existing and (len(existing) != 1 or existing[0].get("state") != "OPEN" or
                     existing[0].get("baseRefName") != job["base"]):
        raise delegation.GrantError("the job branch already has a closed or differently targeted PR; a person looks")
    title = next((line[2:].strip() for line in plan_text.splitlines() if line.startswith("# ")), "Job completion")[:72]
    # Publish a projection, not the private plan, shell commands, task prompt or raw event logs.
    body = (f"## Summary\n{title}\n\n## Evidence\n"
            f"- Contract checked red before build.\n"
            f"- Integrated verification: {len(integration['verify']['results'])} checks passed.\n"
            f"- Task and integration reviews completed; implementation/review families differ.\n"
            f"- Verified/reviewed head: `{integration['commit']}`.\n"
            "- CI is checked separately against this head; merge remains a human decision.\n")
    gitops.git(worktree, "push", "-q", "-u", "origin", f"{branch}:{branch}", env=env)
    url = existing[0]["url"] if existing else gitops.gh(
        worktree, "pr", "create", "--repo", grant["github_repo"], "--base", job["base"], "--head", branch,
        "--title", title, "--body", body, "--draft", env=env).splitlines()[-1]
    expected = f"https://github.com/{grant['github_repo']}/pull/"
    if not url.startswith(expected) or not url[len(expected):].isdigit():
        raise delegation.GrantError("GitHub returned a PR outside the delegated repository")
    job["pr_url"] = url
    return {"pr_url": url, "head": integration["commit"]}


def check_ci(job, *, deadline=None, check=None):
    """Read-only polling on a snapshot. All reads, retry and sleep share one deadline."""
    deadline = time.monotonic() + CI_TIMEOUT if deadline is None else deadline
    grant, integration = job["delegation"], job["integration"]
    ci = job["ci"] = {"head": integration["commit"], "status": "pending", "checks": [], "attempts": []}
    if not job["pr_url"].startswith(f"https://github.com/{grant['github_repo']}/pull/"):
        raise delegation.GrantError("the PR is outside the delegated repository")
    env = environment(job, deadline=deadline)

    def checkpoint():
        if check:
            check()
        if time.monotonic() >= deadline:
            raise delegation.GrantError("CI is absent or still pending at the wait deadline; run again to check it")

    def read(*args):
        request = secrets.token_hex(6)
        for attempt in (1, 2):
            checkpoint()
            started = time.monotonic()
            receipt = {"request_id": request, "attempt": attempt, "operation": "gh " + args[0],
                       "deadline_monotonic": deadline, "remaining_s": max(0, deadline - started)}
            try:
                result = gitops.gh(integration["worktree"], *args, env=env, deadline=deadline)
            except gitops.GitError as error:
                receipt.update(status="timeout" if error.timed_out else "failed",
                               elapsed_s=time.monotonic() - started)
                ci["attempts"] = (ci["attempts"] + [receipt])[-32:]
                diagnostics.capture(state.job_dir(job["id"]) / "diagnostics",
                                    dict(error.details, job_id=job["id"], capability="pr.check",
                                         reason=str(error), **receipt))
                if not (attempt == 1 and error.timed_out and error.cleanup_known and error.effect == "read"):
                    raise
                checkpoint()
            else:
                receipt.update(status="done", elapsed_s=time.monotonic() - started)
                ci["attempts"] = (ci["attempts"] + [receipt])[-32:]
                return result

    def pr():
        data = json.loads(read("pr", "view", job["pr_url"], "--repo", grant["github_repo"], "--json",
                               "state,headRefOid,headRefName,baseRefName,statusCheckRollup"))
        if not isinstance(data, dict) or data.get("headRefOid") != integration["commit"] or \
                data.get("headRefName") != integration["branch"] or data.get("baseRefName") != job["base"] or \
                data.get("state") != "OPEN":
            raise delegation.GrantError("the PR head/base/state changed; do not reuse stale verification")
        rollup = data.get("statusCheckRollup") or []
        if not isinstance(rollup, list) or any(not isinstance(item, dict) for item in rollup):
            raise delegation.GrantError("invalid CI check response")
        checks = []
        for item in rollup:
            result = item.get("conclusion") if item.get("status") == "COMPLETED" else item.get("state")
            checks.append({"name": item.get("name") or item.get("context"),
                           "result": str(result or "PENDING").upper()})
        ci.update(checks=checks, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        if any(item["result"] in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
                                  "STARTUP_FAILURE", "STALE") for item in checks):
            ci["status"] = "failed"
            raise delegation.GrantError("CI failed for the published head; no post-PR fix round is authorized")
        return checks and any(item["result"] == "SUCCESS" for item in checks) and \
            all(item["result"] in ("SUCCESS", "SKIPPED", "NEUTRAL") for item in checks)

    while True:
        checkpoint()
        if pr():
            reported = read("api", f"repos/{grant['github_repo']}/actions/runs?"
                            f"head_sha={integration['commit']}&per_page=100",
                            "--method", "GET", "--paginate", "--jq",
                            ".workflow_runs[] | {id, status, conclusion} | @json")
            workflows = [json.loads(line) for line in reported.splitlines()]
            if any(not isinstance(run, dict) for run in workflows):
                raise delegation.GrantError("invalid Actions runs response")
            ci["workflows"] = workflows
            completed = all(run.get("status") == "completed" for run in workflows)
            # A run that fails before creating jobs (startup failure) has no check in the rollup.
            if completed and any(str(run.get("conclusion") or "").lower() not in ("success", "skipped", "neutral")
                                 for run in workflows):
                ci["status"] = "failed"
                raise delegation.GrantError("CI failed for the published head; no post-PR fix round is authorized")
            if completed and pr():
                checkpoint()
                ci["status"] = "success"
                return {"pr_url": job["pr_url"], "head": integration["commit"], "ci": ci}
        checkpoint()
        time.sleep(min(CI_INTERVAL, max(0, deadline - time.monotonic())))

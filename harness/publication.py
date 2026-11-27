"""Delegated publishing credentials live only in Git/gh children, never in the grant or agent environment."""

import json
import os
import time

from harness import delegation, gitops


CI_TIMEOUT = 1800
CI_INTERVAL = 10


def environment(job):
    delegation.check(job)
    grant = job["delegation"]
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["GH_HOST"] = "github.com"
    token = gitops.gh(job["repo"], "auth", "token", "--hostname", "github.com", "--user", grant["gh_user"], env=env)
    if not token:
        raise delegation.GrantError("no token for the authorized GitHub account")
    env["GH_TOKEN"] = token
    env.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="credential.https://github.com.helper", GIT_CONFIG_VALUE_0="",
               GIT_CONFIG_KEY_1="credential.https://github.com.helper", GIT_CONFIG_VALUE_1="!gh auth git-credential",
               GIT_CONFIG_KEY_2="remote.origin.pushurl",
               GIT_CONFIG_VALUE_2=f"https://github.com/{grant['github_repo']}.git")
    login = gitops.gh(job["repo"], "api", "user", "--jq", ".login", env=env)
    if login.lower() != grant["gh_user"].lower():
        raise delegation.GrantError("the publishing token belongs to a different GitHub account")
    info = json.loads(gitops.gh(job["repo"], "repo", "view", grant["github_repo"], "--json",
                               "nameWithOwner,viewerPermission", env=env))
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
        "--title", title, "--body", body, env=env).splitlines()[-1]
    expected = f"https://github.com/{grant['github_repo']}/pull/"
    if not url.startswith(expected) or not url[len(expected):].isdigit():
        raise delegation.GrantError("GitHub returned a PR outside the delegated repository")
    job["pr_url"] = url
    return {"pr_url": url, "head": integration["commit"]}


def check_ci(job):
    """Wait for reported checks for exactly the published head. Empty/pending is never success."""
    grant, integration = job["delegation"], job["integration"]
    if not job["pr_url"].startswith(f"https://github.com/{grant['github_repo']}/pull/"):
        raise delegation.GrantError("the PR is outside the delegated repository")
    env = environment(job)
    deadline = time.monotonic() + CI_TIMEOUT
    job["ci"] = {"head": integration["commit"], "status": "pending", "checks": []}
    while True:
        data = json.loads(gitops.gh(integration["worktree"], "pr", "view", job["pr_url"], "--repo",
                                   grant["github_repo"], "--json",
                                   "state,headRefOid,headRefName,baseRefName,statusCheckRollup", env=env))
        if data.get("headRefOid") != integration["commit"] or data.get("headRefName") != integration["branch"] or \
                data.get("baseRefName") != job["base"] or data.get("state") != "OPEN":
            job["ci"].update(status="blocked", reason="the PR head/base/state changed; do not reuse stale verification")
            raise delegation.GrantError(job["ci"]["reason"])
        checks = []
        for item in data.get("statusCheckRollup") or []:
            state = str(item.get("state") or "").upper()
            conclusion = str(item.get("conclusion") or "").upper()
            result = conclusion if item.get("status") == "COMPLETED" else state
            checks.append({"name": item.get("name") or item.get("context"), "result": result or "PENDING"})
        job["ci"].update(checks=checks, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        failed = [item for item in checks if item["result"] in
                  ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE")]
        if failed:
            job["ci"].update(status="failed", reason="CI failed for the published head; no post-PR fix round is authorized")
            raise delegation.GrantError(job["ci"]["reason"])
        if checks and any(item["result"] == "SUCCESS" for item in checks) and \
                all(item["result"] in ("SUCCESS", "SKIPPED", "NEUTRAL") for item in checks):
            reported = gitops.gh(
                integration["worktree"], "api",
                f"repos/{grant['github_repo']}/actions/runs?head_sha={integration['commit']}&per_page=100",
                "--paginate", "--jq", ".workflow_runs[] | {id, status} | @json", env=env)
            workflows = [json.loads(line) for line in reported.splitlines()]
            job["ci"]["workflows"] = workflows
            if all(run.get("status") == "completed" for run in workflows):
                job["ci"]["status"] = "success"
                return {"pr_url": job["pr_url"], "head": integration["commit"], "ci": job["ci"]}
        if time.monotonic() >= deadline:
            job["ci"].update(status="pending", reason="CI is absent or still pending at the wait deadline; run again to check it")
            raise delegation.GrantError(job["ci"]["reason"])
        time.sleep(CI_INTERVAL)

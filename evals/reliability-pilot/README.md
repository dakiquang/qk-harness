# Isolated reliability pilot (offline fixture)

This is a real two-task evaluation, **not an executed pilot or a production
feature**. Nothing here enables CI, installs a runtime, calls an agent, creates a
repository or grants permission to spend/publish. Python 3.9+ and the standard
library are sufficient for the offline oracle and its tests.

## Fixed behavior and ownership

| Task | Only worker-owned path | API |
| --- | --- | --- |
| `csv` | `evals/reliability-pilot/solution/csv_export.py` | `render(records) -> str` |
| `json` | `evals/reliability-pilot/solution/json_export.py` | `render(records) -> str` |

Input is a list of records with exactly `name` and `note`, both strings. CSV must
roundtrip through `csv.reader` with the `name,note` header; JSON must roundtrip
through `json.loads` to the input list. Preserve values, record order and input.
Cases include empty input/fields, Unicode, commas, quotes and embedded CR/LF.
Invalid-input behavior and JSON formatting are not requirements.

There are deliberately **no seed solutions**. Contract tests generated during
the pilot belong separately to `evals/reliability-pilot/generated-contract/`.
Task workers must not modify the contract, oracle, workflow, spec or each other's
module. `task.txt` specifies the planner's exact `qk-harness` ownership block and
`qk-verify` commands. Each task checks only its own module; integration checks the
generated contract **and** both oracle tasks. The planner stays read-only.

## Offline checks (controller runs these, no agents)

From the harness source root:

```sh
python3 -m unittest discover -s tests -p 'test_reliability_pilot.py' -v
# After all implementation work is integrated, the controller runs the full suite:
python3 -m unittest discover -s tests -v
```

Tests create correct and deliberately incorrect implementations only in temporary
directories. They prove acceptance/rejection, task isolation, input-mutation
rejection, missing-behavior red and spawn-safe imports. Importing either the
oracle or test module does not discover/run tests, create repos or start agents.
The fixture is not registered with qk's eval registry or the harness `eval` CLI.

## Freeze and local seed preparation — no invented identities

Use three distinct paths: the source checkout, a frozen runner checkout outside
it, and an isolated target seed clone outside both. Choose a new state/log
directory outside every repo, never reuse a previous job's state. Record absolute
paths and verify they are not nested/aliased. Do not put credentials, company data
or AWS configuration in the fixture or public evidence. Isolation is **not an OS
sandbox**: solution imports, plan commands and generated tests execute trusted
code with the caller's OS access.

1. Finish offline verification. Show Quang the diff and request permission to
   commit the runner as **H**; do not commit automatically. Record the real H,
   unchanged `qk.lock` bytes/tag/SHA and oracle SHA-256. Until approved and actually
   committed, label H **pending freeze**, not a fabricated SHA.
2. With a real H, local clones can be prepared without a remote/network operation.
   The following is a command template, not a command to run before choosing the
   approved absolute paths and real H:

   ```sh
   git clone --no-hardlinks --no-checkout "$SOURCE" "$RUNNER"
   git -C "$RUNNER" checkout --detach "$H"
   git clone --no-hardlinks "$RUNNER" "$SEED"
   git -C "$SEED" switch -c "$PILOT_BASE_BRANCH"
   ```

   `$PILOT_BASE_BRANCH` is a dedicated `pilot/reliability-base-<id>`, never main.
   A local clone initially has a local origin, **not** the publication origin.
   Do not rewrite it or publish until the account/repo/remote-write gate below.
   Confirm the seed has neither solution file. Keep the runner immutable and run
   its CLI/oracle by absolute path, not from a worker checkout or qk on `PATH`.
3. Render every `__FROZEN_ORACLE_CHECK_PY__` in `task.txt` to the absolute
   `$RUNNER/evals/reliability-pilot/check.py`, shell/JSON-quoted correctly. Save
   the rendered task in the evidence directory outside the target. Inspect its
   complete ownership and verify commands before `start`/`authorize`; no
   placeholders may remain. Render `__PILOT_BASE_BRANCH__` in the workflow
   template to the reviewed base branch, but keep it inactive pending permission.
4. Request explicit bootstrap/commit permission: intended repo/account, local
   branch and remote base branch, activation of the grader workflow, commits H/E,
   and eventual base push/one draft PR. The suggested personal repo is
   `dakiquang/qk-harness`; it is **not** pre-authorized. No force-push, auth switch,
   stored Git config changes or production branch changes are permitted.
5. Only after the corresponding approval, copy the rendered template to
   `.github/workflows/reliability-pilot.yml` **in the isolated seed**, retaining
   existing product workflows unchanged. Show its diff and obtain the required
   local commit approval before creating seed commit **E**, descended from H.
   Do not add either solution. Record real E; hold the remote base ref at E for
   the entire pilot. Record and compare oracle digests at H and E. Do not invent
   E or mark the freeze complete before the commit exists.

The workflow checks out `pull_request.head.sha` for solutions and
`pull_request.base.sha` for the oracle in a separate directory. That base must
be E: an immutable pilot base plus an independent inspector checking the saved E
provides the binding. It does **not** trust an oracle from the PR head. Using the
base event SHA avoids the impossible requirement to embed a commit's own SHA in
that same seed commit. The workflow verifies checkout identities and checks its
oracle digest before/after execution. No job-level skip/conditional, path filter,
`continue-on-error`, write token, explicit secret or deployment is added. Existing
product tests/security checks must still run; the grader is additional, not a
replacement. This template is not an active workflow in the source repository.

Run the oracle from H against the ready seed and retain exit/output/digests:

```sh
python3 "$RUNNER/evals/reliability-pilot/check.py" --repo "$SEED" --task all
```

Expected exit is **1**, with missing-implementation failures for CSV and JSON,
not a checker/tool/setup failure. A nonempty failure message by itself does not
prove that generated contract tests are valid: inspect their later red evidence
for missing behavior, not an import/setup/discovery error. Missing solutions are
intentional; do not create temporary passing implementations in the actual seed.

## Mandatory paid-run/publication gate

Prepare an external manifest and show it to Quang before any model invocation,
base push or PR creation. Earlier bootstrap permission does not imply approval
of unknown cost/models. Record, without guessing:

- real H and E; unchanged lock identity; oracle digest; target origin/account,
  branch topology, absolute runner/seed/state/log paths and rendered task digest;
- permission to push only the immutable pilot base and job branch, enable the
  reviewed workflow and open exactly one draft PR; no merge, release or cleanup;
- two explicit `runtime:model` specs from different families, requested spec vs
  actual model metadata (actual is `unknown` when unavailable), billing mode and
  authorized existing Codex quota; never silently buy quota/change billing;
- current per-model prices and token/turn assumptions, USD estimate/range, soft
  budget, non-Codex per-turn reservation estimate, concurrent reservations and
  possible in-flight overshoot; unknown USD is **not zero/free**;
- normal path: **4 implementation + 4 review turns = 8 total** (plan, contract,
  two builds; contract, two task reviews, integration review). At most one normal
  technical fix round for contract, each task and integration can add **4 + 4**,
  making **16 turns**. Large adds four gate receipts, not more reviews than
  Medium with the same options. No extra review just because this is Large;
- one-night window, stop/blocked policy, evidence destination and person running
  the approved command in an independent Terminal. No `start-all`/`drain` reuse.

There is intentionally no default model, H/E, dollar estimate or budget here.
Historical costs are not a current quote. If a hard USD cap is required, stop at
this gate: the harness provides only a soft stop. Do not add another model turn,
`grant-fix`, `accept-contract`, model change or budget increase without new
explicit permission. A blocked run is not end-to-end completion.

## Execution shape — only after those approvals

Recheck the frozen CLI's `--help`, pinned runtime/bin/lock and scoped GitHub
identity before using this skeleton. Resolve all variables from the approved
manifest; do not use CLI model defaults. `doctor` may fetch and therefore belongs
after the applicable network/runtime permission, not the offline fixture tests.
Run the frozen launcher (which binds the frozen source), not `python -m harness`
from the mutable target. No new parallelism flag is needed: default is two.

```sh
export QK_HARNESS_STATE_DIR="$APPROVED_NEW_STATE_DIR"
"$RUNNER/bin/qk-harness" start --repo "$SEED" --base "$PILOT_BASE_BRANCH" \
  --task "$(cat "$RENDERED_TASK")" --size large \
  --impl "$APPROVED_IMPL_SPEC" --review "$APPROVED_REVIEW_SPEC"
# Set JOB to the actual returned job id, then grant BEFORE plan.write/run:
"$RUNNER/bin/qk-harness" authorize "$JOB" \
  --path evals/reliability-pilot/solution/csv_export.py \
  --path evals/reliability-pilot/solution/json_export.py \
  --path evals/reliability-pilot/generated-contract \
  --budget-usd "$APPROVED_SOFT_BUDGET" \
  --turn-estimate-usd "$APPROVED_NON_CODEX_TURN_ESTIMATE" \
  --gh-user "$APPROVED_GH_USER"
# Independent Bash Terminal; retain the actual exit even when tee is used:
set -o pipefail
"$RUNNER/bin/qk-harness" run "$JOB" --no-wait-limits 2>&1 | tee "$APPROVED_RUN_LOG"
```

Keep contract/per-task review enabled and parallelism on. `--until HH:MM` only
limits waiting for runtime usage limits; it is **not a whole-job hard deadline**.
The default pilot explicitly uses `--no-wait-limits`: quota exhaustion blocks,
not another silently scheduled attempt. A single grant covers exactly four
Large receipts (`plan`, `contract`, `build`, `integrate`), each with
`actor=delegated` and the same authority hash. No extra human approval at each
gate and no additional approval/review turn should be manufactured.

## Evidence and completion

Keep original artifacts; do not close/merge PRs or delete worktrees/branches.
Collect real start/end/exit, H/E/lock/task identities, immutable oracle digests
before/after, plan hash, contract review and behavior-red, disjoint task ownership
and overlapping execution timestamps, task commits/verifies/cross-family reviews,
exact integration commit, four delegated receipts, usage/reservations/unknowns,
and PR URL/head/base. Run the H oracle against the actual integrated target with
`--task all`, not a checker modified by a worker.

The external pilot inspector must independently confirm the PR base OID is saved
E and the head is the reviewed integration commit. Inspect Actions run/head and
check identities: the job **`reliability-pilot` must actually be `SUCCESS`, not
skipped**, and existing product CI must also pass on that head. Do not infer this
from a green aggregate or from a different SHA; generic runner CI guards do not
supply a pilot-specific required-workflow registry. Confirm the oracle digest at
E and workflow diff are the reviewed ones, and that the remote base did not move.
Use scoped read-only `gh pr view`/`gh run view` only after permission, without
claiming they were run during offline preparation.

Only an actual two-task live run, the expected receipts/commits and exact-head CI
constitute this pilot's completion. Preserve blocked/failure/unknown evidence and
ask before any additional paid attempt. One successful night is not a measured
failure rate, a per-task-review ablation, proof of a historical root cause or
permission for broader autonomy/AWS.

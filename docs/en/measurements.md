# Measurements

[Tiếng Việt](../vi/measurements.md) · [Guide](guide.md)

Every optional part of the harness has to show what it changes. These are real runs with `qk-harness eval`, Codex `gpt-6.1-terra` building and Claude (`claude:`) reviewing, against qk v0.9.0 on 23/11/2026 (contract) and qk v0.10.0 on 24/11/2026 (task review, parallel tasks). Each cell is **one run**, so a single difference can be noise.

## Cases

Only two of qk's eval cases build something and so go through the harness: `csv-export` (add a module and its tests) and `pagination-empty-page` (find and fix a bug, add a regression test). Both have one task, so parallel tasks are measured on real two-task work instead (below). Baseline: one qk skill turn, from qk's `evals/baseline.json`: 6/6 on both cases, 88–372 thousand tokens, 35–41 s.

## With and without the contract

| Case | With contract | Without contract (`--no-contract`) |
|---|---|---|
| `csv-export` | 6/6; 425,812 tokens; 197 s; 0.28 USD | 6/6; 281,562 tokens; 130 s; 0.18 USD |
| `pagination-empty-page` | 6/6; 406,222 tokens; 152 s; 0.24 USD | 5/6; 291,408 tokens; 109 s; 0.16 USD |

USD is the Claude share only; Codex reports no cost.

- Without a contract, the worker wrote a test requiring behaviour the task did not state (an empty marker meaning "more pages"); the correct fix failed it, and neither the task review nor the integration review caught it. With a contract, the instruction to test only what the plan asks and the contract review prevented it.
- The contract stage costs about 45–50% more tokens and 40–50% more time.
- Decision: the contract stays on by default. The comparison mixes two things (the stage and the "test only what the plan asks" instruction, which only the contract prompt carries) and has one run per cell.

## With and without per-task review

The contract is on in all four runs.

| Case | Per-task review | Without (`--no-task-review`) |
|---|---|---|
| `csv-export` | 6/6; 392,368 tokens; 188 s; 0.29 USD | 6/6; 332,137 tokens; 178 s; 0.19 USD |
| `pagination-empty-page` | 6/6; 373,729 tokens; 160 s; 0.24 USD | 6/6; 334,396 tokens; 136 s; 0.16 USD |

- Without per-task review, both cases still scored 6/6, with 11–15% fewer tokens, 5–15% less time and about a third less Claude cost. The integrated review still runs.
- The default is unchanged: per-task review stays on. These cases have one task, so the review the switch removes overlaps the integrated review almost entirely; on work with several tasks it is the only review each task gets before integration.

## Parallel and sequential tasks

The same two-task work (three small fixes in `harness/evaluate.py`; review findings in `status JOB`, with docs), run as two Medium jobs on throwaway clones of this repository, without the contract stage, until `pr.open`.

| | Parallel (default) | Sequential (`--sequential`) |
|---|---|---|
| Whole job | 653 s | 766 s |
| Task stage (build and review) | 300 s | 411 s |
| Tokens | 1,424,166 | 1,437,947 |
| Claude cost | 0.50 USD | 0.45 USD |
| Integrated suite | 117 tests OK | 117 tests OK |
| Review findings | none | 3 nits |

- Parallel tasks saved the length of the shorter task (about 110 s, 15% of the job) for the same tokens and result. About 245 s of each job is the full test suite on the integrated branch, which parallel tasks do not shorten.
- The parallel job's branch was merged as [#21](https://github.com/dakiquang/qk-harness/pull/21), after a cross-review found two decisions with another cause for which `status` listed findings, and the contract's findings it left out.

## Earlier runs that shaped the design

- Before the contract had a revision round and the "test only what the plan asks" instruction, `csv-export` stopped twice at a contract review with real findings, and `pagination-empty-page` scored 5/6 because of a contract test the task did not ask for.
- One `--no-contract` run of `csv-export` stopped at planning: the planner read "each task writes its tests" as a request to write files in its read-only turn. A rerun passed; treated as noise.
- A turn stopped with SIGTERM was ended cleanly by `qk agent run`: no agent process and no scratch directory were left.

## Not measured yet

- A Large job through all four gates: the one attempt stopped before the contract gate (see the guide's Limits).
- Per-task review on work with several tasks.
- Reviewer strictness on qk's three `runner-review` cases.

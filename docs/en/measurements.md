# Measurements

[Tiếng Việt](../vi/measurements.md) · [Guide](guide.md)

Every optional part of the harness has to show what it changes. These are real runs with `qk-harness eval`, Codex `gpt-6.1-terra` building and Claude (`claude:`) reviewing, against qk v0.9.0, on 23/11/2026. Each cell is **one run**, so a single difference can be noise.

## Cases

Only two of qk's eval cases build something and so go through the harness: `csv-export` (add a module and its tests) and `pagination-empty-page` (find and fix a bug, add a regression test). Both have one task, so they cannot show the value of parallel tasks. Baseline: one qk skill turn, from qk's `evals/baseline.json`: 6/6 on both cases, 88–372 thousand tokens, 35–41 s.

## With and without the contract

| Case | With contract | Without contract (`--no-contract`) |
|---|---|---|
| `csv-export` | 6/6; 425,812 tokens; 197 s; 0.28 USD | 6/6; 281,562 tokens; 130 s; 0.18 USD |
| `pagination-empty-page` | 6/6; 406,222 tokens; 152 s; 0.24 USD | 5/6; 291,408 tokens; 109 s; 0.16 USD |

USD is the Claude share only; Codex reports no cost.

- Without a contract, the worker wrote a test requiring behaviour the task did not state (an empty marker meaning "more pages"); the correct fix failed it, and neither the task review nor the integration review caught it. With a contract, the instruction to test only what the plan asks and the contract review prevented it.
- The contract stage costs about 45–50% more tokens and 40–50% more time.
- Decision: the contract stays on by default. The comparison mixes two things (the stage and the "test only what the plan asks" instruction, which only the contract prompt carries) and has one run per cell.

## Earlier runs that shaped the design

- Before the contract had a revision round and the "test only what the plan asks" instruction, `csv-export` stopped twice at a contract review with real findings, and `pagination-empty-page` scored 5/6 because of a contract test the task did not ask for.
- One `--no-contract` run of `csv-export` stopped at planning: the planner read "each task writes its tests" as a request to write files in its read-only turn. A rerun passed; treated as noise.
- A turn stopped with SIGTERM was ended cleanly by `qk agent run`: no agent process and no scratch directory were left.

## Not measured yet

- Per-task review on and off (`--no-task-review`): the switch exists; no real run yet.
- Parallel against sequential: needs work with several tasks.
- Reviewer strictness on qk's three `runner-review` cases.

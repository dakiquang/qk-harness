# qk-harness

A personal agent harness: it runs one piece of work that has several tasks (a contract that must fail first, workers on separate worktrees, integration, a review by a different model family, at most one fix round, one pull request) on top of [qk](https://github.com/dakiquang/qk). It never merges.

qk-harness depends on qk one way. It uses only qk's versioned [CLI contract](https://github.com/dakiquang/qk/blob/v0.9.0/docs/en/cli-contract.md), never imports qk's Python modules, and runs the qk pinned in [`qk.lock`](qk.lock), not the one installed on the machine.

## Status

Early work. What exists today:

- `qk.lock`: the qk repository, tag, commit and contract version this harness runs.
- `harness/qk_cli.py`: the only place that runs qk. It fetches the locked tag into the harness state directory, refuses to run when the tag points at another commit, when the checkout moved or changed, or when `qk contract --json` does not report contract 1 with the commands the harness needs. A call past its timeout sends qk SIGTERM so qk stops its agent.
- `python3 -m harness doctor`: fetch and check the locked qk, and print what was found.

## Use

```bash
python3 -m harness doctor
```

The state directory is `$QK_HARNESS_STATE_DIR`, else `$XDG_STATE_HOME/qk-harness`, else `~/.local/state/qk-harness`. Python 3.9+ and `git`; no other dependencies.

## Test

```bash
python3 -m unittest discover -s tests
```

The tests use a stand-in qk (`tests/fake_qk.py`) that answers each contract command; they need no network and run no agent.

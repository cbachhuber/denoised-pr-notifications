# End-to-end tests

These tests exercise the real [run.sh](../../run.sh) against real pull requests on github.com.
One-time setup and credentials are described in [CONTRIBUTING.md](../../CONTRIBUTING.md#end-to-end-tests).

## Test repository

All test pull requests are filed in **[cbachhuber-pr-notify-e2e-tests/sample-repo](https://github.com/cbachhuber-pr-notify-e2e-tests/sample-repo)**.
It is public, belongs to the `cbachhuber-pr-notify-e2e-tests` organization, and is dedicated to these tests:
each run closes every pull request there that has been open for over an hour.
Which repository is used is configured via `E2E_ORG` and `E2E_REPO`.

[set_up_repository.py](set_up_repository.py) idempotently prepares it with the `branch-without-merge-queue`, a merge queue ruleset on `main`, and the workflows the tests rely on.

## How a run works

1. Each scenario opens its own pull request, acts on it as one of the actors, and declares which Teams cards it should cause.
2. A local HTTPS server stands in for the Teams webhooks and records every card posted to it.
3. `run.sh` is run like cron would, in a clean environment and with a temporary state directory, until all expected cards arrived or three minutes passed.
4. After a quiet period, it runs once more, to catch duplicate or unexpected cards.
5. Each scenario passes if the cards for its pull request match its declaration exactly.
6. All pull requests of the run are closed afterwards.

## Actors

| Actor | Kind | Role |
| --- | --- | --- |
| `me` | Machine account | The recipient whose token `run.sh` uses. |
| `alice`, `bob` | GitHub Apps | Other people commenting, reviewing and merging. |

## Files

| File | Contents |
| --- | --- |
| [scenarios.py](scenarios.py) | The scenarios. Every function taking the run's participants and returning an `Expectation` is discovered automatically; `@known_gap` marks one where pr-notify falls short of the README, and runs it as a non-strict expected failure. |
| [test_scenarios.py](test_scenarios.py) | One parametrized test per scenario, comparing the received cards to the expected ones. |
| [test_alerts.py](test_alerts.py) | A rejected token alerts once per channel, then throttles. Needs no pull requests. |
| [actors.py](actors.py) | The GitHub API client for each actor, and the pull requests they open. |
| [conftest.py](conftest.py) | The HTTPS webhook sink, the `run.sh` wrapper, and the session fixture that arranges all scenarios and collects the cards. |
| [set_up_repository.py](set_up_repository.py) | Prepares the test repository. |

## Running

```bash
set -a; . ~/.config/pr-notify/e2e.env; set +a
uv run pytest -m e2e
```

Plain `uv run pytest` skips these tests. Use `-k <scenario>` to arrange and assert on only some scenarios.

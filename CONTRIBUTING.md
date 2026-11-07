# Development

Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`, execute unit tests with `uv run pytest`.

This repository's lint rules live in [.pre-commit-config.yaml](.pre-commit-config.yaml) and are executed with [prek](https://prek.j178.dev/), a fast drop-in replacement for `pre-commit`.
You can locally run the same checks that CI runs.

Install prek with any method from the [prek installation guide](https://prek.j178.dev/installation/), for example:

```bash
uv tool install prek
```

Then install the Git hooks so the checks run before every commit:

```bash
prek install
```

To check the whole repository on demand:

```bash
prek run --all-files
```

## End-to-end tests

The tests in [tests/e2e](tests/e2e) drive real pull requests on github.com and run [run.sh](run.sh) against a local HTTPS webhook sink.
Plain `uv run pytest` skips them; they need the one-time setup below.

### Actors

| Role | Kind | Why |
| --- | --- | --- |
| `me` | Machine account | The recipient. It must be a user, because notifications are searched with `involves:<login>`. |
| `alice`, `bob` | GitHub Apps | Other people commenting, reviewing and merging. GitHub permits only [one free machine account per person](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements), but any number of apps. |

The test repository must be **public**, because merge queues and rulesets on private repositories need a paid plan.

### 1. Machine account

1. In a private browser window, sign up at <https://github.com/signup>, using a `+` alias of your own email, e.g. `you+pr-e2e@example.com`.
2. Verify the email and enable two-factor authentication under **Settings → Password and authentication**.
3. Turn off email notifications under **Settings → Notifications**, as the tests generate a lot of activity.
4. Create a classic token with only the `repo` scope and no expiration at <https://github.com/settings/tokens/new?scopes=repo&description=pr-notify-e2e>.

### 2. Organization, team and repository

As your personal account:

1. Create a free organization at <https://github.com/account/organizations/new?plan=free>.
2. Invite the machine account as a member under **People → Invite member**, and accept the invitation as the machine account.
3. Create a visible team under **Teams → New team** and add the machine account to it.
4. Create a **public** repository in the organization with **Add a README** ticked, so that `main` exists.
5. Under the repository's **Settings → Collaborators and teams**, give the team the **Write** role.

### 3. GitHub Apps

Repeat for `alice` and `bob` under **Organization settings → Developer settings → GitHub Apps → New GitHub App**:

| Field | Value |
| --- | --- |
| Name | Globally unique, e.g. `<org>-alice` |
| Homepage URL | The test repository's URL |
| Webhook → Active | Unticked |
| Repository permissions | Contents, Issues and Pull requests: **Read and write** |
| Where can this app be installed? | Only on this account |

1. Click **Create GitHub App** and note the **App ID**.
2. Under **Private keys**, generate a key and store it as `~/.config/pr-notify/e2e-<alice|bob>.pem` with `chmod 600`.
3. Under **Install App**, install it on the organization for the test repository only.

### 4. Local configuration

Create `~/.config/pr-notify/e2e.env` with `chmod 600`, entering the token with an editor to keep it out of the shell history:

```bash
E2E_ORG=<organization>
E2E_REPO=<repository>
E2E_TEAM=<team slug>
E2E_ME_LOGIN=<machine account login>
E2E_TOKEN_ME='<classic token>'
E2E_ALICE_APP_ID=<app id>
E2E_ALICE_PRIVATE_KEY_FILE=$HOME/.config/pr-notify/e2e-alice.pem
E2E_BOB_APP_ID=<app id>
E2E_BOB_PRIVATE_KEY_FILE=$HOME/.config/pr-notify/e2e-bob.pem
```

### 5. Repository bootstrap

As your personal account, logged in with `gh auth login`, run the idempotent bootstrap once.
It creates the `e2e` label, the unprotected `e2e-direct` branch, the merge queue ruleset on `main`, and the test repository's workflows.

```bash
set -a; . ~/.config/pr-notify/e2e.env; set +a
uv run scripts/e2e_bootstrap.py
```

### 6. CI secrets and variables

In this repository:

```bash
gh variable set E2E_ORG --body <organization>
gh variable set E2E_REPO --body <repository>
gh variable set E2E_TEAM --body <team slug>
gh variable set E2E_ME_LOGIN --body <machine account login>
gh variable set E2E_ALICE_APP_ID --body <app id>
gh variable set E2E_BOB_APP_ID --body <app id>
gh secret set E2E_TOKEN_ME
gh secret set E2E_ALICE_PRIVATE_KEY < ~/.config/pr-notify/e2e-alice.pem
gh secret set E2E_BOB_PRIVATE_KEY < ~/.config/pr-notify/e2e-bob.pem
```

Pull requests from forks and Dependabot receive no secrets, so the end-to-end job skips itself there.

### Running locally

```bash
set -a; . ~/.config/pr-notify/e2e.env; set +a
uv run pytest -m e2e
```

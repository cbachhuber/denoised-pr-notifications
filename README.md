# denoised-teams-notifications

Sends Microsoft Teams notifications for GitHub pull request activity that actually involves me, and nothing else.

Specifically: every comment on a thread I am participating in, every review on a pull request I authored, and every merge of a pull request I am involved in.
Crucially *not*: the pull requests where a team I belong to was auto-requested as a reviewer via `CODEOWNERS`.

## Why this exists

GitHub's own notification stream cannot express "threads I am actually engaged with".

Every notification thread carries a `reason` (`author`, `comment`, `review_requested`, `team_mention`, …).
Because our repositories use teams as code owners, a large share of threads arrive with `reason: review_requested`, most of which are for pull requests nobody expects me to look at.

The obvious fix — filter those out — does not work, because **the reason is sticky**.
Once a thread is tagged `review_requested`, it keeps that tag forever, even after I review the pull request and start a discussion on it.
So `review_requested` cannot be distinguished from "team was pinged and I engaged deeply", and no filter over the notification stream can separate signal from noise.

This tool therefore ignores notifications entirely and reconstructs participation from pull request data instead.

## How it works

Every 15 minutes a scheduled workflow runs one GraphQL search:

```text
is:pr involves:@me updated:>{watermark} sort:updated-desc
```

`involves:` is an OR over **author, commenter, assignee and mentions** — and deliberately *not* review-requested.
That single predicate is what removes the `CODEOWNERS` noise structurally rather than by filtering:

| Situation | Matches `involves:@me`? |
| --- | --- |
| My team is auto-requested for review, I ignore it | No — silent |
| …and then I comment on it | Yes, from then on |
| Someone reviews a pull request I opened | Yes |
| A pull request I am involved in is merged | Yes |

For each matching pull request the script collects comments, review verdicts, inline review comments and the merge, keeps only those newer than the watermark, drops anything authored by me or by a bot, and posts each one as an Adaptive Card.

## Setup

1. **Create the Teams webhook.**
   In Teams open the **Workflows** app and create from the template *Post to a channel when a webhook request is received*, targeting a channel, then copy the generated URL.
   This must be a Workflows webhook — classic Office 365 connector webhooks were switched off in May 2026.

   Target a **channel**, not a private chat.
   Posting to the self-chat fails with `Call made for a thread which is not a ChatThread`, because Graph will not let a bot write there.

2. **Clone the flow to drop the attribution footer.**
   Cards sent by a flow created from a template carry a `<Name> used a Workflow template to send this card. Get template` line.
   It is appended by Power Automate and is not part of the payload, so it cannot be removed from the card JSON — but a copied flow has no originating template and therefore no footer.

   At [make.powerautomate.com](https://make.powerautomate.com/): **My flows** → open the flow → **Save As** → name it → **Save**.
   Then reopen the copy from **My flows** and click **Turn On**; copies are created disabled.
   Turn the original off so it cannot double-post.

   The copy has a **different webhook URL**, so redo step 4 with it.

3. **Create a personal access token.**
   GitHub has no API for creating tokens, so this step is browser-only:

   ```text
   https://github.com/settings/tokens/new?scopes=repo&description=denoised-teams-notifications
   ```

   Use a **classic** token with only the `repo` scope, and set *No expiration* if enterprise policy allows it.
   Fine-grained tokens are capped at 366 days and need org approval, so they cost more maintenance here.

4. **Store both secrets.**

   ```bash
   gh secret set TEAMS_WEBHOOK_URL --repo cbachhuber/denoised-teams-notifications
   gh secret set PR_NOTIFY_TOKEN   --repo cbachhuber/denoised-teams-notifications
   ```

5. **Trigger the first run** from the Actions tab or with `gh workflow run pr-teams-notify.yml`.
   A cold start looks back one hour, so expect a small burst.

6. **Keep the repository private.**
   Scheduled workflows are auto-disabled after 60 days of inactivity in *public* repositories only, and this repository will never receive commits.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GH_TOKEN` | yes | — | Classic PAT with `repo` scope |
| `TEAMS_WEBHOOK_URL` | yes | — | Teams Workflows webhook |
| `GITHUB_GRAPHQL_URL` | no | `https://api.github.com/graphql` | API endpoint |
| `TOKEN_SETTINGS_URL` | no | `https://github.com/settings/tokens` | Deep link in the failure alert |
| `IGNORED_AUTHORS` | no | `github-actions` | Comma-separated logins to suppress |
| `STATE_FILE` | no | `.state/watermark` | Where the watermark is kept |

Add `copilot-pull-request-reviewer` to `IGNORED_AUTHORS` if automated review comments become noise.

## Operating notes

**Watermark.**
The run start time is written to `.state/watermark` and carried between runs by `actions/cache`.
Cache keys are immutable, so each run writes a new key and `restore-keys` picks the most recent.
The watermark only advances on success, so a failed run produces a delayed notification rather than a lost one.

**Token expiry.**
A dead token in a repository nobody opens would otherwise fail silently, so a `401`/`403` posts a "PR notifier is broken" card with a renewal link, and the run still exits non-zero — which also triggers GitHub's own workflow-failure email to whoever last edited the cron.

**Delivery is fire-and-forget.**
A Workflows webhook returns `202 Accepted` and runs the flow asynchronously, so a 2xx means "queued", never "delivered".
If the flow itself fails — a wrong recipient, an expired connection — the notifier cannot tell, and the watermark advances anyway, so those cards are lost.
When that happens, fix the flow and then replay the window:

```bash
gh workflow run pr-teams-notify.yml -f since=2026-09-08T14:00:00+00:00
```

The flow's own run history in Teams (**Workflows → My flows → run history**) is the only authoritative record of delivery.
A Graph error of `Call made for a thread which is not a ChatThread` there means the *Post card in a chat or channel* action is aimed at something that is not a postable chat — typically the self-chat or a meeting chat.
Set **Post in** to *Chat with Flow bot* with yourself as recipient.

**Schedule drift.**
GitHub cron is best-effort and is delayed or dropped under load, especially on the hour.
The watermark makes this harmless.

**Diagnosing a suspiciously quiet feed.**
Each run logs the window it covered:

```text
since=2026-09-08T09:30:00+00:00 pull_requests=2 notifications=0
```

Across consecutive runs, `since` should equal the previous run's start time.
If every run instead reports a `since` exactly one hour in the past, the cache is not restoring and each run is cold-starting, which silently drops everything older than an hour.
Genuinely quiet periods and a broken watermark both show `notifications=0`, so this line is the only way to tell them apart.

## Development

No third-party runtime dependencies; `uv` supplies the interpreter via PEP 723 inline metadata.

```bash
uv run --with pytest pytest pr_notify_test.py   # unit tests
uv run pr_notify.py                             # needs GH_TOKEN and TEAMS_WEBHOOK_URL
```

## Alternatives evaluated

| Approach | Why it was rejected |
| --- | --- |
| **GitHub for Teams app**, `@GitHub subscribe owner/repo pulls comments reviews` | Subscriptions are repo-scoped, not participation-scoped, so it delivers everyone's activity on the whole repository. It is also a GHEC/GHES integration and does not cover `my_org.ghe.com` data residency, which is where most of our pull requests live. |
| **Email notifications filtered on the `Cc` reason address** (`comment@noreply.github.com` and friends) | Tested and disproven. A thread that arrives as `review_requested` keeps that reason after reviewing it and opening a discussion thread, so the wanted and unwanted mail is indistinguishable. |
| **Notifications REST API**, `GET /notifications?participating=true` | Exposes the same sticky `reason` field, so it fails for exactly the same reason. The participating stream also includes team mentions by definition. |
| **Thread subscription endpoint** as a participation test | GitHub creates a subscription for team mentions and review requests too, so `subscribed: true` does not imply genuine participation. |
| **Team code review settings** — auto assignment plus *Only notify requested team members* | Does not fix stickiness for the pull requests I *am* assigned. It also only reduces noise if the team honours round-robin assignment; if someone else picks the review up, the assignee is pinged for work they will not do and the actual reviewer is not notified. Changing team-wide review routing to solve one person's notification problem is the wrong trade. |
| **GitHub App installation token** instead of a PAT, to avoid expiry | Installation tokens authenticate as the app, so `involves:@me` and `viewer` resolve to the bot account. The design requires a user identity. |
| **Classic Teams incoming webhook** (Office 365 connector) | Retired by Microsoft, disabled between 18 and 22 May 2026. Superseded by Power Automate Workflows webhooks. |

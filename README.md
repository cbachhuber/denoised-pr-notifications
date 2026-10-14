# denoised-pr-notifications

Sends notifications for GitHub [pull request activity that actually involves you](#whats-reported), and nothing else.
This is useful to get rid of irrelevant (noisy) notifications in repositories with many contributors.

Notifications are sent to a webhook, currently with MS Teams' message format.

## Why this exists

In big repos where teams, not individuals own code, [GitHub's own notification stream](https://github.com/notifications) cannot express "threads you are actually engaged with".
Typically, one gets a huge number of irrelevant notifications, rendering GitHub's notifications useless.
There seems to be no freely available, effective notification service for the "big repo with teams owning code" scenario, see also [evaluated alternatives](#alternatives-evaluated).
A working alternative that only Slack users have access to are [real-time notifications as part of Slack's scheduled reminders](https://docs.github.com/en/subscriptions-and-notifications/concepts/scheduled-reminders#:~:text=can%20also%20enable-,real%2Dtime,-alerts%20for%20scheduled).

## What's reported

Authorship then decides how much of it is worth hearing about.
Whether someone else's pull request was approved or merged is not your business, even when you reviewed it, so only threads you are genuinely part of survive:

| Event | On a pull request you authored | On someone else's |
| --- | --- | --- |
| Comment in the conversation | Reported | Only what follows your first comment there |
| Inline review comment | Reported | Only what follows your first comment in that thread |
| Review verdict (approved, changes requested) | Reported | Ignored |
| Merge | Reported, even if you enqueued/merged it yourself | Ignored |
| Anything that `@`-mentions you | Reported | Reported |

Copilot's *Pull request overview* review summary is suppressed via `IGNORED_REVIEW_AUTHORS`, while its addressable inline review comments still come through.

## Setup

After setting up [common steps](#common-steps), you can either run this [from your local machine](#running-from-your-local-machine) or [as a github action](#running-from-github-actions).
Github actions run at most every 5 minutes, are more expensive, and require a dedicated github token.
On the other hand, they probably have a higher availability than your local machine.

### Common steps

These steps target MS Teams, but you can set up any service that provides a webhook for sending notifications to.

1. Create a (private) **channel** where the notifications will be sent, not a private chat.
   Posting to the self-chat fails with `Call made for a thread which is not a ChatThread`, because Graph will not let a bot write there.

2. **Create the Teams webhook.**
   In Teams open the **Workflows** app and create from the template *Post to a channel when a webhook request is received*, targeting a channel, then copy the generated URL.
   This must be a Workflows webhook.

3. That's it, you can proceed to [running from GitHub actions](#running-from-github-actions) or [from your local machine](#running-from-your-local-machine).
   To avoid message footer `Foo Bar used a Workflow template to send this card. Get template` on every notification, you may optionally follow [these instructions](#optional-polish-drop-the-attribution-footer) before proceeding.

### Running from your local machine

This needs no personal access token for GitHub, because [run.sh](run.sh) borrows the token from `gh auth token`.
You'll need to be `gh auth login`-ed on your machine, though.
Nothing expires and there is no browser setup step.

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone git@github.com:cbachhuber/denoised-pr-notifications.git ~/denoised-pr-notifications
mkdir -p ~/.config/pr-notify && chmod 700 ~/.config/pr-notify
cat > ~/.config/pr-notify/env <<'EOF'
TEAMS_WEBHOOK_URL='https://...'  # quote the URL to ensure the '&' contained in the string isn't interpreted
# GH_HOST=my_org.ghe.com  # only needed if you're not on github.com
EOF
chmod 600 ~/.config/pr-notify/env

crontab -e   # then append  `* * * * * $HOME/denoised-pr-notifications/run.sh` to run once per minute
```

Cron reports failures to nobody, so `run.sh` posts a card when a run exits non-zero, and the `pr-notify` command throttles its own alerts to one an hour.

### Running from GitHub actions

1. **Fork this repo**
   Such that you have your own repo, where your secrets and scheduled workflow can live

2. **Create a personal access token.**
   GitHub has no API for creating tokens, so this step is browser-only:

   ```text
   https://github.com/settings/tokens/new?scopes=repo&description=denoised-pr-notifications
   ```

   On GitHub Enterprise, swap the host for your own, e.g. `https://YOUR-HOST.ghe.com/settings/tokens/new?...`.

   Use a **classic** token with only the `repo` scope for minimal maintenance.
   Fine-grained tokens are capped at 366 days and need org approval.

3. **Store both secrets.**

   ```bash
   gh secret set TEAMS_WEBHOOK_URL
   gh secret set PR_NOTIFY_TOKEN
   ```

4. **Trigger the first run** from the Actions tab or with `gh workflow run denoised-pr-notifications.yml`.
   A cold start looks back one hour, so expect a small burst of notifications.

5. **Keep the repository private.**
   Scheduled workflows are auto-disabled after 60 days of inactivity in *public* repositories only, and this repository will never receive commits.

### Optional polish: drop the attribution footer

Cards sent by a flow created from a template always carry advertisement `<Name> used a Workflow template to send this card. Get template`.
It is appended by Power Automate and is not part of the payload, so it cannot be removed from the card JSON.
However, a copied flow has no originating template and therefore no footer.

1. At [make.powerautomate.com](https://make.powerautomate.com/): **My flows** → open the flow → **Save As** → name it → **Save**.
2. Reopen the copy from **My flows** and click **Turn On**; copies are created disabled.
3. Turn the original off so it cannot double-post.
4. The copy has a **different webhook URL** — update `TEAMS_WEBHOOK_URL` (in your GitHub secret or local env file) to the copy's URL and continue using that.

## Alternatives evaluated

| Approach | Why it was rejected |
| --- | --- |
| [GitHub for Teams app](https://docs.github.com/en/integrations/how-tos/teams/use-github-in-teams) | Subscriptions are repo-scoped, not participation-scoped, so it delivers everyone's activity on the whole repository. It is also a GHEC/GHES integration and does not cover Enterprise Cloud with data residency (`*.ghe.com`), which is where many enterprise pull requests live. |
| GitHub Email notifications filtered on the `Cc` reason address | Tested and disproven. A thread that arrives as `review_requested` keeps that reason after reviewing it and opening a discussion thread, so the wanted and unwanted mail is indistinguishable. |
| **Notifications REST API**, `GET /notifications?participating=true` | Exposes the same sticky `reason` field, so it fails for exactly the same reason. The participating stream also includes team mentions by definition. |
| [Gitify](https://github.com/gitify-app/gitify) | A better renderer for GitHub's notification inbox, not a different source of truth, so it inherits every limitation of the rows above. Its own tracking issue [#890](https://github.com/gitify-app/gitify/issues/890) records that there is no GraphQL query for notifications at all, which rules out reconstructing participation from `involves:@me` the way this tool does. Filter naming is shaped by multi-forge support rather than GitHub's model, so options such as *Review request type: Direct* match nothing on GitHub. |
| Team code review settings — auto assignment plus *Only notify requested team members* | Does not fix stickiness for the pull requests you *are* assigned. It also only reduces noise if the team honours round-robin assignment; if someone else picks the review up, the assignee is pinged for work they will not do and the actual reviewer is not notified. Changing team-wide review routing to solve one person's notification problem is the wrong trade. |
| [Slack scheduled reminders](https://docs.github.com/en/subscriptions-and-notifications/concepts/scheduled-reminders) | only available to organizations using Slack |

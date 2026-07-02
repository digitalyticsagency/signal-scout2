# Signal — autonomous weekly scout

Runs your speaking-opportunity scan automatically every week, with no tab
open and no computer required — GitHub's servers do it. New opportunities
get posted to Slack. Nothing runs unless GitHub Actions fires the schedule,
so there's no ongoing bill beyond your Anthropic API usage (web search + a
few thousand tokens per run — a few cents a week).

## What you need to set up (one-time, ~10 minutes)

### 1. A GitHub account and this repo
1. Create a free account at github.com if you don't have one.
2. Create a **new private repository** (e.g. `signal-scout`).
3. Upload all the files in this folder to it (drag-and-drop on the GitHub
   web UI works fine, or `git push` if you're comfortable with git).

### 2. Your topics
Copy `config.example.json` to `config.json` and edit the `topics` array and
`region` to match what you want scanned. `region` should be one of:
`"Australia"`, `"the USA"`, or `"Both Australia and the USA"`.

### 3. An Anthropic API key
1. Go to https://console.anthropic.com and create a key.
2. Add credit to the account (web search + Sonnet 5 calls cost a small
   fraction of a cent per run; budget a couple of dollars for months of
   runs).

### 4. A Slack webhook
1. Go to https://api.slack.com/apps → **Create New App** → *From scratch*.
2. Pick a workspace, then under **Incoming Webhooks**, toggle it on and
   **Add New Webhook to Workspace**, choosing the channel you want alerts
   in (e.g. a private `#signal` channel just for you).
3. Copy the webhook URL (starts with `https://hooks.slack.com/services/...`).

### 5. Add both as GitHub Secrets
In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of these:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `SLACK_WEBHOOK_URL` | the Slack webhook URL from step 4 |

### 6. Test it
Go to the **Actions** tab in your repo → **Weekly Signal scan** →
**Run workflow** (this is the `workflow_dispatch` trigger, for manual runs
any time). The first run just saves a baseline and sends no notifications
— that's expected, so you're not pinged for every result you'd have seen
anyway. Run it a second time (or wait for real new listings) to see an
actual notification.

## How it behaves week to week

- Every Monday ~07:00 UTC (edit the `cron` line in
  `.github/workflows/weekly-scan.yml` to change the day/time — see
  https://crontab.guru if you want a different schedule).
- It searches, compares results against `seen_ids.json` (which the workflow
  commits back to your repo after each run — that's the "memory" between
  runs), and only posts to Slack what's genuinely new.
- If a run finds nothing new, you get no notification at all that week —
  by design, so this doesn't turn into noise.

## Privacy note

This lives in *your* GitHub repo with *your* API keys — nobody at
Anthropic or elsewhere sees your topics or results. Keep the repo
**private** since the config file describes what you're pitching for.
Secrets you add under *Settings → Secrets* are encrypted and never shown
in logs.

## Adjusting later

- Change topics/region any time by editing `config.json` and pushing.
- Change frequency by editing the `cron` line.
- Want it to fire more urgently (e.g. same-day CFP deadlines)? Increase
  frequency, but web search costs scale with runs — weekly is a sensible
  default for a speaking-opportunity feed.

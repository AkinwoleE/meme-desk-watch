# meme-desk-watch

Read-only alerts for Plankton's Solana meme-desk. This repository holds a sanitized snapshot (`watch.json`) and a GitHub Actions workflow that compares it to public prices. **Nothing in this repo signs, sends, or builds a transaction. Never put a wallet key, seed, `.env`, RPC key, or GitHub PAT in this repository or in Actions secrets other than the two webhook secrets below.**

## What runs where

| Where | What | Cadence |
| --- | --- | --- |
| The box (`/workspace/meme-desk`) | `export-watch.mjs` reads GC thresholds, open positions, the last-reported-exit marker, the latest live journal sell, and the monitor heartbeat. `github-sync.sh` writes `watch.json` and pushes it here. | A nohup supervisor (`./github-sync.sh start`), same pattern as the trading monitor. Every ~10 minutes, and whenever the snapshot changes. The box has no `crontab`. |
| This repo, GitHub Actions | `.github/workflows/desk-watch.yml` runs `src/check.mjs`. It fetches public DexScreener (GC/META pool) and DexScreener/GeckoTerminal (open Solana positions) prices. | `schedule: '*/10 * * * *'` (GitHub cron is often 5–20 minutes late and can skip) **and** `workflow_dispatch` so an external cron can fire it on time. |

Exits themselves still belong to the always-on box monitor. This workflow only **wakes an agent** when something already looks wrong.

## Alerts

The workflow POSTs JSON `{ "type", "details" }` and nothing else, and only when a **new** incident appears. Repeated runs of the same incident are suppressed via `.alert-state.json` (committed when it changes). After the condition clears, a later occurrence can alert again.

| `type` | When |
| --- | --- |
| `gc_trigger` | GC market cap on the main **GC/META** DexScreener pool is at or below `nextTriggerMcap`, and the $100 dump-buy ladder still has room (`remaining > 0`). |
| `missed_stop` | An open position's live price is at or below `entryPrice * stopMultiple` (the box monitor should have sold). |
| `unreported_sell` | The latest live journal sell timestamp is newer than `lastReportedExitAt`. |
| `stale_heartbeat` | `monitorHeartbeatAt` in `watch.json` is older than 30 minutes (box suspended, sync or monitor down). Brief pauses under 30 minutes are ignored. |

If `DESK_WEBHOOK_URL` or `DESK_WEBHOOK_KEY` is not set, the job **logs the alert and exits successfully**. It does not fail the run and it does not POST anywhere else.

## Secrets and variables you still need to set

In the repo **Settings → Secrets and variables → Actions**:

| Name | Kind | Required | Purpose |
| --- | --- | --- | --- |
| `DESK_WEBHOOK_URL` | Secret | Yes, for delivery | Absolute URL that will receive `POST {type, details}`. |
| `DESK_WEBHOOK_KEY` | Secret | Yes, for delivery | Value sent in the auth header. |
| `DESK_WEBHOOK_HEADER` | Variable | No | Header name (and optional scheme) used with the key. **Default: `Authorization: Bearer`**, which sends `Authorization: Bearer <DESK_WEBHOOK_KEY>`. For a custom header, set this to e.g. `X-Api-Key` (the raw key is the header value) or `X-Api-Key: Bearer`. |

Do **not** store `SOLANA_PRIVATE_KEY`, a seed, or an RPC URL here.

## Triggering from an external cron (cron-job.org, etc.)

GitHub's own schedule is a backup. For on-time checks, POST to the `workflow_dispatch` API:

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/AkinwoleE/meme-desk-watch/actions/workflows/desk-watch.yml/dispatches \
  -d '{"ref":"main"}'
```

`$TOKEN` should be a **fine-grained personal access token** with access to **this repository only**, and:

- **Actions: Read and write** (required to dispatch)
- **Contents: Read** (so the workflow can check out `watch.json`)

Do not reuse a classic PAT with broad `repo` or org scopes for this. Create the token at GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens, expire it reasonably, and paste it only into the cron service.

You can also run it by hand:

```bash
gh workflow run desk-watch.yml --repo AkinwoleE/meme-desk-watch
```

## Box-side commands

From `/workspace/meme-desk`:

```bash
./github-sync.sh once      # export + push watch.json if it changed
./github-sync.sh start     # supervisor loop (every 10 min)
./github-sync.sh status
./github-sync.sh stop
```

The supervisor is a nohup loop (PID in `data/github-sync.supervisor.pid`, log in `logs/github-sync.log`). It survives process crashes the same way `monitor.sh` does; after a full box reboot, start it again the same way you start the monitor: `./github-sync.sh start`.

The clone of this repo lives at `/workspace/meme-desk-watch`. The sync script will only ever `git add watch.json`.

## `watch.json` shape

Sanitized, allow-listed fields only: GC contract and thresholds, the GC/META pair address, each open position's `id`, `mint`, entry mcap/price, stop multiple and profile, `lastReportedExitAt`, `latestJournalSellAt`, and `monitorHeartbeatAt`. No balances, signatures, theses, or keys.

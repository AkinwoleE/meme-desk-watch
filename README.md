# meme-desk-watch

Exit monitor for the live Solana memecoin desk. GitHub Actions evaluates the same exit rules as the box (`trader/exits.py`) and sells through the same route (`trader/execution.py`: Jupiter Ultra, then Jupiter Swap API + Jito, then PumpPortal). The box keeps scanning and buying. It does not sell while this runner's heartbeat is fresh.

The runner never buys. It never sells an excluded mint (including CJ `F4vEFNoH9H2oLYRabaQQ2Z6jAAi2AhRVdTdVH6Nn2J1q`) and never sells a token that is not an open position this system bought. Sell size comes from the on-chain balance, read again immediately before the swap. Slippage is capped at 15% (`sell_max` 1500 bps). The 0.01 SOL fee reserve is preserved because this runner never buys; a sell is not blocked when SOL is already under that reserve (the same as the box), but the run alerts.

## What runs where

| Where | What | Cadence |
| --- | --- | --- |
| The box | Scans and buys. Pushes each new position to the `state` branch. Sells only when this runner's heartbeat is older than 10 minutes and the box is awake. | Buy on the existing scan. Monitor loop still every ~15s, but it only records while GitHub holds the lease. |
| This repo, `.github/workflows/exit-monitor.yml` | Loads `positions.json` from branch `state`, prices (Jupiter, then DexScreener), runs `exits.evaluate`, sells, writes peak / take-profit / closed state back. Also checks the box heartbeat and unexpected outgoing transfers. | One run loops for ~2.5 minutes, checking every ~15s. cron-job.org starts a run every 3 minutes. GitHub `schedule` every 5 minutes is only a backup. |

`concurrency` is `exit-monitor` with `cancel-in-progress: false`. A second trigger waits. GitHub keeps one running job and one pending job, so triggers do not pile up and a run is not cancelled mid-sell.

The old `desk-watch` workflow has been removed. `src/check.mjs` and `watch.json` are the previous read-only watcher; nothing in the exit path reads them.

## 1. Repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret.

| Name | Required | Value |
| --- | --- | --- |
| `SOLANA_TRADING_PRIVATE_KEY` | Yes | Base58 secret key of the trading wallet. Never put this in the repo, in a log, or in cron-job.org. |
| `SOLANA_RPC_URL` | No | HTTPS RPC (Helius or similar). If empty, the public mainnet RPC is used. |
| `TELEGRAM_BOT_TOKEN` | No | From @BotFather. Used only when both Telegram secrets are set. A missing token does not fail the run. |
| `TELEGRAM_CHAT_ID` | No | Chat id. Same rule: both secrets, or neither. |

Alerts are printed in the Actions log and appended to `alerts.jsonl` on the `state` branch. Telegram is sent only when both secrets are set. A missing `SOLANA_TRADING_PRIVATE_KEY` fails the run before any lease heartbeat is written, so the box keeps selling. Alert cases: each sell (symbol, multiple, PnL, tx), a sell that is still failing on attempts 3, 10, and every 40, box heartbeat older than 30 minutes, box heartbeat missing, a health flag on the heartbeat going false, a new box alert carried in the heartbeat, and an outgoing SOL or token transfer that does not touch a known DEX program.

Workflow permissions: Settings → Actions → General → Workflow permissions → **Read and write permissions**. The job uses `github.token` to commit the `state` branch. The trading key is not that token.

## 2. Fine-grained token for cron-job.org

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.

- Resource owner: `AkinwoleE`
- Repository access: **Only select repositories** → `meme-desk-watch`
- Permissions:
  - **Actions: Read and write** (this is what dispatch needs)
  - **Contents: Read and write** (so the same token can also be the box's `GITHUB_STATUS_TOKEN` if you want one token for both)
- No account permissions. No other repositories.

Put this token only in cron-job.org (and, if you reuse it, in the box env). Do not commit it and do not paste it into an Actions secret.

The box can keep its existing `GITHUB_STATUS_TOKEN` if that token already has Contents: Read and write on this repo. It does not need Actions access.

## 3. Enable the workflow

Merge this change to `main` first. `workflow_dispatch` is only offered for a workflow file on the default branch.

1. Actions tab → if GitHub asks, enable Actions for the repository ("I understand my workflows, go ahead and enable them").
2. Open **exit-monitor**. If it shows a disabled banner, enable that workflow.
3. Run it once by hand (Run workflow → branch `main`) after the secrets above are set. The first run creates branch `state` if it does not exist.

## 4. cron-job.org, every 3 minutes

Create a job:

- Method: **POST**
- URL: `https://api.github.com/repos/AkinwoleE/meme-desk-watch/actions/workflows/exit-monitor.yml/dispatches`
- Schedule: every 3 minutes
- Request body (JSON): `{"ref":"main"}`
- Headers:

| Header | Value |
| --- | --- |
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer <the fine-grained token>` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

A successful dispatch returns **204** with an empty body. 401 means the token is wrong or expired. 404 means the workflow file is not on `main` yet, or the token cannot see this private repo. 422 means `ref` is not a branch GitHub can run.

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/AkinwoleE/meme-desk-watch/actions/workflows/exit-monitor.yml/dispatches \
  -d '{"ref":"main"}'
```

GitHub's own `schedule` (`*/5 * * * *` in the workflow) keeps running as a backup when cron-job.org is down. It is often late. Do not rely on it as the only trigger.

## 5. Box-side apply

The box is not a checkout of this repo. From a copy of this repo (or the `box/` directory), on the machine, with the trader source as the current directory (the folder that contains `monitor.py` and `trader/`):

```bash
cp /path/to/meme-desk-watch/trader/lease.py trader/lease.py
cp /path/to/meme-desk-watch/trader/mergepos.py trader/mergepos.py
cp /path/to/meme-desk-watch/box/state_sync.py state_sync.py
patch -p1 < /path/to/meme-desk-watch/box/monitor.patch
patch -p1 < /path/to/meme-desk-watch/box/scan.patch
```

`patch` applies against the monitor and scan that were current when this was built. If either patch rejects, do not force it: the lease hooks are marked in `monitor.py` in this repo (`lease_execute`, the `execute` flag on `act_on`) and the scan hook is the block in `box/scan.patch`. Copy those by hand onto your copies.

Env for the trader processes (the same place `GITHUB_STATUS_TOKEN` already lives, sourced before `start.sh`):

```bash
# existing token is enough when it has Contents: Read and write on AkinwoleE/meme-desk-watch
GITHUB_STATUS_TOKEN=...
GITHUB_STATUS_REPO=AkinwoleE/meme-desk-watch
# optional rename if you would rather not share the status token
# GITHUB_STATE_TOKEN=...
```

Restart the monitor (and the scan scheduler) so they load the patch. The next live buy calls `state_sync.push_position` and `state_sync.push_rules`. The monitor also pushes an open position the branch does not have yet, so a failed push is retried on the next 15s tick.

### Lease

Shared file: `exit_lease.json` on branch `state`.

- Runner heartbeat younger than 6 minutes: the box **does not sell**. It still updates its local peak and writes its own heartbeat.
- Runner heartbeat between 6 and 10 minutes: the box still does not sell (this gap stops the two sides flipping every cron miss).
- Runner heartbeat older than 10 minutes and the box monitor is in its loop: the box sells, using the same `act_on` path as before, and pushes the book back.
- No GitHub token on the box: the box sells, as it does today.

Both sides size a sell from the live balance and re-read that balance before sending. A full exit that already landed comes back as "no balance" and the position is closed instead of being sold twice. A sell claim on the lease (`sell_claims`, 90s) makes the other side skip that mint for one tick.

### State branch files

| File | Who writes it |
| --- | --- |
| `positions.json` | Box on buy (and on failover sells). Runner after every exit tick (peak, `tp_done`, fills, closed). |
| `exit_lease.json` | Both. Each side updates only its own heartbeat; a merge keeps the other side. |
| `rules.json` | Box, on buy. Excluded mints are a union, so a mint excluded on either side stays excluded. |
| `watch_state.json` | Runner. Last signature cursor and which heartbeat alerts already fired. |
| `alerts.jsonl` | Runner. One JSON line per alert. Written even when Telegram is not configured. |

The box heartbeat the runner watches is unchanged: branch `status`, file `status/heartbeat.json`, field `unix`. Stale means older than 30 minutes.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

CI is `.github/workflows/ci.yml`. It does not sell. `exit-monitor.yml` does not run on push or pull request.

## Not covered by tests

Live signing against Jupiter, Jito, and PumpPortal; a real RPC; Telegram delivery; cron-job.org; creating the `state` branch on GitHub; applying the patch to a `monitor.py` that has drifted from the copy these patches were made from. Those paths are mocked or left for the first manual workflow run. The sell route order, the 15% cap, and the on-chain sizing are the existing unit tests, still mocked.

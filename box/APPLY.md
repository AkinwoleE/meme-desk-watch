# Apply on the box

The trading box is not a git checkout of meme-desk-watch. Copy three files in and apply two patches. Run this from the trader source root (the directory that contains `monitor.py`, `scan.py`, and `trader/`).

```bash
cp /path/to/meme-desk-watch/trader/lease.py trader/lease.py
cp /path/to/meme-desk-watch/trader/mergepos.py trader/mergepos.py
cp /path/to/meme-desk-watch/box/state_sync.py ./state_sync.py
patch -p1 --dry-run < /path/to/meme-desk-watch/box/monitor.patch
patch -p1 --dry-run < /path/to/meme-desk-watch/box/scan.patch
patch -p1 < /path/to/meme-desk-watch/box/monitor.patch
patch -p1 < /path/to/meme-desk-watch/box/scan.patch
```

Dry-run first. If a hunk fails, the box copy of that file has drifted. Do not use `patch --force`. Port the hunks by hand from this repo's `monitor.py` (`_sync`, `lease_execute`, the `execute` argument of `act_on`, and the remote-merge / push at the start and end of `tick`) and from `box/scan.patch` (push the position after the on-chain qty correction).

Env (same file that already exports `GITHUB_STATUS_TOKEN` for `status_push.py`):

```bash
GITHUB_STATUS_TOKEN=...          # fine-grained PAT, this repo only, Contents: Read and write
GITHUB_STATUS_REPO=AkinwoleE/meme-desk-watch
# GITHUB_STATE_TOKEN=...         # optional; used instead of GITHUB_STATUS_TOKEN when set
```

Restart the monitor and the scheduler. The next live buy writes `positions.json` and `rules.json` on branch `state`. While `exit_lease.json` shows a GitHub heartbeat younger than 6 minutes, `monitor.py` logs the exit and does not sell. After the GitHub heartbeat is older than 10 minutes, the box sells again and pushes the book.

No token configured: `state_sync` is a no-op and the box keeps selling. That is the fallback if you have not set the token yet.

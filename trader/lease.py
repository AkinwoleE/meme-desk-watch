"""Who is allowed to sell right now.

The GitHub Actions exit runner is the owner while its heartbeat is fresh.
The box only takes exits over once that heartbeat has been stale long enough
and the box itself is awake. The band between those two ages is deliberate:
the box must not sell the moment the heartbeat crosses 6 minutes, or a runner
that is simply between cron ticks would race the box.
"""

GITHUB_FRESH_S = 6 * 60
GITHUB_STALE_S = 10 * 60
BOX_AWAKE_S = 2 * 60
CLAIM_TTL_S = 90


def _age(stamp, now_unix):
    if not isinstance(stamp, (int, float)):
        return float("inf")
    return now_unix - stamp


def decide_owner(lease, now_unix, box_awake=None):
    """Return 'github', 'box', or 'none'.

    box_awake=True means the caller is the box and is inside its monitor loop,
    so this decision does not depend on the heartbeat the box has not written yet.
    box_awake=False means the box is known to be down.
    box_awake=None reads box_heartbeat_unix from the lease.
    """
    lease = lease or {}
    gh_age = _age(lease.get("github_heartbeat_unix"), now_unix)
    if box_awake is True:
        box_age = 0
    elif box_awake is False:
        box_age = float("inf")
    else:
        box_age = _age(lease.get("box_heartbeat_unix"), now_unix)
    if gh_age < GITHUB_FRESH_S:
        return "github"
    if gh_age > GITHUB_STALE_S and box_age <= BOX_AWAKE_S:
        return "box"
    if gh_age <= GITHUB_STALE_S:
        return "github"
    return "none"


def box_may_sell(lease, now_unix, configured=True, box_awake=True):
    """Box sells only when it has taken the lease. Unconfigured (no token) keeps today's behavior."""
    if not configured:
        return True
    return decide_owner(lease, now_unix, box_awake=box_awake) == "box"


def github_may_sell(lease, now_unix):
    return decide_owner(lease, now_unix) == "github"


def stamp_github(lease, now_unix, iso):
    out = dict(lease or {})
    out["github_heartbeat_unix"] = int(now_unix)
    out["github_heartbeat_iso"] = iso
    out["owner"] = decide_owner(out, now_unix)
    return out


def stamp_box(lease, now_unix, iso):
    out = dict(lease or {})
    out["box_heartbeat_unix"] = int(now_unix)
    out["box_heartbeat_iso"] = iso
    out["owner"] = decide_owner(out, now_unix, box_awake=True)
    return out


def merge_lease(a, b):
    """Keep the newer heartbeat from each side. A writer must not erase the other side."""
    a, b = a or {}, b or {}
    out = {}
    for unix_key in ("github_heartbeat_unix", "box_heartbeat_unix"):
        iso_key = unix_key.replace("_unix", "_iso")
        ua, ub = a.get(unix_key), b.get(unix_key)
        src = b if (ub or 0) >= (ua or 0) else a
        if src.get(unix_key) is not None:
            out[unix_key] = src[unix_key]
            if src.get(iso_key):
                out[iso_key] = src[iso_key]
    claims = {}
    for src in (a, b):
        for mint, claim in (src.get("sell_claims") or {}).items():
            prev = claims.get(mint)
            if prev is None or (claim or {}).get("unix", 0) >= prev.get("unix", 0):
                claims[mint] = claim
    if claims:
        out["sell_claims"] = claims
    # Owner is informational. Recompute from the merged stamps when we can.
    if out.get("github_heartbeat_unix") is not None or out.get("box_heartbeat_unix") is not None:
        now_guess = max(
            out.get("github_heartbeat_unix") or 0,
            out.get("box_heartbeat_unix") or 0,
        )
        out["owner"] = decide_owner(out, now_guess)
    return out


def claim_sell(lease, owner, mint, now_unix, ttl=CLAIM_TTL_S):
    """Return (lease, ok). ok is False when the other side claimed this mint inside `ttl`."""
    out = dict(lease or {})
    claims = {}
    for m, c in (out.get("sell_claims") or {}).items():
        if now_unix - (c or {}).get("unix", 0) < ttl:
            claims[m] = c
    cur = claims.get(mint)
    if cur and cur.get("owner") != owner:
        out["sell_claims"] = claims
        return out, False
    claims[mint] = {"owner": owner, "unix": int(now_unix)}
    out["sell_claims"] = claims
    return out, True


def merge_rules(repo_rules, state_rules):
    """Excluded mints are the union. A mint excluded on either side is never traded."""
    out = dict(repo_rules or {})
    if not state_rules:
        out["excluded_mints"] = dict(out.get("excluded_mints") or {})
        return out
    excluded = dict((repo_rules or {}).get("excluded_mints") or {})
    excluded.update(state_rules.get("excluded_mints") or {})
    out["excluded_mints"] = excluded
    for key in ("slippage_bps", "min_sol_reserve", "exits", "monitor_interval_s"):
        if key in state_rules:
            out[key] = state_rules[key]
    return out

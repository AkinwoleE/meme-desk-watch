"""Heartbeat and outgoing-transfer checks, folded in from github-watchdog/scripts/watch.py.

Read-only against the chain: this module never signs. Our own sells go through Jupiter,
Pump.fun, Raydium, Meteora or Orca, so a transfer that also touches one of those programs
is not alerted. A plain SOL or token send that touches none of them is.
"""
import json

# Same set the prepared watchdog used.
KNOWN_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUPSjgjMFjU4453KMgxhqVmzep6W352bQpE4RsNqXAx",  # Jupiter Ultra/order engine
    "61DFfeTKM7trxYcPQCM78bJ794ddZprZpAwAnLiwTpYH",  # Jupiter order engine
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora pools
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora DBC
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
}
NOISE_DEST = set()
HEALTH_FLAGS = ("monitor_ok", "guard_ok", "scheduler_ok")


def outgoing_transfers(tx, wallet, noise_dest=None):
    """Outgoing SOL or token movements from `wallet` in a tx that does not touch a known DEX program.

    Returns (transfers, program_ids). transfers is empty when a known DEX program is present.
    """
    noise = NOISE_DEST if noise_dest is None else noise_dest
    msg = tx["transaction"]["message"]
    meta = tx["meta"]
    ixs = list(msg.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        ixs.extend(group.get("instructions") or [])
    programs = {i.get("programId") for i in ixs if i.get("programId")}
    if programs & KNOWN_PROGRAMS:
        return [], programs
    out = []
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
    for idx, before in pre.items():
        if before.get("owner") != wallet:
            continue
        a0 = float((before.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        after = post.get(idx) or {}
        a1 = float((after.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        if a1 < a0:
            out.append({"asset": before.get("mint"), "amount": a0 - a1})
    for ix in ixs:
        parsed = ix.get("parsed") or {}
        if isinstance(parsed, dict) and parsed.get("type") == "transfer" and ix.get("program") == "system":
            info = parsed.get("info") or {}
            dest = info.get("destination")
            if info.get("source") == wallet and dest not in noise and dest != wallet:
                out.append({"asset": "SOL", "amount": info.get("lamports", 0) / 1e9, "to": dest})
    return out, programs


def check_chain(rpc_fn, wallet, state):
    """One page of new signatures. The first sighting only sets a baseline (no alerts)."""
    alerts = []
    st = dict(state or {})
    params = {"limit": 50}
    if st.get("last_sig"):
        params["until"] = st["last_sig"]
    sigs = rpc_fn("getSignaturesForAddress", [wallet, params]) or []
    if not st.get("last_sig"):
        if sigs:
            st["last_sig"] = sigs[0]["signature"]
        return alerts, st
    for sig in reversed(sigs):
        if sig.get("err"):
            continue
        tx = rpc_fn("getTransaction", [sig["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if not tx:
            continue
        moved, _programs = outgoing_transfers(tx, wallet)
        if moved:
            alerts.append(
                "OUTGOING transfer from trading wallet (no DEX program) "
                + json.dumps(moved)
                + f" https://solscan.io/tx/{sig['signature']}"
            )
    if sigs:
        st["last_sig"] = sigs[0]["signature"]
    return alerts, st


def check_heartbeat(hb, state, now_unix, max_age_min=30):
    """Stale box heartbeat (> max_age_min), health flags, and new alerts carried in the heartbeat."""
    alerts = []
    st = dict(state or {})
    if not hb or hb.get("unix") is None:
        if not st.get("missing_alerted"):
            alerts.append("box heartbeat file missing")
            st["missing_alerted"] = True
        return alerts, st
    st["missing_alerted"] = False
    age_min = (now_unix - hb["unix"]) / 60.0
    stale = age_min > max_age_min
    if stale and not st.get("stale_alerted"):
        alerts.append(f"box heartbeat is {age_min:.0f} min old (box frozen or down). Last: {hb.get('ts')}")
        st["stale_alerted"] = True
    if not stale and st.get("stale_alerted"):
        alerts.append(f"box heartbeat back ({hb.get('ts')})")
        st["stale_alerted"] = False
    if not stale:
        already = set(st.get("down_flags") or [])
        now_down = set()
        for flag in HEALTH_FLAGS:
            if hb.get(flag) is False:
                now_down.add(flag)
                if flag not in already:
                    alerts.append(f"{flag} false on box ({hb.get('ts')})")
        st["down_flags"] = sorted(now_down)
    seen = list(st.get("seen_alerts") or [])
    seen_set = set(seen)
    for item in hb.get("alerts") or []:
        key = (item.get("ts") or "") + (item.get("msg") or "")
        if key and key not in seen_set:
            alerts.append(f"box alert: {item.get('msg')} @ {item.get('ts')}")
            seen.append(key)
            seen_set.add(key)
    st["seen_alerts"] = seen[-200:]
    return alerts, st

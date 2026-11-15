"""Detect wallet activity not signed by this system; log to logs/foreign_activity.log.
User rule: tokens that arrive by transfer (not via a swap our system signed) are likely scams -> logged once as
'transferred_in, ignored' and auto-added to rules.json excluded_mints. No interaction with them at all (no sells, swaps,
account closes or approvals); this module only reads via RPC."""
import os, json, fcntl, datetime as dt
from .core import rpc, WALLET, DATA, LOGS, ROOT, ts, jload, jsave, OUR_SIGS, WAT, USDC, SOL, TOKEN, TOKEN22, rules
from .history import get_tx, deltas

STATE = os.path.join(DATA, "foreign_state.json")
RULES = os.path.join(ROOT, "rules.json")

def flog(msg, **kw):
    with open(os.path.join(LOGS, "foreign_activity.log"), "a") as f: f.write(json.dumps({"ts": ts(), "msg": msg, **kw}, default=str) + "\n")

def managed_mints():
    from . import journal
    return {p["mint"] for p in journal.load(False).values()}

def exclude_transferred(mint, qty, sig=None, when=None):
    with open(RULES + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        R = json.load(open(RULES)); ex = R.setdefault("excluded_mints", {})
        if mint in ex: return False
        ex[mint] = f"transferred_in, ignored (likely scam; never touch) first seen {ts()}"
        tmp = RULES + ".tmp"; json.dump(R, open(tmp, "w"), indent=1); os.replace(tmp, RULES)
    flog("transferred_in, ignored", mint=mint, qty=qty, sig=sig, block_time=when, action="added to excluded_mints; no interaction")
    return True

def sweep_wallet():
    """Any non-USDC token in the wallet that no position of ours bought -> excluded (logged once)."""
    mine = managed_mints(); ex = rules().get("excluded_mints") or {}
    for prog in (TOKEN, TOKEN22):
        r = rpc("getTokenAccountsByOwner", [WALLET, {"programId": prog}, {"encoding": "jsonParsed"}])
        for a in (r or {}).get("value", []):
            i = a["account"]["data"]["parsed"]["info"]; m = i["mint"]; q = float(i["tokenAmount"]["uiAmount"] or 0)
            if q > 0 and m not in (USDC, SOL) and m not in mine and m not in ex:
                exclude_transferred(m, q)

def check():
    st = jload(STATE, {}); last = st.get("last_sig")
    params = {"limit": 50}
    if last: params["until"] = last
    sigs = rpc("getSignaturesForAddress", [WALLET, params])
    if sigs is None: return
    if not last:
        if sigs: st["last_sig"] = sigs[0]["signature"]; jsave(STATE, st)
        flog("baseline set", last_sig=st.get("last_sig")); return
    ours = jload(OUR_SIGS, {})
    for s in reversed(sigs):
        if s["signature"] in ours: continue
        tx = get_tx(s["signature"]); d = {k: round(v, 9) for k, v in (deltas(tx) if tx else {}).items() if abs(v) > 1e-12}
        when = dt.datetime.fromtimestamp(s["blockTime"], WAT).isoformat() if s.get("blockTime") else None
        flog("FOREIGN tx (not signed by this system)", sig=s["signature"], block_time=when, err=s.get("err"), balance_changes=d)
        # foreign sale of one of OUR positions: book its proceeds (external manual close)
        from . import journal
        with journal.locked():
            bk = journal.load(False); ch = False
            for p in bk.values():
                if d.get(p["mint"], 0) < 0 and not p.get("external_sigs", {}).get(s["signature"]):
                    usd = d.get(USDC, 0) if d.get(USDC, 0) > 0 else 0
                    p.setdefault("external_sigs", {})[s["signature"]] = {"qty": -d[p["mint"]], "usdc": usd, "sol": d.get("SOL_native", 0)}
                    p["realized_usd"] = p.get("realized_usd", 0) + usd; ch = True
                    if p["status"] != "open": p["pnl_usd"] = p["realized_usd"] - p["cost_usd"]; p["pnl_x"] = p["realized_usd"] / p["cost_usd"]
                    flog("external sale of a managed position booked", id=p["id"], symbol=p["symbol"], sig=s["signature"], qty=-d[p["mint"]], usdc=usd)
            if ch: journal.save(bk, False)
        for m, q in d.items():
            if m in ("SOL_native", USDC, SOL) or q <= 0 or m in managed_mints(): continue
            exclude_transferred(m, q, s["signature"], when)  # received outside our swaps -> never touch
    if sigs: st["last_sig"] = sigs[0]["signature"]; jsave(STATE, st)
    sweep_wallet()

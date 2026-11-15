"""Reconstruct entry price/time per mint from on-chain txs (owner token-balance deltas vs USDC/SOL deltas)."""
import json, os, time, datetime as dt
from .core import rpc, DATA, WALLET, USDC, SOL, WAT, jload

CACHE = os.path.join(DATA, "txcache")
def get_tx(sig):
    f = os.path.join(CACHE, sig + ".json")
    if os.path.exists(f): return json.load(open(f))
    r = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    if r: os.makedirs(CACHE, exist_ok=True); json.dump(r, open(f, "w"))
    return r

def deltas(tx, owner=WALLET):
    m = tx["meta"]; d = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in m.get(side) or []:
            if b.get("owner") != owner: continue
            d[b["mint"]] = d.get(b["mint"], 0) + sign * float(b["uiTokenAmount"]["uiAmount"] or 0)
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if owner in keys:
        i = keys.index(owner)
        d["SOL_native"] = (m["postBalances"][i] - m["preBalances"][i]) / 1e9
    return d

def reconstruct(mint, sol_usd_now=None):
    sigs = []
    accs = rpc("getTokenAccountsByOwner", [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    for a in accs.get("value", []):
        sigs += rpc("getSignaturesForAddress", [a["pubkey"], {"limit": 100}]) or []
    fills = []
    for s in sorted(sigs, key=lambda s: s.get("blockTime") or 0):
        tx = get_tx(s["signature"])
        if not tx or tx["meta"].get("err"): continue
        d = deltas(tx); q = d.get(mint, 0)
        if abs(q) < 1e-12: continue
        usdc = d.get(USDC, 0); sol = d.get(SOL, 0) + d.get("SOL_native", 0)
        fills.append({"sig": s["signature"], "time": dt.datetime.fromtimestamp(tx["blockTime"], WAT).isoformat(), "qty": q,
                      "usdc": usdc, "sol": sol})
    buys = [f for f in fills if f["qty"] > 0]
    cost_usd = sum(-f["usdc"] for f in buys if f["usdc"] < 0)
    cost_sol = sum(-f["sol"] for f in buys if f["usdc"] >= 0 and f["sol"] < 0)
    qty = sum(f["qty"] for f in buys)
    return {"fills": fills, "buy_qty": qty, "cost_usdc": cost_usd, "cost_sol": cost_sol,
            "first_buy": buys[0]["time"] if buys else None}

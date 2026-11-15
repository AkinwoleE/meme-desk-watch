"""Order execution. DRY-RUN unless core.is_live() (TRADER_LIVE=1 env AND ./LIVE file containing I_APPROVE_LIVE_TRADING).
Live route order:
  1. Jupiter Ultra (/ultra/v1/order + /execute): Jupiter lands the tx itself with MEV protection (Jupiter Beam / Jito).
  2. Jupiter Swap API (/swap/v1/quote + /swap) with a Jito tip, sent to the Jito block engine (private, MEV-protected), RPC as last resort.
  3. PumpPortal trade-local (bonding-curve coins), sent via Jito.
The private key is read from env only inside _keypair(), never logged."""
import os, json, base64, time, requests
from .core import is_live, rules, log, rpc, USDC, SOL, WALLET, excluded, record_our_sig
from . import data

JUP = "https://lite-api.jup.ag"; JITO = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"
SIZE_CAP = 2.0  # hard-coded: never more than $2 per buy, regardless of rules.json

class NotLive(Exception): pass

def _keypair():
    if not is_live(): raise NotLive("live trading disabled")
    import base58
    from solders.keypair import Keypair
    kp = Keypair.from_bytes(base58.b58decode(os.environ["SOLANA_TRADING_PRIVATE_KEY"]))
    assert str(kp.pubkey()) == WALLET, "key/wallet mismatch"
    return kp

def _sign_b64(tx_b64, kp):
    """Sign our slot only (supports Jupiter Ultra gasless txs where another party pays fees)."""
    from solders.transaction import VersionedTransaction
    from solders.message import to_bytes_versioned
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    n = tx.message.header.num_required_signatures
    keys = list(tx.message.account_keys)[:n]
    if n == 1: return base64.b64encode(bytes(VersionedTransaction(tx.message, [kp]))).decode()
    sigs = list(tx.signatures); sigs[keys.index(kp.pubkey())] = kp.sign_message(to_bytes_versioned(tx.message))
    return base64.b64encode(bytes(VersionedTransaction.populate(tx.message, sigs))).decode()

def balances():
    sol = (rpc("getBalance", [WALLET]) or {}).get("value", 0) / 1e9
    usdc = 0.0
    r = rpc("getTokenAccountsByOwner", [WALLET, {"mint": USDC}, {"encoding": "jsonParsed"}]) or {}
    for a in r.get("value", []): usdc += float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    return {"sol": sol, "usdc": usdc}

def _send_jito(raw_b64):
    r = requests.post(JITO, json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [raw_b64, {"encoding": "base64"}]}, timeout=20)
    return r.json().get("result")

def _confirm(sig, timeout=60):
    """True = landed OK, False = landed with error, None = unknown (may still land)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = (rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {}).get("value", [None])[0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"): return st.get("err") is None
        time.sleep(2)
    return None

class Unknown(Exception): pass

def _our_sigs(signed_b64):
    """All non-empty signatures in a signed tx (ours; for gasless Ultra the fee-payer slot is filled later by Jupiter)."""
    from solders.transaction import VersionedTransaction as _VT
    sigs = [str(x) for x in _VT.from_bytes(base64.b64decode(signed_b64)).signatures]
    return [x for x in sigs if x != "1" * 64 and set(x) != {"1"}]

def _bal(mint):
    if mint in (SOL,): return None
    try: return onchain_raw(mint)
    except Exception: return None

def _recover(attempted, side, inp, out, pre_in, pre_out, wait=45):
    """After a submit error/timeout: did any of our signed txs land? Checks signature statuses and the token balance;
    if the balance moved, finds the landed tx in recent wallet history. Returns the landed signature or None."""
    from .history import get_tx
    txids = [s for s in attempted if s]
    t0 = time.time()
    while time.time() - t0 < wait:
        if txids:
            st = (rpc("getSignatureStatuses", [txids, {"searchTransactionHistory": True}]) or {}).get("value") or []
            for sig, x in zip(txids, st):
                if x and x.get("err") is None and x.get("confirmationStatus") in ("confirmed", "finalized"): return sig
        moved = False
        if side == "buy" and pre_out is not None:
            b = _bal(out); moved = b is not None and b > pre_out
        if side == "sell" and pre_in is not None:
            b = _bal(inp); moved = b is not None and b < pre_in
        if moved:
            mint = out if side == "buy" else inp
            for s in rpc("getSignaturesForAddress", [WALLET, {"limit": 15}]) or []:
                if s.get("err"): continue
                tx = get_tx(s["signature"])
                if not tx: continue
                if set(tx["transaction"]["signatures"]) & set(attempted): return s["signature"]
                from .history import deltas
                d = deltas(tx)
                if (side == "buy" and d.get(mint, 0) > 0) or (side == "sell" and d.get(mint, 0) < 0): return s["signature"]
        time.sleep(3)
    return None

def _landed_fill(route, sig, amount):
    d = actual_deltas(sig) or {}
    return {"route": route, "sig": sig, "in": amount, "out": None, "deltas": d}

def _live_swap(inp, out, amount, slippage_bps, curve_mint=None, side="buy", full_sell=False, dec=6):
    kp = _keypair(); attempted = []
    pre_in, pre_out = _bal(inp), _bal(out)
    # 1. Ultra (Jupiter lands it, MEV-protected)
    try:
        o = requests.get(f"{JUP}/ultra/v1/order", params={"inputMint": inp, "outputMint": out, "amount": int(amount), "taker": WALLET,
                         "slippageBps": int(slippage_bps)}, timeout=20).json()
        if o.get("transaction"):
            signed = _sign_b64(o["transaction"], kp)
            for sg in _our_sigs(signed): record_our_sig(sg, f"ultra {side} {inp[:6]}->{out[:6]}"); attempted.append(sg)
            try:
                ex = requests.post(f"{JUP}/ultra/v1/execute", json={"signedTransaction": signed, "requestId": o["requestId"]}, timeout=60).json()
            except Exception as e:
                log("exec", "ultra submit error; checking chain before fallback", err=str(e)[:200])
                sig = _recover(attempted, side, inp, out, pre_in, pre_out)
                if sig: record_our_sig(sig, f"ultra {side} (recovered)"); log("exec", "ultra tx landed despite error", sig=sig); return _landed_fill("jupiter-ultra (recovered)", sig, amount)
                raise
            if ex.get("status") == "Success":
                if ex.get("signature"): record_our_sig(ex["signature"], f"ultra {side} txid")
                return {"route": "jupiter-ultra", "sig": ex.get("signature"), "in": int(ex.get("inputAmountResult") or amount), "out": int(ex.get("outputAmountResult") or o.get("outAmount"))}
            log("exec", "ultra failed", resp={k: ex.get(k) for k in ("status", "error", "code")})
            if ex.get("signature"): record_our_sig(ex["signature"], f"ultra {side} failed txid")
    except Exception as e: log("exec", "ultra error", err=str(e)[:300])
    # 2. Swap API + Jito tip (txid known before sending)
    try:
        q = data.jup_quote(inp, out, amount, slippage_bps)
        sw = requests.post(f"{JUP}/swap/v1/swap", json={"quoteResponse": q, "userPublicKey": WALLET, "wrapAndUnwrapSol": True,
                           "dynamicComputeUnitLimit": True, "dynamicSlippage": False,
                           "prioritizationFeeLamports": {"jitoTipLamports": 100000}}, timeout=20).json()
        raw = _sign_b64(sw["swapTransaction"], kp)
        txid = _our_sigs(raw)[0]; record_our_sig(txid, f"swap {side}"); attempted.append(txid)
        try: _send_jito(raw)
        except Exception as e: log("exec", "jito send error; checking chain", err=str(e)[:200])
        c = _confirm(txid, timeout=45)
        if c: return {"route": "jupiter-swap+jito", "sig": txid, "in": int(q["inAmount"]), "out": int(q["outAmount"])}
        sig = _recover(attempted, side, inp, out, pre_in, pre_out, wait=15)
        if sig: log("exec", "swap tx landed (recovered)", sig=sig); return _landed_fill("jupiter-swap+jito (recovered)", sig, amount)
        if c is None and side == "buy": raise Unknown(f"swap {txid} unconfirmed; not falling back (avoid double buy)")
    except Unknown: raise
    except Exception as e: log("exec", "swap api error", err=str(e)[:300])
    # 3. PumpPortal (bonding curve, or any pump.fun/PumpSwap coin for sells; pool=auto)
    if curve_mint:
        try:
            body = {"publicKey": WALLET, "action": side, "mint": curve_mint, "denominatedInSol": "true" if side == "buy" else "false",
                    "amount": amount / 1e9 if side == "buy" else ("100%" if full_sell else amount / 10 ** dec), "slippage": slippage_bps / 100, "priorityFee": 0.0005, "pool": "auto"}
            r = requests.post("https://pumpportal.fun/api/trade-local", data=body, timeout=20)
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(r.content)
            raw = base64.b64encode(bytes(VersionedTransaction(tx.message, [kp]))).decode()
            txid = _our_sigs(raw)[0]; record_our_sig(txid, f"pumpportal {side}"); attempted.append(txid)
            try: _send_jito(raw)
            except Exception as e: log("exec", "jito send error; checking chain", err=str(e)[:200])
            c = _confirm(txid, timeout=45)
            if c: return {"route": "pumpportal+jito", "sig": txid, "in": amount, "out": None}
            sig = _recover(attempted, side, inp, out, pre_in, pre_out, wait=15)
            if sig: return _landed_fill("pumpportal+jito (recovered)", sig, amount)
            if c is None and side == "buy": raise Unknown(f"pumpportal {txid} unconfirmed")
        except Unknown: raise
        except Exception as e: log("exec", "pumpportal error", err=str(e)[:300])
    # final check: nothing landed?
    sig = _recover(attempted, side, inp, out, pre_in, pre_out, wait=10) if attempted else None
    if sig: return _landed_fill("recovered", sig, amount)
    raise RuntimeError("all live routes failed")

def buy(t, usd, slippage_bps, paper_quote=None, paper=None):
    """Returns fill dict. Dry-run (or paper=True): simulated from a Jupiter quote, nothing signed.
    The GitHub exit runner sets EXIT_RUNNER=1 and must never buy."""
    if os.environ.get("EXIT_RUNNER") == "1": raise RuntimeError("exit runner never buys")
    if excluded(t["id"]): raise RuntimeError("excluded mint: never buy")
    R = rules(); usd = min(usd, SIZE_CAP, R["max_size_usd"])
    dec = t.get("decimals", 6)
    if paper or not is_live():
        q = paper_quote or data.jup_quote(USDC, t["id"], usd * 1e6, slippage_bps)
        qty = int(q["outAmount"]) / 10 ** dec
        return {"ts": __import__("trader.core", fromlist=["ts"]).ts(), "side": "buy", "usd": usd, "qty": qty, "price": usd / qty, "route": "DRY-RUN", "sig": None}
    b = balances()
    if b["sol"] < R["min_sol_reserve"]: raise RuntimeError(f"SOL {b['sol']:.4f} below fee reserve")
    curve = t["id"] if not t.get("graduatedPool") else None
    if b["usdc"] >= usd:
        r = _live_swap(USDC, t["id"], usd * 1e6, slippage_bps, None)  # no PumpPortal for USDC buys (it takes SOL)
    else:
        solp = data.jup_prices([SOL])[SOL]; lam = usd / solp * 1e9
        if b["sol"] - lam / 1e9 < R["min_sol_reserve"]: raise RuntimeError("SOL fallback would breach reserve")
        r = _live_swap(SOL, t["id"], lam, slippage_bps, curve)
    qty = (r["out"] or 0) / 10 ** dec
    d = actual_deltas(r["sig"]) if r.get("sig") else None
    if d and d.get(t["id"]): qty = d[t["id"]]
    if d and d.get(USDC): usd = -d[USDC]
    from .core import ts
    return {"ts": ts(), "side": "buy", "usd": usd, "qty": qty, "price": usd / qty if qty else None, "route": r["route"], "sig": r["sig"]}

class NoBalance(Exception): pass

def onchain_raw(mint):
    r = rpc("getTokenAccountsByOwner", [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
    if r is None: return None
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in r.get("value", []))

def actual_deltas(sig, tries=8):
    """Read the landed tx and return the owner's balance deltas (actual fill), or None."""
    from .history import get_tx, deltas
    for _ in range(tries):
        tx = get_tx(sig)
        if tx: return deltas(tx)
        time.sleep(2)
    return None

def sell(mint, qty, decimals, price_hint, slippage_bps=None, paper=False, full=False):
    """Sell `qty` (UI units). Live: sized from the ACTUAL on-chain balance; a full exit sells the whole raw balance (no dust).
    Slippage is capped at rules sell_max (15%). Fill is read back from the landed tx."""
    if excluded(mint): raise RuntimeError("excluded mint: never sell")
    R = rules(); slip = min(slippage_bps or R["slippage_bps"]["sell_max"], R["slippage_bps"]["sell_max"])
    from .core import ts
    if paper or not is_live():
        return {"ts": ts(), "side": "sell", "qty": qty, "usd": qty * (price_hint or 0), "price": price_hint, "route": "DRY-RUN", "sig": None}
    bal = onchain_raw(mint)
    if bal is None: raise RuntimeError("balance read failed")
    if bal == 0: raise NoBalance("no on-chain balance")
    want = int(qty * 10 ** decimals)
    full = full or want >= bal * 0.99
    amt = bal if full else min(want, bal)
    r = _live_swap(mint, USDC, amt, slip, mint, side="sell", full_sell=full, dec=decimals)
    d = actual_deltas(r["sig"]) if r.get("sig") else None
    sold = -d.get(mint, 0) if d else amt / 10 ** decimals
    usd = d.get(USDC, 0) if d and USDC in d else (r["out"] or 0) / 1e6
    return {"ts": ts(), "side": "sell", "qty": sold, "usd": usd, "price": usd / sold if sold else None, "route": r["route"], "sig": r["sig"],
            "full": full, "actual_from_chain": bool(d)}

def close_empty_account(mint):
    """Reclaim rent from OUR OWN empty token account after a full exit (managed positions only, balance must be exactly 0)."""
    if excluded(mint) or not is_live(): return None
    from solders.pubkey import Pubkey
    from solders.instruction import Instruction, AccountMeta
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from solders.hash import Hash
    r = rpc("getTokenAccountsByOwner", [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    sigs = []
    kp = _keypair()
    for a in r.get("value", []):
        info = a["account"]["data"]["parsed"]["info"]
        if int(info["tokenAmount"]["amount"]) != 0 or info.get("state") != "initialized": continue
        prog = Pubkey.from_string(a["account"]["owner"]); acct = Pubkey.from_string(a["pubkey"])
        ix = Instruction(prog, bytes([9]), [AccountMeta(acct, False, True), AccountMeta(kp.pubkey(), False, True), AccountMeta(kp.pubkey(), True, False)])
        bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
        tx = VersionedTransaction(MessageV0.try_compile(kp.pubkey(), [ix], [], bh), [kp])
        raw = base64.b64encode(bytes(tx)).decode()
        sig = rpc("sendTransaction", [raw, {"encoding": "base64"}]); record_our_sig(sig, f"close empty ATA {mint[:6]}")
        if sig: sigs.append(sig)
    return sigs

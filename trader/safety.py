"""Hard rejects (section 3) + pre-trade checks (section 1). Returns (ok, reasons, facts)."""
import datetime as dt
from .core import rules, USDC, now, WAT
from . import data

BRANDS = {"bitcoin","btc","ethereum","eth","solana","sol","usdc","usdt","tether","binance","bnb","coinbase","jupiter","jup","raydium",
          "ripple","xrp","doge","dogecoin","shib","cardano","ada","tron","trx","pump","pumpfun","bonk","wif","phantom","metamask","uniswap",
          "chainlink","link","avax","polygon","matic","sui","aptos","ton","pepe","hyperliquid","hype","base","arbitrum","optimism"}

def _age_minutes(t):
    c = t.get("createdAt") or (t.get("firstPool") or {}).get("createdAt")
    if not c: return None
    c = dt.datetime.fromisoformat(c.replace("Z", "+00:00"))
    return (now() - c).total_seconds() / 60

def is_graduated(t):
    return bool(t.get("graduatedPool")) or (t.get("launchpad") is None and (t.get("liquidity") or 0) > 0)

def slippage_bps(t, side="buy"):
    s = rules()["slippage_bps"]
    if side == "sell": return s["sell_max"]
    if not is_graduated(t): return s["curve"]
    liq = t.get("liquidity") or 0
    return s["grad_lt_25k"] if liq < 25000 else s["grad_25k_150k"] if liq <= 150000 else s["grad_gt_150k"]

def roundtrip_test(t, usd=None):
    """Buy-then-sell quote test via Jupiter. Catches honeypots. Returns dict."""
    R = rules(); usd = usd or R["size_usd"]; mint = t["id"]
    q1 = data.jup_quote(USDC, mint, usd * 1e6, slippage_bps(t))
    if not q1 or "outAmount" not in q1: return {"ok": False, "why": "no buy route"}
    impact = float(q1.get("priceImpactPct") or 0) * 100
    q2 = data.jup_quote(mint, USDC, int(q1["outAmount"]), slippage_bps(t, "sell"))
    if not q2 or "outAmount" not in q2: return {"ok": False, "why": "no sell route (honeypot?)", "impact_pct": impact}
    back = int(q2["outAmount"]) / 1e6
    loss = (1 - back / usd) * 100
    return {"ok": impact <= R["max_price_impact_pct"] and loss <= R["max_roundtrip_loss_pct"],
            "impact_pct": round(impact, 2), "roundtrip_loss_pct": round(loss, 2), "buy_quote": q1}

def insider_stats(rc):
    """From RugCheck report: insider wallets count + pct; top10 excluding pools/lockers; largest single; dev pct."""
    if not rc: return None
    pools = {m.get("pubkey") for m in rc.get("markets") or []}
    for m in rc.get("markets") or []:
        for k in ("liquidityA", "liquidityB"):
            if m.get(k): pools.add(m[k])
    lockers = set((rc.get("lockerOwners") or {}).keys()) | set((rc.get("lockers") or {}).keys())
    known = rc.get("knownAccounts") or {}
    def excluded(h):
        o = h.get("owner"); a = h.get("address")
        return o in pools or a in pools or o in lockers or (known.get(o) or {}).get("type") in ("AMM", "LOCKER")
    hs = [h for h in rc.get("topHolders") or [] if not excluded(h)]
    ins = [h for h in rc.get("topHolders") or [] if h.get("insider")]
    supply = (rc.get("token") or {}).get("supply") or 0
    dev_pct = (rc.get("creatorBalance") or 0) / supply * 100 if supply else 0
    nets = rc.get("insiderNetworks") or []
    net_wallets = sum(n.get("size", 0) for n in nets) if nets else len(ins)
    net_pct = sum(h.get("pct", 0) for h in ins)
    if nets:
        sup = supply or 1
        net_pct = max(net_pct, sum((n.get("tokenAmount") or 0) for n in nets) / sup * 100)
    return {"top10_pct": sum(h.get("pct", 0) for h in hs[:10]), "max_single_pct": max([h.get("pct", 0) for h in hs] or [0]),
            "dev_pct": dev_pct, "insider_wallets": net_wallets, "insider_pct": net_pct,
            "risks": [r.get("name") for r in rc.get("risks") or []], "rugged": rc.get("rugged"),
            "holders": rc.get("totalHolders")}

def hard_rejects(t, rc=None, mi=None, x_mentions_ca=None, aged_revival=False, do_quote=True):
    R = rules(); H = R["hard"]; why = []; facts = {}
    a = t.get("audit") or {}
    # mint / freeze authority (RPC is authoritative, Jupiter audit as fallback)
    if mi:
        if mi["mint_authority"]: why.append("mint authority enabled")
        if mi["freeze_authority"]: why.append("freeze authority enabled")
        if mi["dangerous_ext"]: why.append("dangerous Token-2022 ext: " + ",".join(mi["dangerous_ext"]))
    else:
        if a.get("mintAuthorityDisabled") is False: why.append("mint authority enabled")
        if a.get("freezeAuthorityDisabled") is False: why.append("freeze authority enabled")
    if rc and (rc.get("transferFee") or {}).get("pct"): why.append("transfer fee")
    if a.get("isSus"): why.append("Jupiter flags suspicious")
    age = _age_minutes(t); facts["age_min"] = age
    if age is not None:
        if age < H["min_age_min"]: why.append(f"too young ({age:.0f}m)")
        if age > H["max_age_days"] * 1440 and not aged_revival: why.append(f"too old ({age/1440:.1f}d)")
    mcap = t.get("mcap") or t.get("fdv") or 0; facts["mcap"] = mcap
    if mcap < H["min_mcap"]: why.append(f"mcap ${mcap:,.0f} < $8k")
    if mcap > H["max_mcap"]: why.append(f"mcap ${mcap:,.0f} > $50M")
    holders = t.get("holderCount") or 0; facts["holders"] = holders
    if holders < H["min_holders"]: why.append(f"{holders} holders < 75")
    liq = t.get("liquidity") or 0; facts["liq"] = liq
    if is_graduated(t) and liq < H["min_grad_liq"]: why.append(f"graduated liq ${liq:,.0f} < $10k")
    if is_graduated(t) and liq < H["phantom_liq"]: why.append("drained/phantom pool")
    ins = insider_stats(rc); facts["insiders"] = ins
    insider_exception = False
    if ins:
        if ins["top10_pct"] > H["top10_max_pct"]: why.append(f"top10 {ins['top10_pct']:.1f}% > 35%")
        if ins["max_single_pct"] > H["single_wallet_max_pct"]: why.append(f"single wallet {ins['max_single_pct']:.1f}% > 12%")
        if ins["dev_pct"] > H["dev_max_pct"]: why.append(f"dev {ins['dev_pct']:.1f}% > 8%")
        if ins["rugged"]: why.append("RugCheck: rugged")
        if any("bundle" in (r or "").lower() for r in ins["risks"]): why.append("launch-block bundle (RugCheck)")
        E = R["entry"]["insider_exception"]
        if ins["insider_pct"] > H["insider_max_pct"]:
            if E["min_wallets"] <= ins["insider_wallets"] <= E["max_wallets"] and E["min_pct"] <= ins["insider_pct"] <= E["max_pct"]:
                insider_exception = True
            else: why.append(f"insiders {ins['insider_pct']:.1f}% > 15%")
        elif E["min_wallets"] <= ins["insider_wallets"] <= E["max_wallets"] and E["min_pct"] <= ins["insider_pct"] <= E["max_pct"]:
            insider_exception = True  # linked insiders present inside the exception band -> tighter exits
    else:
        if (a.get("topHoldersPercentage") or 0) > H["top10_max_pct"]: why.append(f"top holders {a['topHoldersPercentage']:.1f}% > 35% (Jupiter, incl. pools)")
        if (a.get("devBalancePercentage") or 0) > H["dev_max_pct"]: why.append(f"dev {a['devBalancePercentage']:.1f}% > 8%")
    facts["insider_exception"] = insider_exception
    s1 = t.get("stats1h") or {}; s24 = t.get("stats24h") or {}
    v1 = (s1.get("buyVolume") or 0) + (s1.get("sellVolume") or 0); v24 = (s24.get("buyVolume") or 0) + (s24.get("sellVolume") or 0)
    facts["vol1h"] = v1; facts["vol24h"] = v24
    F = H["fake_mcap"]
    if mcap > F["mcap_over"] and v1 < mcap * F["vol1h_pct_under"] / 100 and holders and mcap / holders > F["mcap_per_holder_over"]:
        why.append("fake market cap")
    name = f"{t.get('name','')} {t.get('symbol','')}".lower()
    if any(w in BRANDS for w in name.replace("$", " ").split()) and mcap and v24 > H["wash"]["vol24_over_mcap_x"] * mcap and not x_mentions_ca:
        why.append("wash volume on crypto-brand name (X CA mentions unverified)")
    if do_quote and not why:
        rt = roundtrip_test(t); facts["roundtrip"] = {k: v for k, v in rt.items() if k != "buy_quote"}
        if not rt["ok"]: why.append(f"pre-trade test failed: {rt.get('why') or ''} impact {rt.get('impact_pct')}% rt-loss {rt.get('roundtrip_loss_pct')}%")
    return (not why), why, facts

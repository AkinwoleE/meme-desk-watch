"""Market data from free public APIs: Jupiter (tokens v2 / price v3 / quote), DexScreener, GeckoTerminal, RugCheck, Solana RPC."""
import base64, struct, time, datetime as dt
from .core import get, rpc, SOL, USDC, TOKEN22, log

JUP = "https://lite-api.jup.ag"
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RC = "https://api.rugcheck.xyz/v1"
PUMP = "https://frontend-api-v3.pump.fun"

# ---------- Jupiter ----------
def jup_token(mint):
    d = get("jupiter", f"{JUP}/tokens/v2/search", {"query": mint})
    if d:
        for t in d:
            if t.get("id") == mint: return t
    return None

def jup_tokens(mints):
    out = {}
    for i in range(0, len(mints), 100):
        d = get("jupiter", f"{JUP}/tokens/v2/search", {"query": ",".join(mints[i:i+100])}) or []
        for t in d: out[t["id"]] = t
    return out

def jup_category(cat, interval="1h", limit=50):
    return get("jupiter", f"{JUP}/tokens/v2/{cat}/{interval}", {"limit": limit}) or []

def jup_recent():
    return get("jupiter", f"{JUP}/tokens/v2/recent") or []

def jup_prices(mints):
    out = {}
    for i in range(0, len(mints), 50):
        d = get("jupiter", f"{JUP}/price/v3", {"ids": ",".join(mints[i:i+50])}) or {}
        for k, v in d.items():
            if v and v.get("usdPrice") is not None: out[k] = float(v["usdPrice"])
    return out

def jup_quote(inp, out, amount, slippage_bps, swap_mode="ExactIn"):
    return get("jupiter", f"{JUP}/swap/v1/quote", {"inputMint": inp, "outputMint": out, "amount": int(amount),
               "slippageBps": int(slippage_bps), "swapMode": swap_mode, "restrictIntermediateTokens": "true"})

# ---------- DexScreener ----------
def ds_pairs(mint):
    d = get("dexscreener", f"{DS}/latest/dex/tokens/{mint}") or {}
    ps = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana" and p["baseToken"]["address"] == mint]
    return sorted(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)

def ds_profiles():
    return [p for p in (get("dexscreener", f"{DS}/token-profiles/latest/v1") or []) if p.get("chainId") == "solana"]

def ds_boosts():
    out = {}
    for u in ("token-boosts/latest/v1", "token-boosts/top/v1"):
        for b in get("dexscreener", f"{DS}/{u}") or []:
            if b.get("chainId") == "solana": out[b["tokenAddress"]] = b.get("totalAmount") or b.get("amount") or 1
    return out

def ds_orders(mint):
    return (get("dexscreener", f"{DS}/orders/v1/solana/{mint}") or {}).get("orders") or []

# ---------- GeckoTerminal ----------
def gt_pools(path, pages=1):
    out = []
    for p in range(1, pages + 1):
        d = get("geckoterminal", f"{GT}/networks/solana/{path}", {"page": p, "include": "base_token,dex"})
        if not d: break
        out += d.get("data", [])
    return out

# ---------- pump.fun (Cloudflare-blocked from the box as of build) ----------
def pump_live():
    return get("pumpfun", f"{PUMP}/coins/currently-live", {"limit": 50, "offset": 0, "includeNsfw": "false"}, tries=1)

def pump_creator_coins(creator):
    return get("pumpfun", f"{PUMP}/coins/user-created-coins/{creator}", {"limit": 25, "offset": 0}, tries=1)

# ---------- RugCheck ----------
def rugcheck(mint):
    return get("rugcheck", f"{RC}/tokens/{mint}/report", timeout=25)

# ---------- RPC: mint authority / freeze / Token-2022 extensions ----------
DANGEROUS_EXT = {"transferFeeConfig", "transferHook", "permanentDelegate", "nonTransferable", "defaultAccountState"}
def mint_info(mint):
    r = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not r or not r.get("value"): return None
    v = r["value"]; info = v["data"]["parsed"]["info"] if isinstance(v["data"], dict) else {}
    exts = []
    for e in info.get("extensions", []) or []:
        name = e.get("extension"); st = e.get("state", {})
        if name == "transferFeeConfig":
            fee = (st.get("newerTransferFee") or {}).get("transferFeeBasisPoints", 0) or (st.get("olderTransferFee") or {}).get("transferFeeBasisPoints", 0)
            if not fee: continue  # zero-fee config is harmless
        if name == "transferHook" and not st.get("programId"): continue
        if name == "permanentDelegate" and not st.get("delegate"): continue
        if name == "defaultAccountState" and st.get("accountState") != "frozen": continue
        exts.append(name)
    return {"program": v["owner"], "mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply", 0)), "decimals": info.get("decimals"), "dangerous_ext": [e for e in exts if e in DANGEROUS_EXT],
            "token2022": v["owner"] == TOKEN22}

def largest_holders(mint):
    r = rpc("getTokenLargestAccounts", [mint])
    return (r or {}).get("value") or []

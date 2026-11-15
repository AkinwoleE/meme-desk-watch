"""Shared config, HTTP, logging, LIVE gate. The private key is never printed or logged."""
import os, json, time, datetime as dt, threading, requests
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data"); LOGS = os.path.join(ROOT, "logs"); REPORTS = os.path.join(ROOT, "reports")
for d in (DATA, LOGS, REPORTS): os.makedirs(d, exist_ok=True)
WAT = ZoneInfo("Africa/Lagos")
WALLET = "F3tTYWGeyFkSCEsDnZ35amS96PN6oYPTkKYnao6NTBWn"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Empty secret (GitHub sets unset secrets to "") must fall through to the public RPC.
RPC_URL = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
LIVE_FILE = os.path.join(ROOT, "LIVE")  # must contain exactly "I_APPROVE_LIVE_TRADING"

def now(): return dt.datetime.now(WAT)
def ts(): return now().isoformat(timespec="seconds")

def is_live():
    """Live requires BOTH env TRADER_LIVE=1 AND the LIVE file with the approval phrase."""
    if os.environ.get("TRADER_LIVE") != "1": return False
    try: return open(LIVE_FILE).read().strip() == "I_APPROVE_LIVE_TRADING"
    except FileNotFoundError: return False

def rules():
    return json.load(open(os.path.join(ROOT, "rules.json")))

def excluded(mint):
    """Mints this system must never buy, sell or manage (BABA's manual trades / dust)."""
    return mint in (rules().get("excluded_mints") or {})

OUR_SIGS = os.path.join(DATA, "our_signatures.json")
def record_our_sig(sig, what):
    if not sig: return
    d = jload(OUR_SIGS, {}); d[sig] = {"ts": ts(), "what": what}; jsave(OUR_SIGS, d)

def redact(text):
    """Strip the trading key if it ever reaches a log line. The key is never written on purpose."""
    s = text if isinstance(text, str) else str(text)
    key = os.environ.get("SOLANA_TRADING_PRIVATE_KEY") or ""
    if len(key) >= 8 and key in s:
        s = s.replace(key, "[redacted]")
    return s

def log(name, msg, **kw):
    line = redact(json.dumps({"ts": ts(), "msg": msg, **kw}, default=str))
    with open(os.path.join(LOGS, f"{name}.log"), "a") as f: f.write(line + "\n")
    print(line, flush=True)

# ---- source health ----
_health_lock = threading.Lock()
HEALTH_FILE = os.path.join(DATA, "source_health.json")
def _health(src, ok, code):
    with _health_lock:
        try: h = json.load(open(HEALTH_FILE))
        except Exception: h = {}
        e = h.setdefault(src, {"ok": 0, "fail": 0})
        e["ok" if ok else "fail"] += 1; e["last_code"] = code; e["last"] = ts()
        json.dump(h, open(HEALTH_FILE, "w"), indent=1)

_last_call = {}
MIN_GAP = {"geckoterminal": 2.2, "rpc": 0.35, "rugcheck": 0.6, "dexscreener": 0.25, "jupiter": 0.3}
def get(src, url, params=None, tries=3, timeout=15):
    for a in range(tries):
        gap = MIN_GAP.get(src, 0.2); last = _last_call.get(src, 0)
        if time.time() - last < gap: time.sleep(gap - (time.time() - last))
        _last_call[src] = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"})
            if r.status_code == 429: _health(src, False, 429); time.sleep(2 * (a + 1)); continue
            if r.status_code != 200: _health(src, False, r.status_code); return None
            _health(src, True, 200); return r.json()
        except Exception as e:
            _health(src, False, type(e).__name__); time.sleep(1)
    return None

def rpc(method, params, tries=6):
    for a in range(tries):
        gap = MIN_GAP["rpc"]; last = _last_call.get("rpc", 0)
        if time.time() - last < gap: time.sleep(gap - (time.time() - last))
        _last_call["rpc"] = time.time()
        try:
            r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            if r.status_code == 429: _health("rpc", False, 429); time.sleep(2 * (a + 1)); continue
            j = r.json()
            if "error" in j: _health("rpc", False, j["error"].get("code")); time.sleep(1); continue
            _health("rpc", True, 200); return j.get("result")
        except Exception as e:
            _health("rpc", False, type(e).__name__); time.sleep(1)
    return None

def jload(path, default):
    try: return json.load(open(path))
    except Exception: return default

def jsave(path, obj):
    tmp = path + ".tmp"; json.dump(obj, open(tmp, "w"), indent=1, default=str); os.replace(tmp, path)

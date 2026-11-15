"""GitHub Actions exit loop.

Each run heartbeats, then re-checks open positions every ~15s for ~2.5 minutes.
Sells use trader.exits and trader.execution unchanged: same rules, same route
order, 15% slippage cap, size taken from the on-chain balance (read once here
and again inside execution.sell). This process never buys and never touches an
excluded mint or a token this system did not open.
"""
import os
import sys
import time
import json
import datetime as dt
import traceback

from trader.core import WALLET, rules, excluded, log, redact, ts, is_live, WAT, ROOT
from trader import data, exits, execution, journal, safety
from trader.lease import stamp_github, github_may_sell, decide_owner, claim_sell, merge_rules
from trader.mergepos import merge_books
from box import state_sync
from runner.alerts import alert as telegram_alert
from runner import watchdog

EXIT_OWNER = "github"


def is_system_position(pos):
    """A position this system opened: id, mint, entry, time, and a buy fill. Not legacy, not paper."""
    if not isinstance(pos, dict):
        return False
    if pos.get("legacy") or pos.get("paper"):
        return False
    if pos.get("status") != "open":
        return False
    if not pos.get("id") or not pos.get("mint") or not pos.get("opened_at") or not pos.get("entry_price"):
        return False
    fills = pos.get("fills") or []
    return any(isinstance(f, dict) and f.get("side") == "buy" for f in fills)


def fetch_prices(mints):
    """Jupiter price v3, then DexScreener for anything Jupiter did not price. Same sources as the box."""
    if not mints:
        return {}
    out = dict(data.jup_prices(list(mints)) or {})
    for mint in mints:
        if out.get(mint) is not None:
            continue
        try:
            pairs = data.ds_pairs(mint)
        except Exception:
            pairs = None
        if pairs and pairs[0].get("priceUsd"):
            out[mint] = float(pairs[0]["priceUsd"])
    return out


def live_emergency(pos):
    token = data.jup_token(pos["mint"])
    info = data.mint_info(pos["mint"])
    route = safety.roundtrip_test(token) if token else None
    return exits.emergency(pos, token, info, route)


def _as_dt(now_unix):
    return dt.datetime.fromtimestamp(int(now_unix), WAT)


_journal_ready = False


def bind_journal(path=None):
    """Point the journal at a temp dir. The shared book lives on the state branch, not in this checkout."""
    global _journal_ready
    if _journal_ready and not path:
        return
    import tempfile
    directory = path or os.environ.get("EXIT_JOURNAL_DIR") or tempfile.mkdtemp(prefix="exit-journal-")
    os.makedirs(directory, exist_ok=True)
    journal.POS_FILE = os.path.join(directory, "positions.json")
    journal.JOURNAL = os.path.join(directory, "journal.jsonl")
    journal.LOCK = os.path.join(directory, "lock")
    journal.PAPER_FILE = os.path.join(directory, "paper.json")
    _journal_ready = True


def install_state_rules(client):
    """Overlay excluded mints (and slippage) published by the box. No-op when the branch has no rules yet."""
    remote = state_sync.load_all(client).get("rules")
    if not remote:
        return None
    merged = merge_rules(rules(), remote)
    path = os.path.join(ROOT, "rules.json")
    tmp = path + ".tmp"
    json.dump(merged, open(tmp, "w"), indent=1)
    os.replace(tmp, path)
    return merged


def _persist(client, book, lease, watch):
    return state_sync.save_changed(client, book, lease, watch)


def _apply_actions(client, book, pos, acts, price, alerter, now_unix):
    """Sell each action. A fill is written back to the state branch before the next action."""
    sold = 0
    pid = pos["id"]
    for action in acts:
        mint = pos.get("mint")
        if excluded(mint):
            alerter(f"refusing to sell excluded mint {mint} ({pos.get('symbol')})")
            break
        if not is_system_position(pos):
            alerter(f"refusing to sell {pos.get('symbol') or mint}: not opened by this system")
            break
        remote = state_sync.load_all(client)
        lease, ok = claim_sell(remote["lease"], EXIT_OWNER, mint, int(now_unix))
        if not ok:
            log("exit", "sell claim held by the box", mint=mint, symbol=pos.get("symbol"))
            break
        try:
            _persist(client, book, lease, remote["watch"])
        except Exception as exc:
            log("exit", "claim publish failed; sell is still sized from the chain", err=redact(str(exc))[:200])
        current = ((state_sync.load_all(client)["lease"].get("sell_claims") or {}).get(mint) or {})
        if current.get("owner") not in (None, EXIT_OWNER):
            log("exit", "lost sell claim", mint=mint)
            break
        full = action["qty"] >= (pos.get("qty") or 0) * 0.999
        try:
            pre = execution.onchain_raw(mint)
            if pre is None:
                raise RuntimeError("balance read failed")
            if pre == 0:
                raise execution.NoBalance("no on-chain balance")
            fill = execution.sell(mint, action["qty"], pos.get("decimals", 6), price, paper=False, full=full)
            if not fill or not fill.get("sig") or fill.get("route") == "DRY-RUN":
                raise RuntimeError("sell did not land; refusing to mark the position closed")
        except execution.NoBalance:
            pos["status"] = "closed"
            pos["closed_at"] = ts()
            pos["exit_reason"] = "external"
            pos["qty"] = 0
            book[pid] = pos
            log("exit", "no on-chain balance; marked closed", id=pid, symbol=pos.get("symbol"))
            remote = state_sync.load_all(client)
            _persist(client, book, remote["lease"], remote["watch"])
            break
        except Exception as exc:
            fails = int(pos.get("sell_failures") or 0) + 1
            pos["sell_failures"] = fails
            book[pid] = pos
            err = redact(str(exc))[:300]
            log("exit", "sell failed (will retry next tick)", id=pid, symbol=pos.get("symbol"), reason=action["reason"], attempt=fails, err=err)
            if fails in (3, 10) or fails % 40 == 0:
                alerter(f"SELL FAILED {pos.get('symbol')} after {fails} tries: {action['reason']} ({err})")
            remote = state_sync.load_all(client)
            _persist(client, book, remote["lease"], remote["watch"])
            break
        bind_journal()
        book[pid] = pos
        journal.save(book, False)
        pos = journal.record_fill(pid, fill, action["reason"], False, action.get("tp"))
        book = journal.load(False)
        sold += 1
        entry = pos.get("entry_price") or 0
        multiple = (price / entry) if entry else 0
        pnl = pos.get("pnl_usd")
        if pnl is None:
            pnl = (pos.get("realized_usd") or 0) - (pos.get("cost_usd") or 0)
        alerter(
            f"SELL {pos.get('symbol')} {multiple:.2f}x PnL {pnl:+.4f} USD "
            f"tx {fill.get('sig')} ({action['reason']})"
        )
        log("exit", "LIVE SELL", id=pid, symbol=pos.get("symbol"), qty=fill.get("qty"), reason=action["reason"],
            usd=fill.get("usd"), sig=fill.get("sig"), route=fill.get("route"))
        remote = state_sync.load_all(client)
        try:
            _persist(client, book, remote["lease"], remote["watch"])
        except Exception as exc:
            alerter(
                f"SELL LANDED but state push failed {pos.get('symbol')} tx {fill.get('sig')}: "
                + redact(str(exc))[:200]
            )
            raise
        if pos.get("status") == "closed":
            try:
                closed = execution.close_empty_account(mint)
                if closed:
                    log("exit", "closed empty token account", id=pid, sigs=closed)
            except Exception as exc:
                log("exit", "close account failed", err=redact(str(exc))[:200])
            break
    book[pid] = pos
    return sold, book, pos


def tick(client, now_unix, alerter, price_fn=None, emergency_fn=None, do_emergency=False):
    """One pass: heartbeat, prices, exit rules, sells. Returns a small summary."""
    bundle = state_sync.load_all(client)
    lease = stamp_github(bundle["lease"], int(now_unix), ts())
    state_sync.save_changed(client, bundle["positions"], lease, bundle["watch"])
    bundle = state_sync.load_all(client)
    owner = decide_owner(bundle["lease"], int(now_unix))
    if not github_may_sell(bundle["lease"], int(now_unix)):
        log("exit", "lease is not github; not selling this tick", owner=owner)
        return {"sold": 0, "checked": 0, "owner": owner}

    book = bundle["positions"]
    checked = 0
    sold = 0
    changed = False
    price_fn = price_fn or fetch_prices
    open_ids = []
    for pid, pos in book.items():
        if pos.get("status") != "open":
            continue
        if excluded(pos.get("mint")):
            log("exit", "excluded mint skipped", id=pid, mint=pos.get("mint"), symbol=pos.get("symbol"))
            continue
        if not is_system_position(pos):
            log("exit", "not a system position; not touched", id=pid, mint=pos.get("mint"))
            continue
        open_ids.append(pid)
    prices = price_fn([book[i]["mint"] for i in open_ids]) if open_ids else {}
    t_now = _as_dt(now_unix)
    for pid in open_ids:
        pos = book.get(pid) or {}
        if pos.get("status") != "open":
            continue
        px = (prices or {}).get(pos["mint"])
        if px is None:
            log("exit", "no price", id=pid, symbol=pos.get("symbol"))
            continue
        checked += 1
        if do_emergency and (pos.get("exit_plan") or {}).get("emergency"):
            try:
                why = (emergency_fn or live_emergency)(pos)
            except Exception as exc:
                why = []
                log("exit", "emergency check failed", id=pid, err=redact(str(exc))[:200])
            if why:
                n, book, pos = _apply_actions(
                    client, book, pos,
                    [{"qty": pos["qty"], "reason": "emergency: " + "; ".join(why)}],
                    px, alerter, now_unix,
                )
                sold += n
                changed = True
        if pos.get("status") != "open":
            book[pid] = pos
            continue
        acts = exits.evaluate(pos, px, t_now)
        pos["last_price"] = px
        pos["last_check"] = ts()
        changed = True
        if acts:
            n, book, pos = _apply_actions(client, book, pos, acts, px, alerter, now_unix)
            sold += n
        book[pid] = pos
    if changed:
        remote = state_sync.load_all(client)
        # Keep a close or a peak the box published while we were pricing.
        book = merge_books(remote["positions"], book)
        _persist(client, book, remote["lease"], remote["watch"])
    log("exit", "tick", checked=checked, sold=sold, owner=owner)
    return {"sold": sold, "checked": checked, "owner": owner}


def check_sol_reserve(alerter, balances_fn=None):
    """Alert when SOL is under the fee reserve. Sells are not blocked: a stop must still fire.
    The runner never buys, which is what the reserve exists to fund."""
    balances_fn = balances_fn or execution.balances
    try:
        bal = balances_fn()
    except Exception as exc:
        log("exit", "sol reserve check failed", err=redact(str(exc))[:200])
        return
    if not bal:
        return
    reserve = rules()["min_sol_reserve"]
    sol = bal.get("sol") or 0
    if sol < reserve:
        alerter(f"SOL {sol:.4f} is below the {reserve} SOL fee reserve (runner never buys)")


def run_watchdog(client, alerter, now_unix, rpc_fn=None, heartbeat=None, max_age_min=None):
    bundle = state_sync.load_all(client)
    watch = dict(bundle["watch"])
    alerts = []
    if heartbeat is None:
        try:
            heartbeat, _sha = client.get(
                os.environ.get("HEARTBEAT_PATH", "status/heartbeat.json"),
                ref=os.environ.get("HEARTBEAT_BRANCH", "status"),
            )
        except Exception as exc:
            log("exit", "heartbeat fetch failed", err=redact(str(exc))[:200])
            heartbeat = None
    age = float(max_age_min if max_age_min is not None else os.environ.get("HEARTBEAT_MAX_AGE_MIN", 30))
    found, watch = watchdog.check_heartbeat(heartbeat, watch, int(now_unix), max_age_min=age)
    alerts.extend(found)
    rpc_fn = rpc_fn or __import__("trader.core", fromlist=["rpc"]).rpc
    try:
        found, watch = watchdog.check_chain(rpc_fn, WALLET, watch)
        alerts.extend(found)
    except Exception as exc:
        log("exit", "chain check error", err=redact(str(exc))[:300])
    for text in alerts:
        alerter(text)
    remote = state_sync.load_all(client)
    _persist(client, remote["positions"], remote["lease"], watch)
    return alerts


def run(client, duration_s=None, interval_s=None, sleep=time.sleep, clock=None, alerter=None,
        price_fn=None, emergency_fn=None, once=False, rpc_fn=None, heartbeat=None, balances_fn=None):
    """Loop until duration_s has elapsed. Heartbeat is written before any sell, every tick."""
    os.environ["EXIT_RUNNER"] = "1"
    alerter = alerter or telegram_alert
    if not is_live():
        alerter("exit runner is not live (need TRADER_LIVE=1 and the LIVE file); refusing to run")
        raise SystemExit(1)
    if hasattr(client, "ensure_branch"):
        client.ensure_branch()
    install_state_rules(client)
    clock = clock or time.time
    duration_s = float(150 if duration_s is None else duration_s)
    interval_s = float(15 if interval_s is None else interval_s)
    start = clock()
    deadline = start + duration_s
    run_watchdog(client, alerter, start, rpc_fn=rpc_fn, heartbeat=heartbeat)
    check_sol_reserve(alerter, balances_fn=balances_fn)
    ticks = 0
    last_emerg = None
    while True:
        now_u = clock()
        do_emerg = last_emerg is None or (now_u - last_emerg) >= 120
        try:
            tick(client, now_u, alerter, price_fn=price_fn, emergency_fn=emergency_fn, do_emergency=do_emerg)
        except Exception as exc:
            log("exit", "tick error", err=redact(traceback.format_exc()[-800:]))
            alerter("exit tick failed: " + redact(str(exc))[:300])
        if do_emerg:
            last_emerg = now_u
        ticks += 1
        if once or clock() >= deadline:
            break
        elapsed = clock() - now_u
        remain = deadline - clock()
        if remain <= 0:
            break
        wait = min(max(0.0, interval_s - elapsed), remain)
        if wait > 0:
            sleep(wait)
    log("exit", "run done", ticks=ticks, seconds=round(clock() - start, 1))
    return ticks


def _env_float(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    return float(raw)


def main():
    os.environ["EXIT_RUNNER"] = "1"
    if not os.environ.get("SOLANA_TRADING_PRIVATE_KEY"):
        telegram_alert("SOLANA_TRADING_PRIVATE_KEY is not set; sells cannot be signed")
    client = state_sync.default_client()
    if client is None:
        telegram_alert("GH_TOKEN missing; cannot read the shared position book")
        raise SystemExit(1)
    try:
        run(
            client,
            duration_s=_env_float("EXIT_LOOP_SECONDS", 150),
            interval_s=_env_float("EXIT_INTERVAL_SECONDS", 15),
        )
    except SystemExit:
        raise
    except Exception:
        log("exit", "run failed", err=redact(traceback.format_exc()[-800:]))
        telegram_alert("exit runner failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

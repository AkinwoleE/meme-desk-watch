#!/usr/bin/env python
"""Exit monitor: nonstop, every ~15s. Prices from Jupiter price v3 (batched). Launchpad emergency checks every ~2 min.
Real positions in DRY-RUN: signals are logged (deduped), nothing is sold, position is not mutated except peak tracking.
Paper positions: simulated fills are journaled."""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trader.core import rules, now, ts, log, is_live, DATA, jsave, rpc, WALLET, excluded
from trader import foreign
from trader import data, exits, execution, journal, safety

HEART = os.path.join(DATA, "monitor_heartbeat.json")

def alert(msg, **kw):
    """Alerts go to logs/alerts.log (picked up by the status push / GitHub watchdog)."""
    with open(os.path.join(os.path.dirname(DATA), "logs", "alerts.log"), "a") as f: f.write(json.dumps({"ts": ts(), "msg": msg, **kw}, default=str) + "\n")
    log("monitor", "ALERT " + msg, **kw)

def onchain_qty(mint):
    r = rpc("getTokenAccountsByOwner", [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
    if r is None: return None
    return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r.get("value", []))

def _sync():
    """state_sync.py sits next to this file on the box. In this repo it lives under box/."""
    try:
        import state_sync
        return state_sync
    except ImportError:
        from box import state_sync
        return state_sync

def lease_execute():
    """False when the GitHub exit runner's heartbeat is fresh (<6 min).

    True when that heartbeat is stale (>10 min) and this box is awake, or when
    state sync is not configured (no token): the box keeps selling, as it does today.
    """
    try:
        sync = _sync()
    except ImportError:
        return True, None
    try:
        sync.touch_box_heartbeat()
        remote = sync.fetch_positions()
        return sync.box_should_sell(), remote
    except Exception as e:
        log("monitor", "lease read failed; using cache", err=str(e)[:200])
        try:
            return sync.box_should_sell(), None
        except Exception:
            return True, None

def act_on(pos, acts, price, paper, source, execute=True):
    live = is_live()
    for a in acts:
        if not execute:
            # Do not touch signals_logged: that list suppresses a later real sell.
            log(source, "EXIT RECORDED (GitHub runner owns exits; not sold)", id=pos["id"], symbol=pos.get("symbol"), qty=a["qty"], reason=a["reason"], price=price,
                est_usd=round(a["qty"] * (price or 0), 4))
            continue
        if not paper and not live:
            key = a["reason"].split(" (")[0]
            if key in pos.setdefault("signals_logged", []): continue
            pos["signals_logged"].append(key)
            log(source, "DRY-RUN SIGNAL (real position, not executed)", id=pos["id"], symbol=pos["symbol"], qty=a["qty"], reason=a["reason"], price=price,
                est_usd=round(a["qty"] * price, 4))
            continue
        full = a["qty"] >= pos["qty"] * 0.999
        try:
            fill = execution.sell(pos["mint"], a["qty"], pos.get("decimals", 6), price, paper=paper, full=full)
        except execution.NoBalance:
            reconcile(); log(source, "sell skipped: no on-chain balance (sold outside system)", id=pos["id"]); pos["status"] = "closed"; break
        except Exception as e:
            n = pos["sell_failures"] = pos.get("sell_failures", 0) + 1
            log(source, "sell failed (will retry next tick)", id=pos["id"], symbol=pos["symbol"], reason=a["reason"], attempt=n, err=str(e)[:300])
            if n in (3, 10) or n % 40 == 0:
                alert("SELL STUCK", id=pos["id"], symbol=pos["symbol"], mint=pos["mint"], reason=a["reason"], attempts=n, err=str(e)[:300])
            with journal.locked():
                b = journal.load(paper)
                if pos["id"] in b: b[pos["id"]]["sell_failures"] = n; journal.save(b, paper)
            break
        p2 = journal.record_fill(pos["id"], fill, a["reason"], paper, a.get("tp"))
        pos.update({k: p2[k] for k in ("qty", "tp_done", "status", "realized_usd", "fills")})
        log(source, ("LIVE SELL" if live else "PAPER SELL"), id=pos["id"], symbol=pos["symbol"], qty=a["qty"], reason=a["reason"], usd=fill["usd"])
        if pos["status"] == "closed":
            if not paper and live:
                try:
                    cs = execution.close_empty_account(pos["mint"])
                    if cs: log(source, "closed our empty token account (rent reclaimed)", id=pos["id"], sigs=cs)
                except Exception as e: log(source, "close account failed", err=str(e)[:200])
            break

def tick(source="monitor", price_fn=None, emergency=False):
    t_now = now(); handled = 0
    execute, remote = True, None
    if is_live():
        execute, remote = lease_execute()
        if not execute:
            log(source, "GitHub exit runner holds the lease; this tick records only")
    for paper in ((False,) if is_live() else (False, True)):  # live: paper book ignored
        with journal.locked():
            book = journal.load(paper)
        if remote is not None and not paper:
            from trader.mergepos import merge_books
            book = merge_books(book, remote)
            with journal.locked():
                journal.save(book, paper)
            # A buy whose push failed still has to reach the runner. Push ids the branch does not have.
            try:
                sync = _sync()
                for pid, pos in book.items():
                    if pos.get("status") == "open" and pid not in remote and not excluded(pos["mint"]) and not pos.get("legacy"):
                        sync.push_position(pos)
            except Exception as e:
                log(source, "state push of new position failed", err=str(e)[:200])
        openp = {k: p for k, p in book.items() if p["status"] == "open" and not excluded(p["mint"]) and not p.get("legacy")}
        if not openp: continue
        prices = (price_fn or data.jup_prices)(list({p["mint"] for p in openp.values()}))
        for pid, pos in openp.items():
            px = prices.get(pos["mint"])
            if px is None: log(source, "no price", id=pid, symbol=pos["symbol"]); continue
            if emergency and pos["exit_plan"].get("emergency"):
                t = data.jup_token(pos["mint"]); mi = data.mint_info(pos["mint"])
                rt = safety.roundtrip_test(t) if t else None
                why = exits.emergency(pos, t, mi, rt)
                if why: act_on(pos, [{"qty": pos["qty"], "reason": "emergency: " + "; ".join(why)}], px, paper, source, execute=execute if not paper else True)
            if pos["status"] == "open":
                acts = exits.evaluate(pos, px, t_now)
                if acts: act_on(pos, acts, px, paper, source, execute=execute if not paper else True)
            pos["last_price"] = px; pos["last_check"] = ts(); handled += 1
            with journal.locked():
                b = journal.load(paper)
                if pid in b:
                    for k in ("peak_price", "last_price", "last_check", "signals_logged"):
                        if k in pos: b[pid][k] = pos[k]
                    journal.save(b, paper)
    if is_live() and execute:
        try:
            sync = _sync()
            with journal.locked():
                sync.push_book(journal.load(False))
        except Exception as e:
            log(source, "state push failed", err=str(e)[:200])
    return handled

def reconcile():
    """Real positions whose tokens left the wallet (sold elsewhere) are marked closed_external."""
    with journal.locked():
        b = journal.load(False)
        for pid, p in b.items():
            if p["status"] != "open" or excluded(p["mint"]): continue
            q = onchain_qty(p["mint"])
            if q is not None and q < p["qty"] * 0.01:
                p["status"] = "closed"; p["closed_at"] = ts(); p["exit_reason"] = "external"
                p["qty_external"] = p["qty"] - (q or 0); p["qty"] = q or 0
                p["pnl_usd"] = p.get("realized_usd", 0) - p["cost_usd"]; p["pnl_x"] = p.get("realized_usd", 0) / p["cost_usd"] if p["cost_usd"] else None
                log("monitor", "position sold outside this system", id=pid, symbol=p["symbol"])
                foreign.flog("managed position sold/moved by someone else -> closed, exit_reason=external", id=pid, mint=p["mint"], symbol=p["symbol"], expected_qty=p["qty"], onchain_qty=q)
                journal.event("closed_external", id=pid, mint=p["mint"])
        journal.save(b, False)

def main():
    R = rules(); last_emerg = 0; last_recon = 0
    open(os.path.join(DATA, "monitor.pid"), "w").write(str(os.getpid()))
    log("monitor", "start", live=is_live(), interval=R["monitor_interval_s"])
    last_loop = time.time()
    while True:
        t0 = time.time()
        gap = t0 - last_loop; last_loop = t0
        if gap > 60:
            alert("BOX FROZEN/SUSPENDED: monitor loop gap", gap_s=round(gap), resumed_at=ts())
        try:
            if time.time() - last_recon > 120:
                foreign.check(); reconcile(); last_recon = time.time()
            em = time.time() - last_emerg > 120
            n = tick(emergency=em)
            if em: last_emerg = time.time()
            jsave(HEART, {"ts": ts(), "positions_checked": n, "live": is_live()})
        except Exception:
            log("monitor", "error", tb=traceback.format_exc()[-800:])
        time.sleep(max(1, R["monitor_interval_s"] - (time.time() - t0)))

if __name__ == "__main__":
    main()

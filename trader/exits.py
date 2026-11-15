"""Section 6 exit engine. evaluate(pos, price, t_now) -> list of actions [{'frac_of_current' or 'qty', 'reason'}]."""
import datetime as dt
from .core import rules

def plan_for(profile, catalyst_until=None):
    R = rules(); p = dict(R["exits"][profile]); p["common"] = R["exits"]["common"]; p["profile"] = profile
    if catalyst_until: p["catalyst_until"] = catalyst_until
    return p

def evaluate(pos, price, t_now):
    p = pos["exit_plan"]; C = p["common"]; e = pos["entry_price"]
    if not e or price is None: return []
    x = price / e
    pos["peak_price"] = max(pos.get("peak_price") or e, price)
    peak_x = pos["peak_price"] / e
    el_h = (t_now - dt.datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
    qty = pos["qty"]; orig = pos["qty_orig"]; acts = []
    tp_done = pos.setdefault("tp_done", [])
    launchpad = p["profile"].startswith("launchpad")
    # 1. hard stop
    if x <= p["stop"]:
        return [{"qty": qty, "reason": f"stop {p['stop']}x (now {x:.2f}x)"}]
    # 2. trailing stop
    trail_active = (len(tp_done) > 0) if not launchpad else (peak_x >= p.get("trail_after_x", 2))
    if trail_active and price <= pos["peak_price"] * (1 - p["trail"]):
        return [{"qty": qty, "reason": f"trailing stop {int(p['trail']*100)}% off peak {peak_x:.2f}x (now {x:.2f}x)"}]
    # 3. profit lock
    if p.get("profit_lock") and peak_x >= C["lock_trigger_x"]:
        lvl = max(pos["peak_price"] * (1 - C["lock_drop"]), e * C["lock_floor_x"])
        if price <= lvl: return [{"qty": qty, "reason": f"profit lock (peak {peak_x:.2f}x, now {x:.2f}x)"}]
    if launchpad and peak_x >= p["lock_from_x"]:
        lvl = max(pos["peak_price"] * (1 - p["trail"]), e * p["lock_floor_x"])
        if price <= lvl: return [{"qty": qty, "reason": f"launchpad profit lock (peak {peak_x:.2f}x, floor {p['lock_floor_x']}x)"}]
    # 4. failed entry
    if p.get("failed_entry") and el_h * 60 >= C["failed_after_min"] and peak_x <= C["failed_never_above_x"] and x <= C["failed_at_or_below_x"]:
        return [{"qty": qty, "reason": f"failed entry ({x:.2f}x after {el_h*60:.0f}m, never >1.2x)"}]
    # 5. holding-time rules
    max_h = p["max_hold_h"]
    if p.get("catalyst_until"):
        cu = dt.datetime.fromisoformat(p["catalyst_until"])
        max_h = max(max_h, (cu - dt.datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600 + C["catalyst_extra_h"])
    min_h = p.get("min_hold_h", 0)
    if p.get("time_stop", True) and not launchpad and el_h * 60 >= p.get("expected_hold_min", 1e9) and el_h >= min_h and x < p["time_stop_x"] and not p.get("catalyst_until"):
        return [{"qty": qty, "reason": f"time stop ({el_h:.1f}h, {x:.2f}x < {p['time_stop_x']}x)"}]
    if el_h >= max_h:
        if not launchpad and x >= C["winner_keep_x"] and el_h < max_h * C["winner_ext_mult"]:
            if price <= pos["peak_price"] * (1 - C["winner_drop"]):
                return [{"qty": qty, "reason": f"winner extension: 25% off peak after max hold"}]
        else:
            return [{"qty": qty, "reason": f"max hold {max_h:.0f}h reached ({el_h:.1f}h, {x:.2f}x)"}]
    # 6. take-profit ladder (fractions of ORIGINAL qty)
    for lvl, frac in p["tp"]:
        if x >= lvl and lvl not in tp_done:
            acts.append({"qty": min(qty, orig * frac), "reason": f"take profit {lvl}x ({int(frac*100)}% of original)", "tp": lvl})
            qty -= min(qty, orig * frac)
    return acts

def emergency(pos, t, mi, rt):
    """Launchpad emergency exits (~2 min): authority on, rug, liq <30% entry, dev dump, honeypot."""
    why = []
    if mi and (mi["mint_authority"] or mi["freeze_authority"]): why.append("mint/freeze authority turned on")
    liq = (t or {}).get("liquidity")
    if liq is not None and pos.get("entry_liq") and liq < 0.3 * pos["entry_liq"]: why.append(f"liquidity {liq:,.0f} < 30% of entry")
    dev_now = ((t or {}).get("audit") or {}).get("devBalancePercentage")
    if dev_now is not None and pos.get("entry_dev_pct") and dev_now < pos["entry_dev_pct"] * 0.5: why.append("dev dump")
    if rt is not None and not rt.get("ok") and "sell route" in (rt.get("why") or ""): why.append("honeypot (no sell route)")
    return why

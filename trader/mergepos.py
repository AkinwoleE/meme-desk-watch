"""Merge two position books without resurrecting a sell the other side already did.

Qty only ever moves down once either side has a sell fill. Peak is the max.
Take-profit levels already taken are the union. A new id from the box is kept.
"""
import copy


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fill_key(fill):
    if fill.get("sig"):
        return ("sig", fill["sig"])
    return ("nosig", fill.get("ts"), fill.get("side"), fill.get("qty"))


def union_fills(a, b):
    out, seen = [], set()
    for fill in list(a or []) + list(b or []):
        key = _fill_key(fill)
        if key in seen:
            continue
        seen.add(key)
        out.append(copy.deepcopy(fill))
    return out


def _has_sell(pos):
    return any((f or {}).get("side") == "sell" for f in (pos.get("fills") or []))


def merge_position(a, b):
    p = copy.deepcopy(a)
    p["fills"] = union_fills(a.get("fills"), b.get("fills"))
    sells_a, sells_b = _has_sell(a), _has_sell(b)
    if not sells_a and not sells_b:
        if _num(b.get("qty_orig")) > _num(a.get("qty_orig")):
            for key in ("entry_price", "qty_orig", "qty", "cost_usd"):
                if b.get(key) is not None:
                    p[key] = b[key]
        else:
            p["qty_orig"] = a.get("qty_orig", b.get("qty_orig"))
            p["qty"] = a.get("qty", b.get("qty"))
    else:
        p["qty"] = min(_num(a.get("qty")), _num(b.get("qty")))
    peaks = [n for n in (_num(a.get("peak_price"), None) if a.get("peak_price") is not None else None,
                         _num(b.get("peak_price"), None) if b.get("peak_price") is not None else None) if n is not None]
    if peaks:
        p["peak_price"] = max(peaks)
    levels = set(a.get("tp_done") or []) | set(b.get("tp_done") or [])
    p["tp_done"] = sorted(levels)
    p["realized_usd"] = max(_num(a.get("realized_usd")), _num(b.get("realized_usd")))
    p["sell_failures"] = max(int(a.get("sell_failures") or 0), int(b.get("sell_failures") or 0))
    if a.get("status") == "closed" or b.get("status") == "closed":
        if a.get("status") == "closed" and b.get("status") == "closed":
            src = a if _num(a.get("realized_usd")) >= _num(b.get("realized_usd")) else b
        else:
            src = a if a.get("status") == "closed" else b
        p["status"] = "closed"
        for key in ("closed_at", "exit_reason", "pnl_usd", "pnl_x"):
            if src.get(key) is not None:
                p[key] = src[key]
        p["qty"] = min(_num(a.get("qty")), _num(b.get("qty")))
    else:
        p["status"] = a.get("status") or b.get("status") or "open"
    checks = [x.get("last_check") for x in (a, b) if x.get("last_check")]
    if checks:
        src = a if a.get("last_check") == max(checks) else b
        p["last_check"] = src.get("last_check")
        if src.get("last_price") is not None:
            p["last_price"] = src["last_price"]
    logged = list(dict.fromkeys(list(a.get("signals_logged") or []) + list(b.get("signals_logged") or [])))
    if logged:
        p["signals_logged"] = logged
    for key in ("mint", "symbol", "opened_at", "exit_plan", "decimals", "thesis", "score",
                "cost_usd", "entry_liq", "entry_dev_pct", "graduated", "id", "paper", "legacy"):
        if p.get(key) is None and b.get(key) is not None:
            p[key] = copy.deepcopy(b[key])
    return p


def merge_books(a, b):
    a, b = a or {}, b or {}
    out = {}
    for pid in set(a) | set(b):
        if pid not in a:
            out[pid] = copy.deepcopy(b[pid])
        elif pid not in b:
            out[pid] = copy.deepcopy(a[pid])
        else:
            out[pid] = merge_position(a[pid], b[pid])
    return out


def merge_watch(a, b):
    """Runner-owned watchdog cursor. The newer last_sig write wins; seen alerts are a union."""
    a, b = a or {}, b or {}
    out = dict(a)
    out.update({k: v for k, v in b.items() if k != "seen_alerts"})
    # Prefer whichever side has a last_sig; if both do, keep b (the writer just fetched the chain).
    if b.get("last_sig"):
        out["last_sig"] = b["last_sig"]
    elif a.get("last_sig"):
        out["last_sig"] = a["last_sig"]
    seen = list(dict.fromkeys(list(a.get("seen_alerts") or []) + list(b.get("seen_alerts") or [])))
    out["seen_alerts"] = seen[-200:]
    if "stale_alerted" in b:
        out["stale_alerted"] = b["stale_alerted"]
    return out

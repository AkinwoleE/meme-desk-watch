"""Positions store + JSONL journal (thesis, score, exit plan, fills, exit reason, PnL)."""
import os, json, uuid, threading, fcntl
from .core import DATA, ts, jload, jsave

POS_FILE = os.path.join(DATA, "positions.json")        # real wallet positions
PAPER_FILE = os.path.join(DATA, "paper_positions.json")  # dry-run simulated positions
JOURNAL = os.path.join(DATA, "journal.jsonl")
LOCK = os.path.join(DATA, ".pos.lock")

class locked:
    def __enter__(self): self.f = open(LOCK, "w"); fcntl.flock(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *a): fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()

def load(paper=False): return jload(PAPER_FILE if paper else POS_FILE, {})
def save(d, paper=False): jsave(PAPER_FILE if paper else POS_FILE, d)

def event(kind, **kw):
    with open(JOURNAL, "a") as f: f.write(json.dumps({"ts": ts(), "event": kind, **kw}, default=str) + "\n")

def open_position(mint, symbol, entry_price, qty, cost_usd, thesis, score, exit_plan, fill, paper, extra=None):
    pid = uuid.uuid4().hex[:10]
    pos = {"id": pid, "mint": mint, "symbol": symbol, "opened_at": ts(), "entry_price": entry_price, "qty_orig": qty, "qty": qty,
           "cost_usd": cost_usd, "thesis": thesis, "score": score, "exit_plan": exit_plan, "fills": [fill], "peak_price": entry_price,
           "tp_done": [], "realized_usd": 0.0, "paper": paper, "status": "open", **(extra or {})}
    with locked():
        d = load(paper); d[pid] = pos; save(d, paper)
    event("open", **pos); return pos

def record_fill(pid, fill, reason, paper, tp=None):
    with locked():
        d = load(paper); pos = d[pid]
        pos["fills"].append(fill); pos["qty"] = max(0.0, pos["qty"] - fill["qty"]); pos["realized_usd"] += fill.get("usd", 0)
        if tp is not None: pos["tp_done"].append(tp)
        if pos["qty"] <= pos["qty_orig"] * 1e-6:
            pos["status"] = "closed"; pos["closed_at"] = ts(); pos["exit_reason"] = reason
            pos["pnl_usd"] = pos["realized_usd"] - pos["cost_usd"]; pos["pnl_x"] = pos["realized_usd"] / pos["cost_usd"] if pos["cost_usd"] else None
            event("close", **pos)
        else: event("partial", id=pid, mint=pos["mint"], reason=reason, fill=fill)
        save(d, paper)
    return pos

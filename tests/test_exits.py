"""Simulated price paths for every exit rule. Run: .venv/bin/python -m unittest tests/test_exits.py -v"""
import sys, os, unittest, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import exits
from trader.core import WAT

T0 = dt.datetime(2026, 11, 15, 1, 0, tzinfo=WAT)
def mk(profile, qty=1000.0, catalyst=None):
    return {"id": "t", "entry_price": 1.0, "qty_orig": qty, "qty": qty, "opened_at": T0.isoformat(), "peak_price": 1.0, "tp_done": [],
            "exit_plan": exits.plan_for(profile, catalyst)}

def run(pos, path):
    """path: list of (minutes, price). Applies actions like the monitor (partial fills reduce qty, TP recorded). Returns log."""
    out = []
    for m, px in path:
        acts = exits.evaluate(pos, px, T0 + dt.timedelta(minutes=m))
        for a in acts:
            pos["qty"] -= a["qty"]
            if "tp" in a: pos["tp_done"].append(a["tp"])
            out.append((m, round(a["qty"], 6), a["reason"]))
        if pos["qty"] <= 1e-9: break
    return out

class Exits(unittest.TestCase):
    def test_curve_stop(self):
        r = run(mk("curve"), [(1, 0.9), (2, 0.66), (3, 0.65)]); self.assertEqual(len(r), 1); self.assertIn("stop 0.65", r[0][2]); self.assertEqual(r[0][1], 1000)
    def test_grad_stop(self):
        r = run(mk("graduated"), [(1, 0.71), (2, 0.70)]); self.assertIn("stop 0.7", r[0][2])
    def test_ladder_fractions_of_original(self):
        p = mk("graduated"); r = run(p, [(5, 2.0), (6, 2.1), (10, 4.0), (15, 8.0)])
        self.assertEqual([x[1] for x in r], [500, 200, 150]); self.assertAlmostEqual(p["qty"], 150)  # last 15% rides
    def test_ladder_jump_two_levels_one_tick(self):
        r = run(mk("curve"), [(5, 4.2)]); self.assertEqual([x[1] for x in r], [500, 200])
    def test_launchpad_ladder(self):
        p = mk("launchpad_grad"); r = run(p, [(5, 2.0), (10, 4.0), (15, 8.0)])
        self.assertEqual([x[1] for x in r], [300, 200, 150]); self.assertAlmostEqual(p["qty"], 350)
    def test_trailing_after_first_tp_curve(self):
        p = mk("curve"); r = run(p, [(5, 2.0), (10, 3.0), (11, 2.26), (12, 2.24)])
        self.assertIn("trailing stop 25%", r[-1][2]); self.assertEqual(r[-1][0], 12)
    def test_trailing_grad_40(self):
        # profit lock (25% off peak, armed at 1.5x) is tighter than the 40% trail, so it fires first
        p = mk("graduated"); r = run(p, [(5, 2.0), (10, 3.0), (11, 2.26), (12, 2.24)]); self.assertIn("profit lock", r[-1][2])
    def test_no_trailing_before_tp(self):
        # 1.4x peak then -26%: no TP, no lock (peak<1.5) -> nothing
        r = run(mk("curve"), [(5, 1.4), (6, 1.03)]); self.assertEqual(r, [])
    def test_profit_lock(self):
        r = run(mk("graduated"), [(5, 1.5), (6, 1.6), (7, 1.21), (8, 1.19)])
        self.assertEqual(r[0][0], 8); self.assertIn("profit lock", r[0][2])
    def test_profit_lock_floor(self):
        # peak 1.5 -> 25% off = 1.125 but floor 1.15 wins: sells at <=1.15
        r = run(mk("graduated"), [(5, 1.5), (6, 1.16), (7, 1.15)]); self.assertEqual(r[0][0], 7)
    def test_failed_entry(self):
        r = run(mk("curve"), [(5, 1.1), (19, 0.85), (20, 0.85)]); self.assertEqual(r[0][0], 20); self.assertIn("failed entry", r[0][2])
    def test_failed_entry_not_if_was_above_1_2(self):
        r = run(mk("curve"), [(5, 1.25), (25, 0.85)]); self.assertEqual(r, [])
    def test_time_stop_curve_90m(self):
        r = run(mk("curve"), [(60, 1.1), (89, 1.2), (90, 1.29)]); self.assertEqual(r[0][0], 90); self.assertIn("time stop", r[0][2])
    def test_time_stop_grad_4h(self):
        r = run(mk("graduated"), [(239, 1.1), (240, 1.1)]); self.assertEqual(r[0][0], 240)
    def test_no_time_stop_if_up(self):
        r = run(mk("graduated"), [(240, 1.35), (600, 1.4)]); self.assertEqual(r, [])
    def test_max_hold_and_winner_extension(self):
        p = mk("graduated"); r = run(p, [(60, 1.45), (24*60, 1.45), (30*60, 1.48), (31*60, 1.10)])
        # at 24h x>=1.3 -> kept; drop >25% off peak in extension -> sell (profit lock may also fire)
        self.assertEqual(r[0][0], 31*60)
    def test_extension_ends_at_1_5x_max(self):
        r = run(mk("graduated"), [(60, 1.45), (36*60, 1.45)]); self.assertIn("max hold", r[0][2])
    def test_max_hold_loser(self):
        p = mk("launchpad_grad"); r = run(p, [(60, 0.9), (72*60, 0.9)]); self.assertIn("max hold 72h", r[0][2])
    def test_insider_grad_stop_and_trail(self):
        self.assertIn("stop 0.78", run(mk("insider_grad"), [(5, 0.78)])[0][2])
        self.assertIn("stop 0.75", run(mk("insider_curve"), [(5, 0.75)])[0][2])
        p = mk("insider_curve"); r = run(p, [(5, 2.0), (6, 2.5), (7, 1.99)]); self.assertIn("trailing stop 20%", r[-1][2])
    def test_launchpad_stops_and_no_time_stop(self):
        self.assertIn("stop 0.55", run(mk("launchpad_grad"), [(5, 0.55)])[0][2])
        self.assertIn("stop 0.5", run(mk("launchpad_curve"), [(5, 0.50)])[0][2])
        self.assertEqual(run(mk("launchpad_grad"), [(25, 0.8), (300, 1.0), (24*60, 1.0)]), [])  # no failed-entry/time stop
    def test_launchpad_trail_and_lock_floor(self):
        p = mk("launchpad_grad"); r = run(p, [(5, 2.0), (6, 3.0), (7, 1.66), (8, 1.64)]); self.assertIn("45%", r[-1][2])
        p = mk("launchpad_grad"); r = run(p, [(5, 2.0), (6, 1.11), (7, 1.10)]); self.assertEqual(r[-1][0], 7)
    def test_second_run(self):
        self.assertIn("stop 0.7", run(mk("second_run"), [(5, 0.7)])[0][2])
        self.assertEqual(run(mk("second_run"), [(240, 1.1)])[0][0], 240)  # min hold 4h then time stop
        self.assertIn("max hold 8h", run(mk("second_run"), [(60, 1.35), (8*60, 1.35), (12*60, 1.35)])[-1][2])
    def test_catalyst_extends_hold(self):
        cu = (T0 + dt.timedelta(hours=30)).isoformat()
        r = run(mk("graduated", catalyst=cu), [(240, 1.1), (24*60, 1.1), (41*60, 1.1), (42*60, 1.1)])
        self.assertEqual(r[0][0], 42*60)
    def test_emergency(self):
        pos = {"entry_liq": 20000, "entry_dev_pct": 4}
        self.assertTrue(exits.emergency(pos, {"liquidity": 5000}, None, None))
        self.assertTrue(exits.emergency(pos, {"liquidity": 20000}, {"mint_authority": "x", "freeze_authority": None}, None))
        self.assertTrue(exits.emergency(pos, {"liquidity": 20000, "audit": {"devBalancePercentage": 1}}, None, None))
        self.assertTrue(exits.emergency(pos, {"liquidity": 20000}, None, {"ok": False, "why": "no sell route (honeypot?)"}))
        self.assertFalse(exits.emergency(pos, {"liquidity": 20000, "audit": {"devBalancePercentage": 4}}, {"mint_authority": None, "freeze_authority": None}, {"ok": True}))
    def test_billie_path(self):
        # billie: insider_grad, peaked 1.57x -> lock armed at max(1.178,1.15); next seen price 0.40x -> stop fires (hard stop first)
        p = mk("insider_grad"); r = run(p, [(60, 1.57), (500, 0.40)]); self.assertIn("stop 0.78", r[0][2])
        p = mk("insider_grad"); r = run(p, [(60, 1.57), (61, 1.18), (62, 1.17)]); self.assertEqual(r[0][0], 62); self.assertIn("profit lock", r[0][2])

if __name__ == "__main__":
    unittest.main(verbosity=2)

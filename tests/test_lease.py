"""Lease ownership, book merge, and the box monitor yielding to a fresh GitHub runner."""
import os
import sys
import json
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.lease import (
    GITHUB_FRESH_S, GITHUB_STALE_S, decide_owner, box_may_sell, github_may_sell,
    stamp_github, stamp_box, merge_lease, claim_sell, merge_rules,
)
from trader.mergepos import merge_books, merge_position
from box import state_sync
import monitor
from trader import journal, exits


NOW = 1_700_000_000
CJ = "F4vEFNoH9H2oLYRabaQQ2Z6jAAi2AhRVdTdVH6Nn2J1q"


def lease(github_age=None, box_age=0):
    out = {}
    if github_age is not None:
        out["github_heartbeat_unix"] = NOW - github_age
    if box_age is not None:
        out["box_heartbeat_unix"] = NOW - box_age
    return out


class Lease(unittest.TestCase):
    def test_fresh_runner_owns_even_if_the_box_is_awake(self):
        self.assertEqual(decide_owner(lease(github_age=GITHUB_FRESH_S - 1), NOW), "github")
        self.assertFalse(box_may_sell(lease(github_age=30), NOW, configured=True, box_awake=True))
        self.assertTrue(github_may_sell(lease(github_age=30), NOW))

    def test_six_minutes_is_not_fresh_but_box_still_does_not_sell(self):
        self.assertEqual(decide_owner(lease(github_age=GITHUB_FRESH_S), NOW), "github")
        self.assertFalse(box_may_sell(lease(github_age=7 * 60), NOW))

    def test_ten_minutes_is_not_yet_stale(self):
        self.assertEqual(decide_owner(lease(github_age=GITHUB_STALE_S), NOW), "github")
        self.assertFalse(box_may_sell(lease(github_age=GITHUB_STALE_S), NOW))

    def test_stale_runner_and_awake_box_takes_over(self):
        self.assertEqual(decide_owner(lease(github_age=GITHUB_STALE_S + 1), NOW), "box")
        self.assertTrue(box_may_sell(lease(github_age=11 * 60), NOW))
        self.assertFalse(github_may_sell(lease(github_age=11 * 60), NOW))

    def test_stale_runner_and_asleep_box_sells_nothing(self):
        self.assertEqual(decide_owner(lease(github_age=11 * 60, box_age=None), NOW, box_awake=False), "none")
        self.assertEqual(decide_owner(lease(github_age=None, box_age=None), NOW, box_awake=False), "none")

    def test_no_runner_yet_and_box_awake(self):
        self.assertEqual(decide_owner(lease(github_age=None), NOW, box_awake=True), "box")

    def test_unconfigured_box_keeps_selling(self):
        self.assertTrue(box_may_sell({}, NOW, configured=False))

    def test_merge_lease_keeps_the_other_sides_heartbeat(self):
        remote = {"box_heartbeat_unix": 50, "box_heartbeat_iso": "box"}
        local = stamp_github({}, 80, "gh")
        merged = merge_lease(remote, local)
        self.assertEqual(merged["box_heartbeat_unix"], 50)
        self.assertEqual(merged["github_heartbeat_unix"], 80)
        stamped = stamp_box(merged, 90, "box2")
        merged2 = merge_lease(local, stamped)
        self.assertEqual(merged2["github_heartbeat_unix"], 80)
        self.assertEqual(merged2["box_heartbeat_unix"], 90)

    def test_claim_blocks_the_other_side_until_ttl(self):
        held, ok = claim_sell({}, "box", "Mint", NOW)
        self.assertTrue(ok)
        again, ok = claim_sell(held, "github", "Mint", NOW + 10)
        self.assertFalse(ok)
        later, ok = claim_sell(held, "github", "Mint", NOW + 120)
        self.assertTrue(ok)
        self.assertEqual(later["sell_claims"]["Mint"]["owner"], "github")

    def test_rules_union_keeps_cj_and_a_state_only_mint(self):
        repo = {"excluded_mints": {CJ: "CJ"}, "slippage_bps": {"sell_max": 1500}, "min_sol_reserve": 0.01}
        state = {"excluded_mints": {"NewMint": "transferred_in"}, "slippage_bps": {"sell_max": 1500}}
        merged = merge_rules(repo, state)
        self.assertIn(CJ, merged["excluded_mints"])
        self.assertIn("NewMint", merged["excluded_mints"])
        self.assertEqual(merged["min_sol_reserve"], 0.01)


class Merge(unittest.TestCase):
    def test_new_box_position_survives_and_a_close_is_not_reopened(self):
        github = {"old": {"id": "old", "status": "closed", "qty": 0, "qty_orig": 10, "peak_price": 2,
                          "tp_done": [2], "realized_usd": 3, "fills": [{"side": "sell", "sig": "S", "qty": 10}]}}
        box = {"old": {"id": "old", "status": "open", "qty": 10, "qty_orig": 10, "peak_price": 1.2,
                       "tp_done": [], "realized_usd": 0, "fills": [{"side": "buy", "sig": "B", "qty": 10}]},
               "new": {"id": "new", "status": "open", "qty": 5, "qty_orig": 4, "peak_price": 1,
                       "fills": [{"side": "buy", "sig": "B2", "qty": 5}]}}
        merged = merge_books(github, box)
        self.assertEqual(merged["old"]["status"], "closed")
        self.assertEqual(merged["old"]["qty"], 0)
        self.assertEqual(merged["old"]["tp_done"], [2])
        self.assertEqual(merged["old"]["peak_price"], 2)
        self.assertEqual(merged["new"]["status"], "open")
        self.assertEqual(merged["new"]["qty"], 5)
        self.assertEqual(len(merged["old"]["fills"]), 2)
        corrected = merge_position(
            {"status": "open", "qty": 4, "qty_orig": 4, "entry_price": 1, "peak_price": 1,
             "fills": [{"side": "buy", "sig": "B"}]},
            {"status": "open", "qty": 5, "qty_orig": 5, "entry_price": 0.8, "peak_price": 1,
             "fills": [{"side": "buy", "sig": "B"}]},
        )
        self.assertEqual(corrected["qty_orig"], 5)
        self.assertEqual(corrected["entry_price"], 0.8)

    def test_partial_sell_keeps_the_smaller_qty(self):
        a = {"status": "open", "qty": 1000, "qty_orig": 1000, "peak_price": 1, "tp_done": [],
             "fills": [{"side": "buy", "sig": "B"}, {"side": "sell", "sig": "S", "qty": 0}]}
        b = {"status": "open", "qty": 500, "qty_orig": 1000, "peak_price": 3, "tp_done": [2],
             "fills": [{"side": "buy", "sig": "B"}, {"side": "sell", "sig": "S2", "qty": 500}]}
        merged = merge_position(a, b)
        self.assertEqual(merged["qty"], 500)
        self.assertEqual(merged["peak_price"], 3)
        self.assertEqual(merged["tp_done"], [2])


class BoxYields(unittest.TestCase):
    def test_monitor_does_not_sell_when_the_runner_is_fresh(self):
        d = tempfile.mkdtemp()
        pos = {
            "id": "a", "mint": "MintNotExcluded", "symbol": "X", "qty": 10, "qty_orig": 10,
            "entry_price": 1.0, "peak_price": 1.0, "tp_done": [], "opened_at": "2026-11-15T01:00:00+01:00",
            "status": "open", "exit_plan": exits.plan_for("curve"), "decimals": 6,
        }
        with mock.patch.object(journal, "POS_FILE", d + "/p.json"), mock.patch.object(journal, "JOURNAL", d + "/j.jsonl"), \
             mock.patch.object(journal, "LOCK", d + "/l"), mock.patch.object(journal, "PAPER_FILE", d + "/paper.json"), \
             mock.patch.object(monitor, "is_live", return_value=True), \
             mock.patch.object(state_sync, "default_client", return_value=None), \
             mock.patch.object(state_sync, "box_should_sell", return_value=False), \
             mock.patch.object(monitor.execution, "sell") as sell, mock.patch.object(monitor, "log"):
            journal.save({"a": pos}, False)
            monitor.tick(price_fn=lambda mints: {m: 0.5 for m in mints}, emergency=False)
            sell.assert_not_called()
            saved = json.load(open(d + "/p.json"))
            self.assertEqual(saved["a"]["status"], "open")
            self.assertEqual(saved["a"]["qty"], 10)

    def test_monitor_pushes_open_positions_when_the_branch_is_empty(self):
        d = tempfile.mkdtemp()
        pos = {
            "id": "a", "mint": "MintNotExcluded", "symbol": "X", "qty": 10, "qty_orig": 10,
            "entry_price": 1.0, "peak_price": 1.0, "tp_done": [], "opened_at": "2026-11-15T01:00:00+01:00",
            "status": "open", "exit_plan": exits.plan_for("curve"), "decimals": 6, "cost_usd": 2,
            "fills": [{"side": "buy", "sig": "B", "qty": 10}],
        }
        pushed = []
        with mock.patch.object(journal, "POS_FILE", d + "/p.json"), mock.patch.object(journal, "JOURNAL", d + "/j.jsonl"), \
             mock.patch.object(journal, "LOCK", d + "/l"), mock.patch.object(journal, "PAPER_FILE", d + "/paper.json"), \
             mock.patch.object(monitor, "is_live", return_value=True), \
             mock.patch.object(state_sync, "touch_box_heartbeat", return_value="ok"), \
             mock.patch.object(state_sync, "fetch_positions", return_value={}), \
             mock.patch.object(state_sync, "box_should_sell", return_value=False), \
             mock.patch.object(state_sync, "push_position", side_effect=lambda p, client=None: pushed.append(p["id"]) or "ok"), \
             mock.patch.object(monitor.execution, "sell") as sell, mock.patch.object(monitor, "log"):
            journal.save({"a": pos}, False)
            monitor.tick(price_fn=lambda mints: {m: 0.5 for m in mints}, emergency=False)
        sell.assert_not_called()
        self.assertEqual(pushed, ["a"])

    def test_monitor_sells_when_the_runner_is_not_configured(self):
        d = tempfile.mkdtemp()
        pos = {
            "id": "a", "mint": "MintNotExcluded", "symbol": "X", "qty": 10, "qty_orig": 10,
            "entry_price": 1.0, "peak_price": 1.0, "tp_done": [], "opened_at": "2026-11-15T01:00:00+01:00",
            "status": "open", "exit_plan": exits.plan_for("curve"), "decimals": 6, "cost_usd": 2,
            "realized_usd": 0, "fills": [{"side": "buy", "qty": 10, "usd": 2, "sig": "B"}],
        }
        fill = {"ts": "t", "side": "sell", "qty": 10, "usd": 1, "price": 0.5, "route": "DRY-RUN", "sig": None}

        def box_sells(client=None, now_unix=None):
            return True

        with mock.patch.object(journal, "POS_FILE", d + "/p.json"), mock.patch.object(journal, "JOURNAL", d + "/j.jsonl"), \
             mock.patch.object(journal, "LOCK", d + "/l"), mock.patch.object(journal, "PAPER_FILE", d + "/paper.json"), \
             mock.patch.object(monitor, "is_live", return_value=True), \
             mock.patch.object(state_sync, "default_client", return_value=None), \
             mock.patch.object(state_sync, "box_should_sell", side_effect=box_sells), \
             mock.patch.object(monitor.execution, "sell", return_value=fill) as sell, \
             mock.patch.object(monitor.execution, "close_empty_account", return_value=None), \
             mock.patch.object(monitor, "log"):
            journal.save({"a": dict(pos)}, False)
            monitor.tick(price_fn=lambda mints: {m: 0.5 for m in mints}, emergency=False)
            self.assertEqual(sell.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Exit runner: same rules, on-chain sizing, no buys, no excluded mints, no overlapping double-sells."""
import os
import sys
import json
import unittest
import datetime as dt
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.core import WAT, ROOT, redact, log
from trader import exits, execution
from box.state_sync import MemoryClient, save_changed, push_position
from runner import exit_runner

CJ = "F4vEFNoH9H2oLYRabaQQ2Z6jAAi2AhRVdTdVH6Nn2J1q"
T0 = dt.datetime(2026, 11, 15, 1, 5, tzinfo=WAT)
NOW = int(T0.timestamp())


def make_pos(mint="MintNotExcluded111", symbol="TEST", price_path="curve"):
    return {
        "id": "p1", "mint": mint, "symbol": symbol, "opened_at": "2026-11-15T01:00:00+01:00",
        "entry_price": 1.0, "qty_orig": 1000.0, "qty": 1000.0, "cost_usd": 2.0,
        "peak_price": 1.0, "tp_done": [], "realized_usd": 0.0, "status": "open",
        "decimals": 6, "paper": False, "exit_plan": exits.plan_for(price_path),
        "fills": [{"side": "buy", "qty": 1000.0, "usd": 2.0, "ts": "2026-11-15T01:00:00+01:00", "sig": "BUY1"}],
    }


def fill_for(qty, price, sig="SIGSELL", full=False):
    return {"ts": "t", "side": "sell", "qty": qty, "usd": qty * price, "price": price,
            "route": "jupiter-ultra", "sig": sig, "full": full}


class ClientBook:
    def __init__(self, positions=None, lease=None):
        self.client = MemoryClient()
        if positions is not None:
            self.client.put("positions.json", positions, None, "seed")
        if lease is not None:
            self.client.put("exit_lease.json", lease, None, "seed")


class Runner(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("EXIT_RUNNER", None)
        os.environ.pop("TRADER_LIVE", None)
        live = os.path.join(ROOT, "LIVE")
        if os.path.exists(live):
            os.remove(live)

    def _arm(self):
        os.environ["TRADER_LIVE"] = "1"
        os.environ["EXIT_RUNNER"] = "1"
        with open(os.path.join(ROOT, "LIVE"), "w") as f:
            f.write("I_APPROVE_LIVE_TRADING\n")

    def test_stop_sells_once_and_records_pnl(self):
        self._arm()
        book = ClientBook({"p1": make_pos()})
        alerts = []
        order = []

        def raw(mint):
            order.append(("raw", mint))
            return 1_000_000_000

        def sell(mint, qty, decimals, price, paper=False, full=False):
            order.append(("sell", mint, qty, full))
            return fill_for(qty, price, full=full)

        with mock.patch.object(execution, "onchain_raw", side_effect=raw), \
             mock.patch.object(execution, "sell", side_effect=sell), \
             mock.patch.object(execution, "close_empty_account", return_value=["CLOSESIG"]) as closed:
            summary = exit_runner.tick(
                book.client, NOW, alerts.append,
                price_fn=lambda mints: {m: 0.5 for m in mints}, do_emergency=False,
            )
        self.assertEqual(summary["sold"], 1)
        self.assertEqual(order[0][0], "raw")
        self.assertEqual(order[1][0], "sell")
        self.assertEqual(order[1][2], 1000)
        self.assertTrue(order[1][3])
        saved = book.client.get("positions.json")[0]["p1"]
        self.assertEqual(saved["status"], "closed")
        self.assertEqual(saved["exit_reason"].split(" (")[0], "stop 0.65x")
        self.assertAlmostEqual(saved["pnl_usd"], saved["realized_usd"] - saved["cost_usd"])
        self.assertTrue(any("SELL TEST" in a and "0.50x" in a and "SIGSELL" in a and "PnL" in a for a in alerts))
        closed.assert_called_once_with("MintNotExcluded111")
        # Second tick must not sell again: the balance is the source of truth and the book is closed.
        with mock.patch.object(execution, "onchain_raw", side_effect=raw), \
             mock.patch.object(execution, "sell", side_effect=sell) as again:
            exit_runner.tick(book.client, NOW + 20, alerts.append, price_fn=lambda mints: {m: 0.4 for m in mints})
            again.assert_not_called()

    def test_launchpad_emergency_sells_the_position(self):
        self._arm()
        book = ClientBook({"p1": make_pos(price_path="launchpad_grad")})

        def sell(mint, qty, decimals, price, paper=False, full=False):
            return fill_for(qty, price, sig="EMSIG", full=full)

        with mock.patch.object(execution, "onchain_raw", return_value=1_000_000_000), \
             mock.patch.object(execution, "sell", side_effect=sell) as sell_mock, \
             mock.patch.object(execution, "close_empty_account", return_value=None):
            exit_runner.tick(
                book.client, NOW, lambda text: None,
                price_fn=lambda mints: {m: 1.0 for m in mints},
                do_emergency=True, emergency_fn=lambda pos: ["liquidity crashed"],
            )
        self.assertEqual(sell_mock.call_args[0][1], 1000)
        self.assertTrue(sell_mock.call_args.kwargs["full"])
        saved = book.client.get("positions.json")[0]["p1"]
        self.assertEqual(saved["status"], "closed")
        self.assertIn("emergency", saved["exit_reason"])

    def test_partial_take_profit_records_the_level(self):
        self._arm()
        book = ClientBook({"p1": make_pos()})

        def sell(mint, qty, decimals, price, paper=False, full=False):
            return fill_for(qty, price, sig="TPSIG", full=full)

        with mock.patch.object(execution, "onchain_raw", return_value=1_000_000_000), \
             mock.patch.object(execution, "sell", side_effect=sell) as sell_mock, \
             mock.patch.object(execution, "close_empty_account", return_value=None):
            exit_runner.tick(book.client, NOW, lambda t: None, price_fn=lambda mints: {m: 2.0 for m in mints})
        self.assertEqual(sell_mock.call_args[0][1], 500)
        self.assertFalse(sell_mock.call_args.kwargs["full"])
        saved = book.client.get("positions.json")[0]["p1"]
        self.assertEqual(saved["status"], "open")
        self.assertAlmostEqual(saved["qty"], 500)
        self.assertIn(2, saved["tp_done"])

    def test_excluded_mint_is_never_sold(self):
        self._arm()
        book = ClientBook({"p1": make_pos(mint=CJ, symbol="CJ")})
        with mock.patch.object(execution, "sell") as sell, mock.patch.object(execution, "onchain_raw") as raw:
            exit_runner.tick(book.client, NOW, lambda t: None, price_fn=lambda mints: {CJ: 0.1})
        sell.assert_not_called()
        raw.assert_not_called()
        self.assertEqual(book.client.get("positions.json")[0]["p1"]["status"], "open")

    def test_execution_sell_rejects_cj_before_any_route(self):
        with self.assertRaises(RuntimeError) as caught:
            execution.sell(CJ, 10, 6, 1.0)
        self.assertIn("excluded", str(caught.exception))

    def test_token_without_a_buy_fill_is_not_touched(self):
        self._arm()
        pos = make_pos()
        pos["fills"] = []
        book = ClientBook({"p1": pos})
        with mock.patch.object(execution, "sell") as sell:
            exit_runner.tick(book.client, NOW, lambda t: None, price_fn=lambda mints: {m: 0.1 for m in mints})
        sell.assert_not_called()

    def test_zero_balance_closes_without_a_swap(self):
        self._arm()
        book = ClientBook({"p1": make_pos()})
        with mock.patch.object(execution, "onchain_raw", return_value=0), \
             mock.patch.object(execution, "sell") as sell:
            exit_runner.tick(book.client, NOW, lambda t: None, price_fn=lambda mints: {m: 0.5 for m in mints})
        sell.assert_not_called()
        saved = book.client.get("positions.json")[0]["p1"]
        self.assertEqual(saved["status"], "closed")
        self.assertEqual(saved["exit_reason"], "external")

    def test_dry_run_fill_is_not_recorded_as_a_close(self):
        self._arm()
        book = ClientBook({"p1": make_pos()})
        alerts = []

        def sell(*a, **k):
            return {"ts": "t", "side": "sell", "qty": 1000, "usd": 1, "route": "DRY-RUN", "sig": None}

        with mock.patch.object(execution, "onchain_raw", return_value=1_000_000_000), \
             mock.patch.object(execution, "sell", side_effect=sell):
            exit_runner.tick(book.client, NOW, alerts.append, price_fn=lambda mints: {m: 0.5 for m in mints})
        self.assertEqual(book.client.get("positions.json")[0]["p1"]["status"], "open")
        self.assertEqual(book.client.get("positions.json")[0]["p1"]["sell_failures"], 1)

    def test_failed_sell_alerts_on_the_third_attempt(self):
        self._arm()
        book = ClientBook({"p1": make_pos()})
        alerts = []
        with mock.patch.object(execution, "onchain_raw", return_value=1_000_000_000), \
             mock.patch.object(execution, "sell", side_effect=RuntimeError("all live routes failed")):
            for i in range(3):
                exit_runner.tick(book.client, NOW + i, alerts.append, price_fn=lambda mints: {m: 0.5 for m in mints})
        self.assertEqual(book.client.get("positions.json")[0]["p1"]["sell_failures"], 3)
        self.assertEqual(sum(1 for a in alerts if a.startswith("SELL FAILED")), 1)
        self.assertIn("TEST", alerts[-1])

    def test_box_claim_blocks_this_tick(self):
        self._arm()
        pos = make_pos()
        book = ClientBook({"p1": pos}, {"sell_claims": {pos["mint"]: {"owner": "box", "unix": NOW}}})
        with mock.patch.object(execution, "sell") as sell, \
             mock.patch.object(execution, "onchain_raw", return_value=1_000_000_000):
            exit_runner.tick(book.client, NOW, lambda t: None, price_fn=lambda mints: {m: 0.5 for m in mints})
        sell.assert_not_called()

    def test_runner_never_buys_and_does_not_scan_the_wallet(self):
        os.environ["EXIT_RUNNER"] = "1"
        with self.assertRaises(RuntimeError):
            execution.buy({"id": "SomeMint"}, 2, 300)
        src = open(exit_runner.__file__).read()
        self.assertNotIn("execution.buy", src)
        self.assertNotIn("getTokenAccountsByOwner", src)
        workflow = open(os.path.join(ROOT, ".github", "workflows", "exit-monitor.yml")).read()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("*/5 * * * *", workflow)
        self.assertIn("SOLANA_TRADING_PRIVATE_KEY", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertNotIn("pull_request:", workflow)
        readme = open(os.path.join(ROOT, "README.md")).read()
        self.assertIn(
            "https://api.github.com/repos/AkinwoleE/meme-desk-watch/actions/workflows/exit-monitor.yml/dispatches",
            readme,
        )
        self.assertIn('{"ref":"main"}', readme)

    def test_loop_covers_about_two_and_a_half_minutes(self):
        self._arm()
        book = ClientBook({})

        class Clock:
            def __init__(self):
                self.t = 1_000_000.0
            def __call__(self):
                return self.t
            def sleep(self, seconds):
                self.t += seconds

        clock = Clock()
        ticks = exit_runner.run(
            book.client, duration_s=150, interval_s=15, sleep=clock.sleep, clock=clock,
            alerter=lambda text: None, price_fn=lambda mints: {},
            rpc_fn=lambda *a, **k: [], heartbeat={"unix": 1_000_000, "ts": "t", "alerts": []},
            balances_fn=lambda: {"sol": 1.0, "usdc": 0},
        )
        self.assertEqual(ticks, 11)
        lease = book.client.get("exit_lease.json")[0]
        self.assertEqual(lease["github_heartbeat_unix"], 1_000_000 + 150)
        self.assertEqual(lease["owner"], "github")

    def test_refuses_to_start_when_not_live(self):
        os.environ.pop("TRADER_LIVE", None)
        with self.assertRaises(SystemExit):
            exit_runner.run(MemoryClient(), once=True, alerter=lambda text: None, balances_fn=lambda: {"sol": 1})

    def test_key_never_reaches_a_log_line(self):
        os.environ["SOLANA_TRADING_PRIVATE_KEY"] = "5" * 64
        try:
            self.assertNotIn("5" * 64, redact("boom " + os.environ["SOLANA_TRADING_PRIVATE_KEY"]))
            with mock.patch("sys.stdout") as out:
                log("exit", "err", err="failed " + os.environ["SOLANA_TRADING_PRIVATE_KEY"])
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertNotIn("5" * 64, printed)
            self.assertIn("[redacted]", printed)
        finally:
            os.environ.pop("SOLANA_TRADING_PRIVATE_KEY", None)

    def test_price_falls_back_to_dexscreener(self):
        with mock.patch.object(exit_runner.data, "jup_prices", return_value={}), \
             mock.patch.object(exit_runner.data, "ds_pairs", return_value=[{"priceUsd": "0.42"}]):
            self.assertEqual(exit_runner.fetch_prices(["MintA"]), {"MintA": 0.42})

    def test_state_push_retries_a_conflict_and_does_not_drop_a_new_buy(self):
        client = MemoryClient()
        client.put("positions.json", {"keep": {"id": "keep", "status": "open", "qty": 1, "fills": [{"side": "buy", "sig": "B"}]}}, None, "seed")
        client.fail_puts = 1
        push_position({"id": "new", "status": "open", "qty": 3, "qty_orig": 3, "fills": [{"side": "buy", "sig": "B2"}]}, client)
        book = client.get("positions.json")[0]
        self.assertIn("keep", book)
        self.assertIn("new", book)
        client.fail_puts = 1
        save_changed(client, book, {"github_heartbeat_unix": 5}, {})
        self.assertEqual(client.get("exit_lease.json")[0]["github_heartbeat_unix"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Stale box heartbeat and unexpected outgoing transfers. No network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.core import WALLET
from runner.watchdog import outgoing_transfers, check_chain, check_heartbeat, KNOWN_PROGRAMS

JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
NOW = 1_700_000_000


def tx_sol_out():
    return {
        "transaction": {"message": {"instructions": [{
            "programId": "11111111111111111111111111111111",
            "program": "system",
            "parsed": {"type": "transfer", "info": {"source": WALLET, "destination": "SomeoneElse", "lamports": 50_000_000}},
        }]}},
        "meta": {"innerInstructions": [], "preTokenBalances": [], "postTokenBalances": []},
    }


def tx_our_sell():
    body = tx_sol_out()
    body["transaction"]["message"]["instructions"].append({"programId": JUP, "program": "jupiter"})
    body["meta"]["preTokenBalances"] = [{
        "accountIndex": 1, "mint": "SomeMint", "owner": WALLET,
        "uiTokenAmount": {"uiAmount": 1000},
    }]
    body["meta"]["postTokenBalances"] = [{
        "accountIndex": 1, "mint": "SomeMint", "owner": WALLET,
        "uiTokenAmount": {"uiAmount": 0},
    }]
    return body


def tx_token_drain():
    body = {
        "transaction": {"message": {"instructions": [{"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}]}},
        "meta": {"innerInstructions": [], "preTokenBalances": [{
            "accountIndex": 1, "mint": "SomeMint", "owner": WALLET,
            "uiTokenAmount": {"uiAmount": 10},
        }], "postTokenBalances": [{
            "accountIndex": 1, "mint": "SomeMint", "owner": WALLET,
            "uiTokenAmount": {"uiAmount": 0},
        }]},
    }
    return body


class Watchdog(unittest.TestCase):
    def test_plain_sol_send_is_outgoing_and_a_jupiter_sell_is_not(self):
        moved, _ = outgoing_transfers(tx_sol_out(), WALLET)
        self.assertEqual(moved, [{"asset": "SOL", "amount": 0.05, "to": "SomeoneElse"}])
        moved, programs = outgoing_transfers(tx_our_sell(), WALLET)
        self.assertEqual(moved, [])
        self.assertIn(JUP, programs)
        self.assertTrue(JUP in KNOWN_PROGRAMS)
        moved, _ = outgoing_transfers(tx_token_drain(), WALLET)
        self.assertEqual(moved[0]["asset"], "SomeMint")
        self.assertEqual(moved[0]["amount"], 10)

    def test_chain_baseline_then_alert(self):
        calls = {"n": 0}

        def rpc(method, params):
            calls["n"] += 1
            if method == "getSignaturesForAddress":
                return [{"signature": "sig1", "err": None}]
            return tx_sol_out()

        alerts, state = check_chain(rpc, WALLET, {})
        self.assertEqual(alerts, [])
        self.assertEqual(state["last_sig"], "sig1")
        alerts, state = check_chain(rpc, WALLET, state)
        self.assertEqual(len(alerts), 1)
        self.assertIn("OUTGOING", alerts[0])
        self.assertIn("sig1", alerts[0])

        def rpc_sell(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "sig2", "err": None}]
            return tx_our_sell()

        alerts, state = check_chain(rpc_sell, WALLET, {"last_sig": "older"})
        self.assertEqual(alerts, [])
        self.assertEqual(state["last_sig"], "sig2")

    def test_stale_heartbeat_alerts_once_then_recovers(self):
        hb = {"unix": NOW - 31 * 60, "ts": "old", "alerts": [{"ts": "t", "msg": "SELL STUCK"}]}
        alerts, state = check_heartbeat(hb, {}, NOW, max_age_min=30)
        self.assertTrue(any("31 min" in a for a in alerts))
        self.assertTrue(any("SELL STUCK" in a for a in alerts))
        alerts, state = check_heartbeat(hb, state, NOW + 10, max_age_min=30)
        self.assertEqual(alerts, [])
        fresh = {"unix": NOW + 10, "ts": "back", "monitor_ok": False, "alerts": [{"ts": "t", "msg": "SELL STUCK"}]}
        alerts, state = check_heartbeat(fresh, state, NOW + 10, max_age_min=30)
        self.assertTrue(any("back" in a for a in alerts))
        self.assertTrue(any("monitor_ok false" in a for a in alerts))
        alerts, state = check_heartbeat(fresh, state, NOW + 20, max_age_min=30)
        self.assertEqual(alerts, [])

    def test_missing_heartbeat_alerts_once(self):
        alerts, state = check_heartbeat(None, {}, NOW)
        self.assertEqual(alerts, ["box heartbeat file missing"])
        alerts, state = check_heartbeat(None, state, NOW + 1)
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

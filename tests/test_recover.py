"""Submit recovery: a timeout after the tx is signed must not send a second route if the first one landed."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import execution as ex

M = "MintXyz"


class Resp:
    def __init__(self, body):
        self._body = body
        self.content = b""
    def json(self):
        return self._body


class Recover(unittest.TestCase):
    def test_confirmed_signature_is_the_fill(self):
        def rpc(method, params):
            self.assertEqual(method, "getSignatureStatuses")
            return {"value": [{"err": None, "confirmationStatus": "confirmed"}]}
        with mock.patch.object(ex, "rpc", side_effect=rpc):
            self.assertEqual(ex._recover(["SIG1"], "sell", M, ex.USDC, 100, 0, wait=5), "SIG1")

    def test_balance_drop_finds_the_landed_wallet_tx(self):
        def rpc(method, params):
            if method == "getSignatureStatuses":
                return {"value": [None]}
            if method == "getSignaturesForAddress":
                return [{"signature": "LANDED", "err": None}]
            raise AssertionError(method)
        with mock.patch.object(ex, "rpc", side_effect=rpc), \
             mock.patch.object(ex, "_bal", return_value=40), \
             mock.patch("trader.history.get_tx", return_value={"transaction": {"signatures": ["LANDED"]}, "meta": {}}), \
             mock.patch("trader.history.deltas", return_value={M: -60}):
            sig = ex._recover(["ATTEMPTED"], "sell", M, ex.USDC, 100, None, wait=5)
        self.assertEqual(sig, "LANDED")

    def test_errored_signature_and_unchanged_balance_is_not_a_fill(self):
        clock = {"t": 0}
        def now():
            clock["t"] += 10
            return clock["t"]
        def rpc(method, params):
            return {"value": [{"err": "boom", "confirmationStatus": "confirmed"}]}
        with mock.patch.object(ex, "rpc", side_effect=rpc), \
             mock.patch.object(ex, "_bal", return_value=100), \
             mock.patch.object(ex.time, "time", side_effect=now), \
             mock.patch.object(ex.time, "sleep"):
            self.assertIsNone(ex._recover(["SIG"], "sell", M, ex.USDC, 100, None, wait=5))

    def _swap_mocks(self, send, recover):
        return [
            mock.patch.object(ex, "_keypair", return_value=object()),
            mock.patch.object(ex, "_bal", return_value=1_000_000),
            mock.patch.object(ex, "_sign_b64", return_value="signed"),
            mock.patch.object(ex, "_our_sigs", return_value=["SWAPSIG"]),
            mock.patch.object(ex, "_send_jito", side_effect=send),
            mock.patch.object(ex, "_confirm", return_value=None),
            mock.patch.object(ex, "_recover", side_effect=recover),
            mock.patch.object(ex, "actual_deltas", return_value={M: -1.0, ex.USDC: 0.5}),
            mock.patch.object(ex, "record_our_sig"),
            mock.patch.object(ex, "log"),
            mock.patch.object(ex, "is_live", return_value=True),
            mock.patch.object(ex, "excluded", return_value=False),
            mock.patch.object(ex, "onchain_raw", return_value=1_000_000),
        ]

    def test_sell_keeps_a_landed_swap_and_does_not_try_pumpportal(self):
        seq = []
        def get(url, **k):
            seq.append("ultra")
            return Resp({"transaction": None})
        def post(url, **k):
            if "pumpportal" in url:
                raise AssertionError("pumpportal after a landed swap")
            seq.append("swap")
            return Resp({"swapTransaction": "raw"})
        patches = self._swap_mocks(send=TimeoutError("jito timeout"), recover=lambda *a, **k: "SWAPSIG")
        with mock.patch.object(ex.requests, "get", side_effect=get), \
             mock.patch.object(ex.requests, "post", side_effect=post), \
             mock.patch.object(ex.data, "jup_quote", return_value={"inAmount": "1000000", "outAmount": "500000"}):
            for p in patches:
                p.start()
            try:
                fill = ex.sell(M, 1.0, 6, 0.5)
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(fill["sig"], "SWAPSIG")
        self.assertEqual(fill["route"], "jupiter-swap+jito (recovered)")
        self.assertEqual(fill["usd"], 0.5)
        self.assertEqual(seq, ["ultra", "swap"])

    def test_ultra_timeout_returns_the_landed_sig_and_skips_swap(self):
        seq = []
        def get(url, **k):
            seq.append("ultra-order")
            return Resp({"transaction": "tx", "requestId": "req"})
        def post(url, **k):
            seq.append("execute" if "execute" in url else "swap")
            if "execute" in url:
                raise TimeoutError("timeout")
            raise AssertionError(url)
        with mock.patch.object(ex, "_keypair", return_value=object()), \
             mock.patch.object(ex, "_bal", return_value=None), \
             mock.patch.object(ex, "_sign_b64", return_value="signed"), \
             mock.patch.object(ex, "_our_sigs", return_value=["ULTRA"]), \
             mock.patch.object(ex, "_recover", return_value="ULTRA"), \
             mock.patch.object(ex, "actual_deltas", return_value={}), \
             mock.patch.object(ex, "record_our_sig"), \
             mock.patch.object(ex, "log"), \
             mock.patch.object(ex.requests, "get", side_effect=get), \
             mock.patch.object(ex.requests, "post", side_effect=post):
            got = ex._live_swap(M, ex.USDC, 10, 1500, M, side="sell", full_sell=True)
        self.assertEqual(got["route"], "jupiter-ultra (recovered)")
        self.assertEqual(got["sig"], "ULTRA")
        self.assertEqual(seq, ["ultra-order", "execute"])

    def test_ultra_timeout_with_nothing_on_chain_still_tries_the_next_route(self):
        seq = []
        def get(url, **k):
            seq.append("ultra-order")
            return Resp({"transaction": "tx", "requestId": "req"})
        def post(url, **k):
            if "execute" in url:
                seq.append("execute")
                raise TimeoutError("timeout")
            seq.append("pumpportal" if "pumpportal" in url else "swap")
            raise RuntimeError("down")
        with mock.patch.object(ex, "_keypair", return_value=object()), \
             mock.patch.object(ex, "_bal", return_value=None), \
             mock.patch.object(ex, "_sign_b64", return_value="signed"), \
             mock.patch.object(ex, "_our_sigs", return_value=["ULTRA"]), \
             mock.patch.object(ex, "_recover", return_value=None), \
             mock.patch.object(ex, "log"), \
             mock.patch.object(ex.requests, "get", side_effect=get), \
             mock.patch.object(ex.requests, "post", side_effect=post), \
             mock.patch.object(ex.data, "jup_quote", return_value={"inAmount": "1", "outAmount": "1"}):
            with self.assertRaises(RuntimeError):
                ex._live_swap(M, ex.USDC, 10, 1500, M, side="sell", full_sell=True)
        self.assertEqual(seq, ["ultra-order", "execute", "swap", "pumpportal"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

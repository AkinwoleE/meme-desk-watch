"""Sell sizing / retry tests with mocked chain (no network, nothing signed)."""
import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import execution as ex
M = "MintXyz"; USDC = ex.USDC

class Sell(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(ex, "is_live", return_value=True), mock.patch.object(ex, "excluded", return_value=False)]
        for x in self.p: x.start()
    def tearDown(self):
        for x in self.p: x.stop()
    def _run(self, bal, qty, full=False):
        calls = {}
        def swap(inp, out, amt, slip, curve, side, full_sell, dec):
            calls.update(amt=amt, slip=slip, full=full_sell, curve=curve); return {"route": "jupiter-ultra", "sig": "S", "out": 1_000_000}
        with mock.patch.object(ex, "onchain_raw", return_value=bal), mock.patch.object(ex, "_live_swap", side_effect=swap), \
             mock.patch.object(ex, "actual_deltas", return_value={M: -bal / 1e6 if calls.get("full", True) else 0, USDC: 0.99}):
            f = ex.sell(M, qty, 6, 1e-4, full=full)
        return calls, f
    def test_full_exit_sells_entire_raw_balance(self):
        c, f = self._run(bal=25920287254, qty=25920.287)  # journal qty slightly lower than chain
        self.assertEqual(c["amt"], 25920287254); self.assertTrue(c["full"]); self.assertEqual(f["usd"], 0.99)
    def test_partial_uses_min_of_request_and_balance(self):
        c, _ = self._run(bal=1_000_000_000, qty=500.0); self.assertEqual(c["amt"], 500_000_000); self.assertFalse(c["full"])
    def test_partial_capped_by_balance(self):
        c, _ = self._run(bal=400_000_000, qty=500.0); self.assertEqual(c["amt"], 400_000_000)
    def test_slippage_capped_15pct(self):
        c, _ = self._run(bal=10**9, qty=1000.0); self.assertEqual(c["slip"], 1500)
        with mock.patch.object(ex, "onchain_raw", return_value=10**9), mock.patch.object(ex, "_live_swap", side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError(a[3]))) as s:
            with self.assertRaises(AssertionError) as e: ex.sell(M, 1000, 6, 1, slippage_bps=3000)
            self.assertEqual(e.exception.args[0], 1500)
    def test_pumpportal_fallback_available_for_sells(self):
        c, _ = self._run(bal=10**9, qty=1000.0); self.assertEqual(c["curve"], M)
    def test_no_balance_raises(self):
        with mock.patch.object(ex, "onchain_raw", return_value=0):
            with self.assertRaises(ex.NoBalance): ex.sell(M, 10, 6, 1)
    def test_excluded_never_sold(self):
        with mock.patch.object(ex, "excluded", return_value=True):
            with self.assertRaises(RuntimeError): ex.sell(M, 10, 6, 1)
    def test_route_fallback_order(self):
        """Ultra fails -> Swap API fails -> PumpPortal tried; all fail -> RuntimeError (monitor retries next tick)."""
        seq = []
        class R:
            def __init__(s, j): s._j = j; s.content = b""
            def json(s): return s._j
        def get(url, **k): seq.append("ultra-order"); return R({"transaction": None})
        def post(url, **k):
            seq.append("pumpportal" if "pumpportal" in url else "swap"); raise RuntimeError("down")
        with mock.patch.object(ex, "_keypair", return_value=object()), mock.patch.object(ex, "_bal", return_value=None), \
             mock.patch.object(ex.requests, "get", side_effect=get), \
             mock.patch.object(ex.requests, "post", side_effect=post), mock.patch.object(ex.data, "jup_quote", return_value={"inAmount": "1", "outAmount": "1"}), \
             mock.patch.object(ex, "log"):
            with self.assertRaises(RuntimeError): ex._live_swap(M, USDC, 10, 1500, M, side="sell", full_sell=True)
        self.assertEqual(seq, ["ultra-order", "swap", "pumpportal"])

if __name__ == "__main__":
    unittest.main(verbosity=2)

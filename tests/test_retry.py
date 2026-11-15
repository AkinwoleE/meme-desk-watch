"""Stuck sell: retried every tick, alert after 3 failures; NoBalance -> no crash."""
import sys, os, unittest, tempfile, json
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor
from trader import journal, execution

class Retry(unittest.TestCase):
    def test_retry_and_alert(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(journal, "POS_FILE", d + "/p.json"), mock.patch.object(journal, "JOURNAL", d + "/j.jsonl"), \
             mock.patch.object(journal, "LOCK", d + "/l"), mock.patch.object(monitor, "is_live", return_value=True), \
             mock.patch.object(monitor.execution, "sell", side_effect=RuntimeError("all live routes failed")), \
             mock.patch.object(monitor, "alert") as al, mock.patch.object(monitor, "log"):
            pos = {"id": "a", "mint": "M", "symbol": "X", "qty": 10, "status": "open"}
            journal.save({"a": dict(pos)}, False)
            for _ in range(4): monitor.act_on(pos, [{"qty": 10, "reason": "stop"}], 1.0, False, "monitor")
            self.assertEqual(pos["sell_failures"], 4); self.assertEqual(al.call_count, 1)
            self.assertEqual(json.load(open(d + "/p.json"))["a"]["sell_failures"], 4)

if __name__ == "__main__":
    unittest.main()

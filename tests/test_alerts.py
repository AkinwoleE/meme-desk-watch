"""Alerts go to the state branch. Telegram is optional and a missing trading key must not take the lease."""
import os
import sys
import json
import base64
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runner import alerts, exit_runner

ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GH_TOKEN", "GITHUB_STATE_TOKEN",
       "GITHUB_STATUS_TOKEN", "REPO", "GITHUB_REPOSITORY", "SOLANA_TRADING_PRIVATE_KEY",
       "EXIT_RUNNER", "STATE_BRANCH")


class Env(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body or {}
    def json(self):
        return self._body


class AlertDelivery(Env):
    def test_missing_telegram_does_not_call_telegram_or_fail(self):
        with mock.patch.object(alerts.requests, "post") as post, \
             mock.patch.object(alerts.requests, "get") as get, \
             mock.patch.object(alerts.requests, "put") as put:
            self.assertTrue(alerts.alert("sell happened"))
        post.assert_not_called()
        get.assert_not_called()
        put.assert_not_called()

    def test_appends_alerts_jsonl_without_telegram_when_only_the_bot_token_is_set(self):
        os.environ["GH_TOKEN"] = "tok"
        os.environ["REPO"] = "AkinwoleE/meme-desk-watch"
        os.environ["STATE_BRANCH"] = "state"
        os.environ["TELEGRAM_BOT_TOKEN"] = "bot-only"
        saved = {}
        def get(url, **k):
            self.assertIn("/contents/alerts.jsonl", url)
            self.assertNotIn("exit_lease", url)
            return Resp(404)
        def put(url, **k):
            saved["body"] = k["json"]
            return Resp(201)
        with mock.patch.object(alerts.requests, "get", side_effect=get), \
             mock.patch.object(alerts.requests, "put", side_effect=put), \
             mock.patch.object(alerts.requests, "post") as post:
            self.assertTrue(alerts.alert("SELL TEST 1.10x"))
        post.assert_not_called()
        line = base64.b64decode(saved["body"]["content"]).decode().strip()
        entry = json.loads(line)
        self.assertEqual(entry["msg"], "SELL TEST 1.10x")
        self.assertEqual(entry["source"], "github-exit-runner")
        self.assertEqual(saved["body"]["branch"], "state")
        self.assertEqual(saved["body"]["message"], "alert")

    def test_telegram_is_used_only_when_both_secrets_exist(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "bot"
        os.environ["TELEGRAM_CHAT_ID"] = "99"
        with mock.patch.object(alerts.requests, "post") as post:
            self.assertTrue(alerts.alert("hi"))
        self.assertIn("api.telegram.org/botbot/sendMessage", post.call_args[0][0])

    def test_telegram_error_does_not_fail_the_alert(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "bot"
        os.environ["TELEGRAM_CHAT_ID"] = "99"
        with mock.patch.object(alerts.requests, "post", side_effect=TimeoutError("down")), \
             mock.patch.object(alerts, "log"):
            self.assertTrue(alerts.alert("hi"))


class KeylessRun(Env):
    def test_missing_key_exits_before_the_lease(self):
        with mock.patch.object(exit_runner, "telegram_alert") as alert, \
             mock.patch.object(exit_runner, "run") as run, \
             mock.patch.object(exit_runner.state_sync, "default_client") as client:
            with self.assertRaises(SystemExit) as caught:
                exit_runner.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("not taking the lease", alert.call_args[0][0])
        run.assert_not_called()
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

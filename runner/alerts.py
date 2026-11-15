"""Runner alerts. No Telegram required: every alert is printed (visible in the Actions log) and appended to
alerts.jsonl on the `state` branch, which the box pulls into logs/alerts.log. Telegram is optional (only if both
TELEGRAM_* secrets exist). The trading key is redacted again here in case a caller interpolated it."""
import os
import json
import base64
import time
import requests
from trader.core import log, redact, ts

ALERTS_PATH = os.environ.get("ALERTS_PATH", "alerts.jsonl")
KEEP = 300


def _append_state(entry):
    token = os.environ.get("GITHUB_STATE_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_STATUS_TOKEN")
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("STATE_BRANCH", "state")
    if not token or not repo:
        return False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    url = f"https://api.github.com/repos/{repo}/contents/{ALERTS_PATH}"
    for _ in range(4):
        cur = requests.get(url, headers=headers, params={"ref": branch}, timeout=20)
        sha, lines = None, []
        if cur.status_code == 200:
            sha = cur.json()["sha"]
            lines = base64.b64decode(cur.json()["content"]).decode().splitlines()
        lines = (lines + [json.dumps(entry)])[-KEEP:]
        body = {"message": "alert", "branch": branch, "content": base64.b64encode(("\n".join(lines) + "\n").encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=headers, json=body, timeout=20)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (409, 422):
            time.sleep(1)
            continue
        return False
    return False


def alert(text):
    text = redact(text)
    print("ALERT: " + text, flush=True)
    entry = {"ts": ts(), "unix": int(time.time()), "source": "github-exit-runner", "msg": text[:1000],
             "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        _append_state(entry)
    except Exception as e:
        log("exit", "alert append failed", err=redact(str(e))[:200])
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}, timeout=20)
        except Exception as e:
            log("exit", "telegram failed", err=redact(str(e))[:200])
    return True

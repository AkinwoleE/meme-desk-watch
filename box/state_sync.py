"""Shared position book on the `state` branch.

The box is not a checkout of this repo. It talks to GitHub with the contents API
and the same fine-grained token it already uses for the status heartbeat
(GITHUB_STATUS_TOKEN or GITHUB_STATE_TOKEN). The Actions runner uses the
workflow's GITHUB_TOKEN (GH_TOKEN) the same way.

This module never prints the token or the trading key.
"""
import os
import json
import time
import base64
import copy

import requests

from trader.core import redact, ts as core_ts
from trader.lease import box_may_sell, merge_lease, stamp_box
from trader.mergepos import merge_books, merge_watch

PATH_POSITIONS = "positions.json"
PATH_LEASE = "exit_lease.json"
PATH_RULES = "rules.json"
PATH_WATCH = "watch_state.json"
API = "https://api.github.com"


class Conflict(Exception):
    pass


class GitHubContentClient:
    def __init__(self, token, repo, branch):
        self.token = token
        self.repo = repo
        self.branch = branch

    def _request(self, method, path, **kwargs):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meme-desk-exit-monitor",
        }
        url = f"{API}/repos/{self.repo}{path}"
        return requests.request(method, url, headers=headers, timeout=30, **kwargs)

    def ensure_branch(self):
        got = self._request("GET", f"/branches/{self.branch}")
        if got.status_code == 200:
            return
        if got.status_code != 404:
            raise RuntimeError(f"branch lookup failed: {got.status_code} {redact(got.text[:200])}")
        repo = self._request("GET", "")
        if repo.status_code != 200:
            raise RuntimeError(f"repo lookup failed: {repo.status_code}")
        base = repo.json()["default_branch"]
        ref = self._request("GET", f"/git/ref/heads/{base}")
        if ref.status_code != 200:
            raise RuntimeError(f"base ref lookup failed: {ref.status_code}")
        created = self._request("POST", "/git/refs", json={"ref": f"refs/heads/{self.branch}", "sha": ref.json()["object"]["sha"]})
        if created.status_code not in (201, 422):
            raise RuntimeError(f"create branch failed: {created.status_code} {redact(created.text[:200])}")

    def get(self, path, ref=None):
        r = self._request("GET", f"/contents/{path}", params={"ref": ref or self.branch})
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise RuntimeError(f"get {path} failed: {r.status_code} {redact(r.text[:200])}")
        body = r.json()
        if isinstance(body, list) or "content" not in body:
            raise RuntimeError(f"get {path} was not a file")
        raw = base64.b64decode(body["content"])
        return json.loads(raw.decode()), body["sha"]

    def put(self, path, obj, sha, message):
        payload = {
            "message": message,
            "content": base64.b64encode(json.dumps(obj, indent=1, default=str).encode()).decode(),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        r = self._request("PUT", f"/contents/{path}", json=payload)
        if r.status_code in (409, 422):
            raise Conflict(redact(r.text[:200]))
        if r.status_code not in (200, 201):
            raise RuntimeError(f"put {path} failed: {r.status_code} {redact(r.text[:200])}")
        return r.json()["content"]["sha"]


class MemoryClient:
    """In-memory contents API for tests. `fail_puts` raises Conflict that many times."""

    def __init__(self):
        self.files = {}
        self.puts = 0
        self.fail_puts = 0
        self.ensure_called = 0

    def ensure_branch(self):
        self.ensure_called += 1

    def get(self, path, ref=None):
        cur = self.files.get(path)
        if not cur:
            return None, None
        return copy.deepcopy(cur[0]), cur[1]

    def put(self, path, obj, sha, message):
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise Conflict("forced")
        cur = self.files.get(path)
        if cur and sha != cur[1]:
            raise Conflict("sha mismatch")
        if cur is None and sha:
            raise Conflict("missing")
        sha_new = f"s{self.puts}"
        self.puts += 1
        self.files[path] = (copy.deepcopy(obj), sha_new)
        return sha_new


def default_client():
    token = os.environ.get("GITHUB_STATE_TOKEN") or os.environ.get("GITHUB_STATUS_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return None
    repo = os.environ.get("GITHUB_STATUS_REPO") or os.environ.get("REPO") or "AkinwoleE/meme-desk-watch"
    branch = os.environ.get("STATE_BRANCH") or "state"
    return GitHubContentClient(token, repo, branch)


def load_all(client):
    positions, psha = client.get(PATH_POSITIONS)
    lease, lsha = client.get(PATH_LEASE)
    rules, rsha = client.get(PATH_RULES)
    watch, wsha = client.get(PATH_WATCH)
    return {
        "positions": positions or {},
        "lease": lease or {},
        "rules": rules,
        "watch": watch or {},
        "shas": {"positions": psha, "lease": lsha, "rules": rsha, "watch": wsha},
    }


def save_changed(client, positions, lease, watch, messages=None):
    """Merge into the latest branch contents and put only the files that changed.

    rules.json is not written. The box owns that file.
    """
    messages = messages or {}
    last = None
    positions = positions or {}
    lease = lease or {}
    watch = watch or {}
    for _ in range(5):
        remote = load_all(client)
        positions = merge_books(remote["positions"], positions)
        lease = merge_lease(remote["lease"], lease)
        watch = merge_watch(remote["watch"], watch)
        try:
            if positions != (remote["positions"] or {}):
                client.put(PATH_POSITIONS, positions, remote["shas"]["positions"], messages.get("positions", "exit-monitor: update positions"))
            if lease != (remote["lease"] or {}):
                client.put(PATH_LEASE, lease, remote["shas"]["lease"], messages.get("lease", "exit-monitor: heartbeat"))
            if watch != (remote["watch"] or {}):
                client.put(PATH_WATCH, watch, remote["shas"]["watch"], messages.get("watch", "exit-monitor: watch state"))
            return {"positions": positions, "lease": lease, "watch": watch}
        except Conflict as exc:
            last = exc
            continue
    raise RuntimeError(f"state branch stayed conflicted: {last}")


def _cache_path():
    from trader.core import DATA
    return os.path.join(DATA, "exit_lease_cache.json")


def _write_cache(lease):
    try:
        from trader.core import jsave
        jsave(_cache_path(), lease or {})
    except Exception:
        return


def _read_cache():
    try:
        from trader.core import jload
        return jload(_cache_path(), None)
    except Exception:
        return None


def push_position(pos, client=None):
    """Merge one position into the shared book. Used by the box right after a buy."""
    client = client if client is not None else default_client()
    if client is None:
        return "no token"
    if not pos or not pos.get("id"):
        return "no position"
    last = None
    for _ in range(5):
        remote, sha = client.get(PATH_POSITIONS)
        book = merge_books(remote or {}, {pos["id"]: pos})
        try:
            client.put(PATH_POSITIONS, book, sha, f"position {pos.get('symbol') or pos['id']}")
            return "ok"
        except Conflict as exc:
            last = exc
    raise RuntimeError(f"push position conflicted: {last}")


def push_rules(client=None, rules_obj=None):
    """Publish rules.json so the runner sees new excluded mints. Exclusions are sticky."""
    client = client if client is not None else default_client()
    if client is None:
        return "no token"
    if rules_obj is None:
        from trader.core import rules
        rules_obj = rules()
    last = None
    for _ in range(5):
        remote, sha = client.get(PATH_RULES)
        body = dict(rules_obj)
        excluded = dict((remote or {}).get("excluded_mints") or {})
        excluded.update(rules_obj.get("excluded_mints") or {})
        body["excluded_mints"] = excluded
        try:
            client.put(PATH_RULES, body, sha, "rules")
            return "ok"
        except Conflict as exc:
            last = exc
    raise RuntimeError(f"push rules conflicted: {last}")


def push_book(book, client=None):
    """Box failover path: publish the book after the box itself sold."""
    client = client if client is not None else default_client()
    if client is None:
        return "no token"
    return save_changed(client, book or {}, {}, {})


def touch_box_heartbeat(client=None, now_unix=None, iso=None):
    client = client if client is not None else default_client()
    if client is None:
        return "no token"
    now_unix = int(time.time() if now_unix is None else now_unix)
    iso = iso or core_ts()
    last = None
    for _ in range(5):
        remote, sha = client.get(PATH_LEASE)
        lease = stamp_box(merge_lease(remote or {}, {}), now_unix, iso)
        # Keep a claim the runner just published.
        lease = merge_lease(remote or {}, lease)
        try:
            client.put(PATH_LEASE, lease, sha, "box heartbeat")
            _write_cache(lease)
            return "ok"
        except Conflict as exc:
            last = exc
    raise RuntimeError(f"heartbeat conflicted: {last}")


def fetch_positions(client=None):
    client = client if client is not None else default_client()
    if client is None:
        return None
    obj, _sha = client.get(PATH_POSITIONS)
    # 404 is an empty book, not "unreachable". The box then pushes positions it already holds.
    return obj or {}


def box_should_sell(client=None, now_unix=None):
    """True only when this box should execute exits.

    No token configured: the GitHub runner is not in use, so the box sells (today's behavior).
    Token configured: sell only when the runner heartbeat is stale (>10 min) and this process is awake.
    A failed read uses the last cached lease so a fresh runner still suppresses sells.
    """
    now_unix = int(time.time() if now_unix is None else now_unix)
    client = client if client is not None else default_client()
    if client is None:
        return True
    try:
        lease, _sha = client.get(PATH_LEASE)
        _write_cache(lease or {})
    except Exception:
        lease = _read_cache()
        if lease is None:
            return True
    return box_may_sell(lease, now_unix, configured=True, box_awake=True)

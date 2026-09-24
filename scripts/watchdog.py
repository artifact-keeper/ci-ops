#!/usr/bin/env python3
"""CI watchdog for the artifact-keeper org.

Reads GitHub's view of the Actions queue across the org repos, evaluates the
rules from ci-architecture-final.md section 3.4, and posts to Discord.

Standard library only. Three layers, so everything past the HTTP call is
testable offline:

    collect(client, now)            -> snapshot  (raw API objects, unchanged shape)
    evaluate(snapshot, state, now)  -> alerts, new state   (pure)
    deliver(alerts, webhook)        -> Discord

Fixtures mode (--fixture world.json) swaps the HTTP transport for FakeGitHub,
which serves the same REST endpoints from recorded run and job objects, so the
collector, the rules and the suppression all run exactly as they do live.
--record writes such a world file from a live run.

Exit status: 0 when the evaluation completed and every post was delivered
(including a run that found the API degraded and said so); 1 otherwise. The
workflow pings healthchecks.io on 0 and /fail on anything else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration (section 3.4). Environment overrides exist for game days only.
# ---------------------------------------------------------------------------

ORG = os.environ.get("WATCHDOG_ORG", "artifact-keeper")
REPOS = os.environ.get(
    "WATCHDOG_REPOS",
    "artifact-keeper,artifact-keeper-web,artifact-keeper-iac,artifact-keeper-test,ci-ops",
).split(",")
LABELS = ["ak-ci-runners", "ak-docker-runners", "ak-e2e-runners", "ak-beefy-runners", "hosted"]
CANARY_LABELS = ["ak-ci-runners", "ak-docker-runners", "ak-e2e-runners"]
CANARY_REPO, CANARY_WORKFLOW = "ci-ops", "runner-canary.yml"
MAIN_RED_REPOS = ["artifact-keeper", "artifact-keeper-web", "artifact-keeper-iac"]
MAIN_RED_WORKFLOW = "ci.yml"
DEADMAN_CHECKS = [
    # name, repo, workflow file, maximum age since the last successful run on main
    ("runner-version-check", "artifact-keeper-arc-runners", "runner-version-check.yml", timedelta(hours=48)),
    ("runner-images", "artifact-keeper-arc-runners", "runner-images.yml", timedelta(days=8)),
]
HOSTED_RE = re.compile(r"^(ubuntu|windows|macos)-")

STALL_QUEUE_AGE = timedelta(minutes=15)
STALL_NO_START = timedelta(minutes=15)
LAST_STARTED_WINDOW = timedelta(minutes=60)
QUEUE_OLD_SELF_HOSTED = timedelta(minutes=30)
QUEUE_OLD_HOSTED = timedelta(minutes=10)
CANARY_WINDOW = timedelta(minutes=90)
FORK_WAITING_AGE = timedelta(hours=4)
RATE_FLOOR = 1000

# Collection windows. "Runs created in the last hour" from the design is widened
# to "created in the last 6 h and updated in the last 60 min": a job that started
# 10 minutes ago inside a run created 2 hours ago must count as a start, or a busy
# pool would look stalled. A run whose updated_at is older than 60 min cannot
# hold a start inside the 60-min window.
RECENT_RUNS_WINDOW = timedelta(hours=6)
CANARY_RUNS_WINDOW = timedelta(hours=3)
MAX_PAGES = 5

# Repeat policy per rule. None = once (per key) until the condition clears.
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
POLICY = {
    # rule: (repeat interval, forget the key when the condition clears)
    "STALLED": (HOUR, True),
    "QUEUE_OLD": (HOUR, False),       # design: at most once per hour per label
    "CANARY_STALE": (timedelta(hours=6), True),
    "MAIN_RED": (None, False),        # design: once per head_sha (the key carries the sha)
    "FORK_WAITING": (DAY, False),     # design: once per day
    "DEADMAN": (DAY, True),
    "RATE": (HOUR, True),
    "API_DEGRADED": (None, True),     # design: post once; re-arms after a clean run
}
STATE_RETENTION = timedelta(days=30)
DISCORD_LIMIT = 1900

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(dt):
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M:%SZ")


def fmt_age(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 72:
        return f"{hours}h{minutes:02d}m"
    return f"{hours // 24}d{hours % 24}h"


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class ApiDegraded(Exception):
    """5xx, 403, 429 or a transport error: the data for this run is not trustworthy."""

    def __init__(self, endpoint, status, detail=""):
        super().__init__(f"{endpoint}: {status} {detail}".strip())
        self.endpoint, self.status, self.detail = endpoint, status, detail


class NotFound(Exception):
    pass


def urllib_transport(method, url, headers, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read() or b""


class GitHub:
    def __init__(self, token, transport=urllib_transport, api="https://api.github.com", sleep=time.sleep):
        self.token, self.transport, self.api, self.sleep = token, transport, api.rstrip("/"), sleep
        self.calls = 0
        self.rate_remaining = None
        self.rate_limit = None
        self.rate_reset = None

    def _url(self, path, params=None):
        url = path if path.startswith("http") else self.api + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe="")
        return url

    def get(self, path, params=None):
        """Returns (json, headers). One retry on 5xx or transport errors."""
        url = self._url(path, params)
        endpoint = urllib.parse.urlsplit(url).path
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "artifact-keeper-ci-watchdog",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        last = None
        for attempt in range(2):
            self.calls += 1
            try:
                status, rheaders, body = self.transport("GET", url, headers)
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last = ApiDegraded(endpoint, "network", type(err).__name__)
                if attempt == 0:
                    self.sleep(3)
                    continue
                raise last
            self._track_rate(rheaders)
            if status == 404:
                raise NotFound(endpoint)
            if status in (403, 429):
                raise ApiDegraded(endpoint, status, _message(body))
            if status >= 500:
                last = ApiDegraded(endpoint, status, _message(body))
                if attempt == 0:
                    self.sleep(3)
                    continue
                raise last
            if status >= 400:
                raise ApiDegraded(endpoint, status, _message(body))
            return json.loads(body or b"null"), rheaders
        raise last  # pragma: no cover

    def _track_rate(self, headers):
        rem = headers.get("x-ratelimit-remaining")
        if rem is not None and rem.isdigit():
            rem = int(rem)
            if self.rate_remaining is None or rem < self.rate_remaining:
                self.rate_remaining = rem
                self.rate_limit = headers.get("x-ratelimit-limit")
                self.rate_reset = headers.get("x-ratelimit-reset")

    def paginate(self, path, params, key, max_pages=MAX_PAGES):
        items, url, p = [], path, dict(params)
        for _ in range(max_pages):
            data, headers = self.get(url, p)
            items.extend(data.get(key, []))
            nxt = _next_link(headers.get("link", ""))
            if not nxt:
                break
            url, p = nxt, None
        return items


def _message(body):
    try:
        return str(json.loads(body).get("message", ""))[:120]
    except (ValueError, AttributeError):
        return ""


def _next_link(link):
    for part in link.split(","):
        m = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Collection: raw API objects, shape unchanged, plus a "_repo" tag on each
# ---------------------------------------------------------------------------


def collect(gh, now):
    snap = {
        "now": iso(now),
        "errors": [],
        "warnings": [],
        "jobs": None,
        "canary_jobs": None,
        "main": {},
        "forks": None,
        "deadman": {},
        "rate": None,
        "world": {},  # everything fetched, for --record
    }
    jobs_by_run = {}

    def remember(repo, runs=(), jobs=None, run_id=None):
        w = snap["world"].setdefault(repo, {"runs": {}, "jobs": {}})
        for r in runs:
            w["runs"][str(r["id"])] = r
        if jobs is not None:
            w["jobs"][str(run_id)] = jobs

    def jobs_for(repo, run_id):
        if run_id not in jobs_by_run:
            jobs = gh.paginate(f"/repos/{ORG}/{repo}/actions/runs/{run_id}/jobs",
                               {"filter": "latest", "per_page": 100}, "jobs")
            remember(repo, jobs=jobs, run_id=run_id)
            jobs_by_run[run_id] = [dict(j, _repo=repo) for j in jobs]
        return jobs_by_run[run_id]

    def guarded(fn):
        try:
            fn()
        except ApiDegraded as err:
            snap["errors"].append({"endpoint": err.endpoint, "status": err.status, "detail": err.detail})

    # 1. Queue data: queued and in-progress runs, plus recently updated ones.
    def queue():
        all_jobs = []
        since = iso(now - RECENT_RUNS_WINDOW)
        for repo in REPOS:
            base = f"/repos/{ORG}/{repo}/actions/runs"
            runs = {}
            try:
                for status in ("queued", "in_progress"):
                    for r in gh.paginate(base, {"status": status, "per_page": 100}, "workflow_runs"):
                        runs[r["id"]] = r
                for r in gh.paginate(base, {"created": f">={since}", "per_page": 100}, "workflow_runs"):
                    runs.setdefault(r["id"], r)
            except NotFound:
                snap["warnings"].append(f"{repo}: 404 on the runs list (repo missing or token cannot see it)")
                continue
            remember(repo, runs.values())
            for r in runs.values():
                active = r.get("status") in ("queued", "in_progress")
                upd = parse_ts(r.get("updated_at"))
                recent = upd is not None and now - upd <= LAST_STARTED_WINDOW
                if r.get("conclusion") == "action_required":
                    continue
                if active or recent:
                    all_jobs.extend(jobs_for(repo, r["id"]))
        snap["jobs"] = all_jobs

    # 2. Canary results.
    def canary():
        try:
            runs = gh.paginate(
                f"/repos/{ORG}/{CANARY_REPO}/actions/workflows/{CANARY_WORKFLOW}/runs",
                {"created": f">={iso(now - CANARY_RUNS_WINDOW)}", "per_page": 20}, "workflow_runs", max_pages=1)
        except NotFound:
            snap["canary_jobs"] = []
            return
        remember(CANARY_REPO, runs)
        jobs = []
        for r in runs:
            jobs.extend(jobs_for(CANARY_REPO, r["id"]))
        snap["canary_jobs"] = jobs

    # 3. Newest completed push run of ci.yml on main.
    def main_red(repo):
        def fn():
            try:
                data, _ = gh.get(f"/repos/{ORG}/{repo}/actions/workflows/{MAIN_RED_WORKFLOW}/runs",
                                 {"branch": "main", "event": "push", "status": "completed", "per_page": 1})
            except NotFound:
                snap["main"][repo] = None
                return
            runs = data.get("workflow_runs", [])
            remember(repo, runs)
            snap["main"][repo] = dict(runs[0], _repo=repo) if runs else None
        return fn

    # 4. Runs waiting for fork approval.
    def forks():
        found = []
        for repo in REPOS:
            try:
                runs = gh.paginate(f"/repos/{ORG}/{repo}/actions/runs",
                                   {"status": "action_required", "per_page": 100}, "workflow_runs", max_pages=2)
            except NotFound:
                continue
            remember(repo, runs)
            found.extend(dict(r, _repo=repo) for r in runs)
        snap["forks"] = found

    # 5. Dead-man checks.
    def deadman(name, repo, workflow, max_age):
        def fn():
            entry = {"repo": repo, "workflow": workflow, "max_age_s": max_age.total_seconds(),
                     "found": True, "last_success": None}
            try:
                data, _ = gh.get(f"/repos/{ORG}/{repo}/actions/workflows/{workflow}/runs",
                                 {"branch": "main", "status": "success", "per_page": 1})
                runs = data.get("workflow_runs", [])
                remember(repo, runs)
                entry["last_success"] = runs[0] if runs else None
            except NotFound:
                entry["found"] = False
            snap["deadman"][name] = entry
        return fn

    guarded(queue)
    guarded(canary)
    for repo in MAIN_RED_REPOS:
        guarded(main_red(repo))
    guarded(forks)
    for check in DEADMAN_CHECKS:
        guarded(deadman(*check))

    snap["rate"] = {"remaining": gh.rate_remaining, "limit": gh.rate_limit, "reset": gh.rate_reset,
                    "calls": gh.calls}
    return snap


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def job_buckets(job):
    """Watched labels a job counts against: the named ak-* pools, any other ak-*
    label (so a dispatched job on ak-nonexistent-runners is watched too), and
    'hosted' for any ubuntu-*/windows-*/macos-* label."""
    out = set()
    for label in job.get("labels") or []:
        if HOSTED_RE.match(label):
            out.add("hosted")
        elif label in LABELS or label.startswith("ak-"):
            out.add(label)
    return out


def has_started(job):
    # A queued job reports started_at == created_at and no runner; so does a
    # skipped one. Only a job that got a runner has really started.
    return job.get("status") in ("in_progress", "completed") and bool(job.get("runner_name")) \
        and job.get("started_at") is not None


def repo_url(repo):
    return f"https://github.com/{ORG}/{repo}"


class Alert:
    def __init__(self, rule, key, text):
        self.rule, self.key, self.text = rule, key, text

    def __repr__(self):
        return f"Alert({self.key})"


def label_stats(jobs, now):
    stats = {}
    for job in jobs:
        for label in job_buckets(job):
            s = stats.setdefault(label, {"queued": [], "last_started": None, "last_job": None})
            if job.get("status") == "queued":
                s["queued"].append(job)
            elif has_started(job):
                started = parse_ts(job["started_at"])
                if now - LAST_STARTED_WINDOW <= started <= now and (
                        s["last_started"] is None or started > s["last_started"]):
                    s["last_started"], s["last_job"] = started, job
    return stats


def evaluate(snap, state, now, run_url=None):
    """Returns (alerts_to_post, all_firing_alerts, new_state, evaluated_rules)."""
    firing, evaluated = [], set()
    footer = f"\nWatchdog run: <{run_url}>" if run_url else ""
    degraded = bool(snap["errors"])

    # API_DEGRADED
    evaluated.add("API_DEGRADED")
    if degraded:
        lines = [f"`{e['endpoint']}` -> {e['status']} {e.get('detail', '')}".rstrip() for e in snap["errors"][:5]]
        firing.append(Alert("API_DEGRADED", "API_DEGRADED",
                            f"**API_DEGRADED**: {len(snap['errors'])} GitHub API request(s) failed (5xx/403/429/network). "
                            "Queue checks (STALLED, QUEUE_OLD, CANARY_STALE) skipped this run; "
                            "no page on stale data.\n" + "\n".join(lines) +
                            "\nStatus: <https://www.githubstatus.com/>" + footer))

    # RATE
    rate = snap.get("rate") or {}
    if rate.get("remaining") is not None:
        evaluated.add("RATE")
        if rate["remaining"] < RATE_FLOOR:
            reset = rate.get("reset")
            reset_s = hhmm(datetime.fromtimestamp(int(reset), timezone.utc)) if reset and str(reset).isdigit() else "?"
            firing.append(Alert("RATE", "RATE",
                                f"**RATE**: X-RateLimit-Remaining {rate['remaining']} of {rate.get('limit') or '?'} "
                                f"(threshold < {RATE_FLOOR}); resets {reset_s}; this run made {rate.get('calls', '?')} calls. "
                                "Something else is spending the watchdog's token." + footer))

    # Queue checks, skipped when degraded.
    if not degraded and snap.get("jobs") is not None:
        evaluated.update({"STALLED", "QUEUE_OLD"})
        stats = label_stats(snap["jobs"], now)
        labels = list(LABELS) + sorted(l for l in stats if l not in LABELS)
        for label in labels:
            s = stats.get(label, {"queued": [], "last_started": None, "last_job": None})
            queued = s["queued"]
            if not queued:
                continue
            oldest = min(queued, key=lambda j: j["created_at"])
            age = now - parse_ts(oldest["created_at"])
            per_repo = {}
            for j in queued:
                per_repo[j["_repo"]] = per_repo.get(j["_repo"], 0) + 1
            repos_s = ", ".join(f"{r} {n}" for r, n in sorted(per_repo.items(), key=lambda x: -x[1]))
            last = s["last_started"]
            last_s = (f"last start {fmt_age(now - last)} ago ({hhmm(last)}, <{s['last_job']['html_url']}>)"
                      if last else f"no start on this label in the last {int(LAST_STARTED_WINDOW.total_seconds() // 60)}m")
            oldest_s = (f"oldest {fmt_age(age)} (created {hhmm(parse_ts(oldest['created_at']))}, "
                        f"{oldest['_repo']} / {oldest.get('workflow_name', '?')} / {oldest.get('name', '?')})\n"
                        f"Oldest job: <{oldest.get('html_url')}>")
            queue_links = " ".join(f"<{repo_url(r)}/actions?query=is%3Aqueued>" for r in sorted(per_repo))

            if age > STALL_QUEUE_AGE and (last is None or now - last > STALL_NO_START):
                firing.append(Alert("STALLED", f"STALLED:{label}",
                                    f"**STALLED** `{label}`: {len(queued)} job(s) queued ({repos_s}), {oldest_s}\n"
                                    f"Nothing is starting: {last_s}. Threshold: oldest queued > 15m and no start in 15m.\n"
                                    f"Queues: {queue_links}" + footer))
            limit = QUEUE_OLD_HOSTED if label == "hosted" else QUEUE_OLD_SELF_HOSTED
            if age > limit:
                firing.append(Alert("QUEUE_OLD", f"QUEUE_OLD:{label}",
                                    f"**QUEUE_OLD** `{label}`: {len(queued)} job(s) queued ({repos_s}), {oldest_s}\n"
                                    f"{last_s[0].upper() + last_s[1:]}. Threshold: oldest queued > {fmt_age(limit)}.\n"
                                    f"Queues: {queue_links}" + footer))

        if snap.get("canary_jobs") is not None:
            evaluated.add("CANARY_STALE")
            for label in CANARY_LABELS:
                ok = [j for j in snap["canary_jobs"]
                      if label in (j.get("labels") or []) and j.get("conclusion") == "success"
                      and j.get("completed_at") and now - parse_ts(j["completed_at"]) <= CANARY_WINDOW]
                queued_n = len(stats.get(label, {}).get("queued", []))
                if not ok and queued_n == 0:
                    seen = [j for j in snap["canary_jobs"] if label in (j.get("labels") or [])]
                    latest = max(seen, key=lambda j: j["created_at"]) if seen else None
                    latest_s = (f"latest canary job: {latest.get('status')}/{latest.get('conclusion')} "
                                f"<{latest.get('html_url')}>" if latest else
                                f"no canary job on this label in the last {fmt_age(CANARY_RUNS_WINDOW)}")
                    firing.append(Alert("CANARY_STALE", f"CANARY_STALE:{label}",
                                        f"**CANARY_STALE** `{label}`: no successful runner-canary job in "
                                        f"{fmt_age(CANARY_WINDOW)} and nothing queued on the label, so the canary itself "
                                        f"broke or its schedule stopped; STALLED cannot fire on an idle pool until it runs again. "
                                        f"{latest_s}\nCanary: <{repo_url(CANARY_REPO)}/actions/workflows/{CANARY_WORKFLOW}>"
                                        + footer))

    # MAIN_RED, per repo whose data arrived.
    for repo, run in snap.get("main", {}).items():
        evaluated.add(f"MAIN_RED:{repo}")
        if run and run.get("conclusion") == "failure":
            sha = run.get("head_sha", "?")
            firing.append(Alert("MAIN_RED", f"MAIN_RED:{repo}:{sha}",
                                f"**MAIN_RED** `{repo}`: newest completed push run of {MAIN_RED_WORKFLOW} on main "
                                f"failed (head {sha[:10]}, run #{run.get('run_number', '?')}, finished "
                                f"{hhmm(parse_ts(run.get('updated_at')))}).\nRun: <{run.get('html_url')}>" + footer))

    # FORK_WAITING
    if snap.get("forks") is not None:
        evaluated.add("FORK_WAITING")
        old = [r for r in snap["forks"] if now - parse_ts(r["created_at"]) > FORK_WAITING_AGE]
        if old:
            old.sort(key=lambda r: r["created_at"])
            links = "\n".join(
                f"- {r['_repo']} / {r.get('name', '?')} from "
                f"`{(r.get('head_repository') or {}).get('full_name', '?')}`, waiting "
                f"{fmt_age(now - parse_ts(r['created_at']))}: <{r.get('html_url')}>" for r in old[:8])
            more = f"\n... and {len(old) - 8} more" if len(old) > 8 else ""
            firing.append(Alert("FORK_WAITING", "FORK_WAITING",
                                f"**FORK_WAITING**: {len(old)} run(s) in `action_required` for more than "
                                f"{fmt_age(FORK_WAITING_AGE)} (oldest {fmt_age(now - parse_ts(old[0]['created_at']))}).\n"
                                f"{links}{more}\nApprove: `gh api -X POST repos/{ORG}/<repo>/actions/runs/<id>/approve`"
                                + footer))

    # DEADMAN
    for name, entry in snap.get("deadman", {}).items():
        evaluated.add(f"DEADMAN:{name}")
        max_age = timedelta(seconds=entry["max_age_s"])
        wf_url = f"{repo_url(entry['repo'])}/actions/workflows/{entry['workflow']}"
        run = entry.get("last_success")
        if not entry["found"]:
            detail = f"workflow `{entry['workflow']}` not found in {entry['repo']} (not merged yet, renamed, or no read access)"
        elif run is None:
            detail = "no successful run on main at all"
        else:
            done = parse_ts(run.get("updated_at") or run.get("created_at"))
            if now - done <= max_age:
                continue
            detail = f"last success {fmt_age(now - done)} ago ({hhmm(done)}): <{run.get('html_url')}>"
        firing.append(Alert("DEADMAN", f"DEADMAN:{name}",
                            f"**DEADMAN** `{name}` ({entry['repo']}): {detail}. Threshold: last success on main older "
                            f"than {fmt_age(max_age)}.\nWorkflow: <{wf_url}>" + footer))

    return apply_suppression(firing, state, now, evaluated)


def _rule_scope(key):
    """The evaluated-rules token a key belongs to (for clearing)."""
    rule = key.split(":", 1)[0]
    if rule in ("MAIN_RED", "DEADMAN"):
        return ":".join(key.split(":")[:2])
    return rule


def apply_suppression(firing, state, now, evaluated):
    posted = dict((state or {}).get("posted", {}))
    to_post = []
    firing_keys = {a.key for a in firing}
    for alert in firing:
        interval, _ = POLICY[alert.rule]
        last = parse_ts(posted.get(alert.key))
        if last is not None and (interval is None or now - last < interval):
            continue
        to_post.append(alert)
    # Forget keys whose condition cleared, for rules that re-arm on recovery.
    for key in list(posted):
        rule = key.split(":", 1)[0]
        if rule not in POLICY:
            continue
        _, clears = POLICY[rule]
        if clears and _rule_scope(key) in evaluated and key not in firing_keys:
            del posted[key]
    # Prune old entries.
    for key, ts in list(posted.items()):
        t = parse_ts(ts)
        if t is None or now - t > STATE_RETENTION:
            del posted[key]
    new_state = {"version": 1, "updated": iso(now), "posted": posted}
    return to_post, firing, new_state, evaluated


def mark_posted(state, alerts, now):
    for a in alerts:
        state["posted"][a.key] = iso(now)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def chunk_messages(texts, limit=DISCORD_LIMIT):
    """Pack whole messages into posts of at most `limit` chars; a single long
    message is truncated. Returns [(post_text, [indices])]."""
    chunks, cur, idx = [], "", []
    for i, t in enumerate(texts):
        t = t if len(t) <= limit else t[: limit - 20] + "\n... (truncated)"
        if cur and len(cur) + 2 + len(t) > limit:
            chunks.append((cur, idx))
            cur, idx = "", []
        cur = f"{cur}\n\n{t}" if cur else t
        idx.append(i)
    if cur:
        chunks.append((cur, idx))
    return chunks


def post_webhook(url, text, transport=urllib_transport, sleep=time.sleep):
    """Discord webhook. The Slack-compatible endpoint (URL ending in /slack, the
    form Alertmanager uses) takes {"text"}; the native one takes {"content"}."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path.endswith("/slack"):
        payload = {"text": text}
    else:
        payload = {"content": text, "allowed_mentions": {"parse": []}}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "artifact-keeper-ci-watchdog"}
    for attempt in range(3):
        try:
            status, _, rbody = transport("POST", url, headers, body)
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            status, rbody = 0, str(type(err).__name__).encode()
        if 200 <= status < 300:
            return True
        if status == 429 or status >= 500 or status == 0:
            try:
                wait = float(json.loads(rbody).get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2
            sleep(min(wait, 10))
            continue
        print(f"webhook rejected the post: HTTP {status}", file=sys.stderr)
        return False
    print("webhook post failed after retries", file=sys.stderr)
    return False


def deliver(alerts, webhook, transport=urllib_transport, sleep=time.sleep):
    """Returns the alerts that were delivered."""
    delivered = []
    for text, idx in chunk_messages([a.text for a in alerts]):
        if post_webhook(webhook, text, transport, sleep):
            delivered.extend(alerts[i] for i in idx)
        sleep(0.5)
    return delivered


# ---------------------------------------------------------------------------
# Fixtures mode: a fake GitHub serving the REST endpoints from a world file
# ---------------------------------------------------------------------------


def rewind_world(world, t):
    """The world as the API would have shown it at time t: drop runs and jobs
    created after t, un-start jobs that started after t (a queued job reports
    started_at == created_at and no runner), un-complete jobs that finished
    after t, and derive run status and updated_at from the jobs."""
    out = {k: v for k, v in world.items() if k != "repos"}
    out["repos"] = {}
    for repo, data in world.get("repos", {}).items():
        runs, jobs = [], {}
        for run in data.get("runs", []):
            if parse_ts(run["created_at"]) > t:
                continue
            run = dict(run)
            rjobs = []
            for job in data.get("jobs", {}).get(str(run["id"]), []):
                if parse_ts(job["created_at"]) > t:
                    continue
                job = dict(job)
                if job.get("runner_name") and parse_ts(job["started_at"]) > t:
                    job.update(status="queued", conclusion=None, started_at=job["created_at"],
                               completed_at=None, runner_id=None, runner_name=None,
                               runner_group_id=None, runner_group_name=None, steps=[])
                elif job.get("completed_at") and parse_ts(job["completed_at"]) > t:
                    job.update(status="in_progress", conclusion=None, completed_at=None)
                rjobs.append(job)
            if rjobs:
                events = [parse_ts(run["created_at"])]
                for j in rjobs:
                    if j.get("runner_name"):
                        events.append(parse_ts(j["started_at"]))
                    if j.get("completed_at"):
                        events.append(parse_ts(j["completed_at"]))
                run["updated_at"] = iso(max(events))
                statuses = {j["status"] for j in rjobs}
                if statuses == {"completed"} and parse_ts(run.get("updated_at")) <= t:
                    concl = {j.get("conclusion") for j in rjobs}
                    run["status"] = "completed"
                    run["conclusion"] = ("failure" if "failure" in concl else
                                         "cancelled" if "cancelled" in concl else "success")
                else:
                    run["status"] = "in_progress" if any(has_started(j) for j in rjobs) else "queued"
                    run["conclusion"] = None
            jobs[str(run["id"])] = rjobs
            runs.append(run)
        out["repos"][repo] = {k: v for k, v in data.items() if k not in ("runs", "jobs")}
        out["repos"][repo].update(runs=runs, jobs=jobs)
    return out


class FakeGitHub:
    """Transport that answers the endpoints collect() uses, from a world dict:

    {"rate": {"remaining": 4200, "limit": 5000, "reset": 1790000000},
     "fail": [{"path_re": "/actions/runs$", "status": 503}],
     "repos": {"<repo>": {"runs": [<run>...], "jobs": {"<run_id>": [<job>...]},
                          "workflows": ["ci.yml", ...]}}}   # workflows optional

    Runs and jobs are verbatim API objects. With "rewind": true the world is
    first rewound to the evaluation time.
    """

    def __init__(self, world, now=None):
        if world.get("rewind") and now is not None:
            world = rewind_world(world, now)
        self.world = world
        self.requests = []

    def __call__(self, method, url, headers, body=None, timeout=20):
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append(path + ("?" + parts.query if parts.query else ""))
        rate = self.world.get("rate", {})
        remaining = rate.get("remaining", 4999)
        hdrs = {"x-ratelimit-remaining": str(remaining), "x-ratelimit-limit": str(rate.get("limit", 5000)),
                "x-ratelimit-reset": str(rate.get("reset", 1790000000))}
        for f in self.world.get("fail", []):
            if re.search(f["path_re"], path):
                return f["status"], hdrs, json.dumps({"message": "fixture failure"}).encode()

        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/actions/(?:workflows/([^/]+)/)?runs", path)
        if m:
            repo = self.world.get("repos", {}).get(m.group(2))
            if repo is None:
                return 404, hdrs, b'{"message": "Not Found"}'
            runs = list(repo.get("runs", []))
            wf = m.group(3)
            if wf:
                known = set(repo.get("workflows", [])) | {r.get("path", "").rsplit("/", 1)[-1] for r in runs}
                if wf not in known:
                    return 404, hdrs, b'{"message": "Not Found"}'
                runs = [r for r in runs if r.get("path", "").rsplit("/", 1)[-1] == wf]
            runs = [r for r in runs if _run_matches(r, query)]
            runs.sort(key=lambda r: r["created_at"], reverse=True)
            return self._page(url, query, runs, "workflow_runs", hdrs)

        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/actions/runs/(\d+)/jobs", path)
        if m:
            repo = self.world.get("repos", {}).get(m.group(2))
            if repo is None:
                return 404, hdrs, b'{"message": "Not Found"}'
            jobs = repo.get("jobs", {}).get(m.group(3), [])
            return self._page(url, query, jobs, "jobs", hdrs)
        return 404, hdrs, b'{"message": "Not Found"}'

    @staticmethod
    def _page(url, query, items, key, hdrs):
        per_page = int(query.get("per_page", 30))
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * per_page: page * per_page]
        if page * per_page < len(items):
            q = dict(query, page=str(page + 1))
            base = url.split("?", 1)[0]
            hdrs = dict(hdrs, link=f'<{base}?{urllib.parse.urlencode(q)}>; rel="next"')
        return 200, hdrs, json.dumps({"total_count": len(items), key: chunk}).encode()


def _run_matches(run, query):
    status = query.get("status")
    if status and status not in (run.get("status"), run.get("conclusion")):
        return False
    for field in ("branch", "event"):
        want = query.get(field)
        have = run.get("head_branch" if field == "branch" else "event")
        if want and want != have:
            return False
    created = query.get("created")
    if created and created.startswith(">="):
        if parse_ts(run["created_at"]) < parse_ts(created[2:]):
            return False
    return True


def world_from_snapshot(snap):
    repos = {}
    for repo, data in snap["world"].items():
        repos[repo] = {"runs": sorted(data["runs"].values(), key=lambda r: r["created_at"]),
                       "jobs": data["jobs"]}
    return {"recorded_at": snap["now"], "repos": repos,
            "rate": {"remaining": (snap.get("rate") or {}).get("remaining") or 4999}}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_state(path):
    try:
        with open(path) as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("posted"), dict):
            return state
        print(f"state file {path} has an unexpected shape; starting empty", file=sys.stderr)
    except FileNotFoundError:
        print(f"no state at {path}; starting empty (first run or cache miss)", file=sys.stderr)
    except (OSError, ValueError) as err:
        print(f"state file {path} unreadable ({type(err).__name__}); starting empty", file=sys.stderr)
    return {"version": 1, "posted": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def summary_md(snap, firing, to_post, delivered, now):
    lines = [f"### CI watchdog, {iso(now)}", ""]
    rate = snap.get("rate") or {}
    lines.append(f"API calls: {rate.get('calls')}, rate remaining: {rate.get('remaining')}, "
                 f"errors: {len(snap['errors'])}")
    for w in snap.get("warnings", []):
        lines.append(f"- warning: {w}")
    if snap.get("jobs") is not None:
        stats = label_stats(snap["jobs"], now)
        lines += ["", "| label | queued | oldest queued | last start (60m) |", "|---|---|---|---|"]
        for label in list(LABELS) + sorted(l for l in stats if l not in LABELS):
            s = stats.get(label, {"queued": [], "last_started": None})
            q = s["queued"]
            oldest = fmt_age(now - min(parse_ts(j["created_at"]) for j in q)) if q else "-"
            last = fmt_age(now - s["last_started"]) + " ago" if s["last_started"] else "none"
            lines.append(f"| {label} | {len(q)} | {oldest} | {last} |")
    lines += ["", f"Firing: {', '.join(a.key for a in firing) or 'none'}",
              f"Posted: {', '.join(a.key for a in delivered) or 'none'}"
              f" (suppressed: {len(firing) - len(to_post)})"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture", help="world JSON file: serve the API from it instead of api.github.com")
    ap.add_argument("--now", help="evaluation time, ISO 8601 (default: current time, or the fixture's 'now')")
    ap.add_argument("--state", default=".watchdog-state/state.json", help="suppression state file")
    ap.add_argument("--dry-run", action="store_true", help="print the posts instead of sending them")
    ap.add_argument("--record", help="write everything fetched as a world file (for fixtures)")
    args = ap.parse_args(argv)

    world = None
    if args.fixture:
        with open(args.fixture) as fh:
            world = json.load(fh)
    now_s = args.now or (world or {}).get("now")
    now = parse_ts(now_s) if now_s else datetime.now(timezone.utc)

    if world is not None:
        gh = GitHub("fixture", transport=FakeGitHub(world, now), sleep=lambda s: None)
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print("::error::no GH_TOKEN: set the ci-ops App (CI_OPS_APP_ID + CI_OPS_APP_PRIVATE_KEY) "
                  "or the CI_OPS_TOKEN secret", file=sys.stderr)
            return 1
        gh = GitHub(token)

    webhook = os.environ.get("OPS_WEBHOOK_URL", "")
    if not args.dry_run and not webhook:
        print("::error::OPS_WEBHOOK_URL is not set; alerts could not be delivered", file=sys.stderr)
        return 1

    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")

    state = load_state(args.state)
    snap = collect(gh, now)
    if args.record:
        with open(args.record, "w") as fh:
            json.dump(world_from_snapshot(snap), fh, indent=1)
    for w in snap["warnings"]:
        print(f"::warning::{w}", file=sys.stderr)
    to_post, firing, new_state, _ = evaluate(snap, state, now, run_url)

    ok = True
    if args.dry_run:
        for a in to_post:
            print(f"--- would post [{a.key}]\n{a.text}\n")
        delivered = to_post
    else:
        delivered = deliver(to_post, webhook)
        if len(delivered) != len(to_post):
            ok = False
            print(f"::error::{len(to_post) - len(delivered)} alert(s) not delivered; they stay armed for the next run",
                  file=sys.stderr)
    mark_posted(new_state, delivered, now)
    save_state(args.state, new_state)

    text = summary_md(snap, firing, to_post, delivered, now)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Offline tests for watchdog.py. Run: python3 -m unittest discover -s scripts -v

Every test goes through the real collector against FakeGitHub, which serves
the REST endpoints from verbatim API objects (fixtures/shapes.json and
fixtures/incident1-2026-09-24.json), so URL building, pagination, the
rate-limit header and the rules are all exercised together.
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchdog as wd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
with open(os.path.join(FIX, "shapes.json")) as _fh:
    SHAPES = json.load(_fh)

T0 = wd.parse_ts("2026-09-24T16:00:00Z")


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def ts(minutes):
    return wd.iso(at(minutes))


class World:
    """Builds a FakeGitHub world out of the captured real objects."""

    _ids = iter(range(900000000, 999999999))

    def __init__(self, remaining=4500):
        self.data = {"rate": {"remaining": remaining, "limit": 5000, "reset": 1790280000},
                     "repos": {r: {"runs": [], "jobs": {}} for r in wd.REPOS}}
        self.data["repos"]["artifact-keeper-arc-runners"] = {
            "runs": [], "jobs": {}, "workflows": ["runner-version-check.yml", "runner-images.yml"]}
        # By default the dead-man checks and canaries are healthy, so each test
        # only sees the rule it is about.
        self.success_run("artifact-keeper-arc-runners", "runner-version-check.yml", at(-60))
        self.success_run("artifact-keeper-arc-runners", "runner-images.yml", at(-60 * 24))
        self.canary_ok = True

    def run(self, repo, created, path=".github/workflows/ci.yml", event="pull_request", branch="feature",
            status="queued", conclusion=None, updated=None, **extra):
        rid = next(self._ids)
        r = copy.deepcopy(SHAPES["run_queued"])
        r.update(id=rid, path=path, name=path.rsplit("/", 1)[-1], event=event, head_branch=branch,
                 status=status, conclusion=conclusion, created_at=wd.iso(created),
                 run_started_at=wd.iso(created), updated_at=wd.iso(updated or created),
                 html_url=f"https://github.com/artifact-keeper/{repo}/actions/runs/{rid}",
                 jobs_url=f"https://api.github.com/repos/artifact-keeper/{repo}/actions/runs/{rid}/jobs",
                 **extra)
        self.data["repos"][repo]["runs"].append(r)
        self.data["repos"][repo]["jobs"][str(rid)] = []
        return r

    def job(self, repo, run, labels, created, started=None, completed=None, conclusion=None):
        jid = next(self._ids)
        if started is None:
            j = copy.deepcopy(SHAPES["job_queued"])  # started_at == created_at, no runner
            j.update(created_at=wd.iso(created), started_at=wd.iso(created))
        else:
            j = copy.deepcopy(SHAPES["job_completed_hosted"])
            j.update(created_at=wd.iso(created), started_at=wd.iso(started),
                     status="completed" if completed else "in_progress",
                     conclusion=conclusion if completed else None,
                     completed_at=wd.iso(completed) if completed else None,
                     runner_name=f"{labels[0]}-runner-{jid}", runner_id=jid)
        j.update(id=jid, run_id=run["id"], labels=list(labels),
                 html_url=f"{run['html_url']}/job/{jid}")
        self.data["repos"][repo]["jobs"][str(run["id"])].append(j)
        # Keep the run's status/updated_at consistent with its jobs, as the API does.
        jobs = self.data["repos"][repo]["jobs"][str(run["id"])]
        if any(x["status"] == "queued" for x in jobs) and not any(x["status"] != "queued" for x in jobs):
            run["status"] = "queued"
        elif any(x["status"] != "completed" for x in jobs):
            run["status"] = "in_progress"
        else:
            run["status"] = "completed"
            run["conclusion"] = "failure" if any(x["conclusion"] == "failure" for x in jobs) else "success"
        times = [run["created_at"]] + [x["started_at"] for x in jobs if x.get("runner_name")] + \
                [x["completed_at"] for x in jobs if x.get("completed_at")]
        run["updated_at"] = max(times)
        return j

    def success_run(self, repo, workflow, finished, conclusion="success", branch="main", event="schedule",
                    head_sha="a" * 40):
        r = self.run(repo, finished - timedelta(minutes=5), path=f".github/workflows/{workflow}", event=event,
                     branch=branch, status="completed", conclusion=conclusion, updated=finished)
        r["head_sha"] = head_sha
        return r

    def queued_job(self, repo, label, created):
        r = self.run(repo, created)
        return self.job(repo, r, [label], created)

    def started_job(self, repo, label, created, started, completed=None):
        r = self.run(repo, created, status="in_progress")
        return self.job(repo, r, [label], created, started=started, completed=completed,
                        conclusion="success" if completed else None)

    def canary(self, label, created, started=None, completed=None, conclusion="success"):
        r = self.run("ci-ops", created, path=".github/workflows/runner-canary.yml", event="schedule",
                     branch="main", status="in_progress")
        return self.job("ci-ops", r, [label], created, started=started, completed=completed,
                        conclusion=conclusion)

    def healthy_canaries(self, now_min):
        for label in wd.CANARY_LABELS:
            self.canary(label, at(now_min - 20), started=at(now_min - 19), completed=at(now_min - 19))


def evaluate(world, minutes, state=None, healthy_canaries=True):
    data = copy.deepcopy(world.data)
    if healthy_canaries and world.canary_ok:
        w2 = World.__new__(World)
        w2.data = data
        w2.canary_ok = True
        w2.healthy_canaries(minutes)
    fake = wd.FakeGitHub(data, at(minutes))
    gh = wd.GitHub("t", transport=fake, sleep=lambda s: None)
    snap = wd.collect(gh, at(minutes))
    to_post, firing, new_state, evaluated = wd.evaluate(snap, state or {"posted": {}}, at(minutes))
    wd.mark_posted(new_state, to_post, at(minutes))
    return {"post": [a.key for a in to_post], "fire": [a.key for a in firing], "alerts": to_post,
            "state": new_state, "snap": snap, "fake": fake, "gh": gh}


def keys(result, prefix):
    return [k for k in result["fire"] if k.startswith(prefix)]


class LabelTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(wd.job_buckets({"labels": ["ubuntu-24.04-arm"]}), {"hosted"})
        self.assertEqual(wd.job_buckets({"labels": ["windows-latest"]}), {"hosted"})
        self.assertEqual(wd.job_buckets({"labels": ["macos-14"]}), {"hosted"})
        self.assertEqual(wd.job_buckets({"labels": ["ak-ci-runners"]}), {"ak-ci-runners"})
        self.assertEqual(wd.job_buckets({"labels": ["ak-nonexistent-runners"]}), {"ak-nonexistent-runners"})
        self.assertEqual(wd.job_buckets({"labels": ["self-hosted", "linux"]}), set())
        self.assertEqual(wd.job_buckets({"labels": []}), set())

    def test_queued_and_skipped_jobs_are_not_starts(self):
        # Real API shape: a queued job and a skipped job both carry started_at
        # (== created_at) and no runner_name.
        self.assertFalse(wd.has_started(SHAPES["job_queued"]))
        self.assertFalse(wd.has_started(SHAPES["job_skipped"]))
        self.assertTrue(wd.has_started(SHAPES["job_completed_hosted"]))
        self.assertEqual(SHAPES["job_queued"]["started_at"], SHAPES["job_queued"]["created_at"])


class StalledTests(unittest.TestCase):
    def test_fires_when_queued_over_15m_and_nothing_started(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        r = evaluate(w, 16)
        self.assertIn("STALLED:ak-ci-runners", r["post"])

    def test_not_before_15m(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        self.assertEqual(keys(evaluate(w, 15), "STALLED"), [])
        self.assertEqual(keys(evaluate(w, 14), "STALLED"), [])

    def test_not_when_a_job_started_recently(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        w.started_job("artifact-keeper-web", "ak-ci-runners", at(-30), at(10))
        self.assertEqual(keys(evaluate(w, 20), "STALLED"), [])

    def test_fires_when_last_start_older_than_15m(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        w.started_job("artifact-keeper", "ak-ci-runners", at(-30), at(2), completed=at(5))
        r = evaluate(w, 20)
        self.assertIn("STALLED:ak-ci-runners", r["post"])
        self.assertIn("last start 18m ago", r["alerts"][0].text)

    def test_start_older_than_60m_is_none(self):
        # Design: last_started = max(started_at) over jobs on L in the last 60 min, else none.
        w = World()
        w.queued_job("artifact-keeper", "ak-beefy-runners", at(0))
        w.started_job("artifact-keeper", "ak-beefy-runners", at(-80), at(-70))  # still running
        r = evaluate(w, 20)
        text = [a for a in r["alerts"] if a.key == "STALLED:ak-beefy-runners"][0].text
        self.assertIn("no start on this label in the last 60m", text)

    def test_start_in_old_run_recently_updated_counts(self):
        # Run created 3 h ago and already completed; its job started 8 min ago.
        # That run is neither queued, in progress, nor created in the last hour,
        # but the start must count, or a busy pool looks stalled.
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        j = w.started_job("artifact-keeper", "ak-ci-runners", at(-180), at(12), completed=at(18))
        run = w.data["repos"]["artifact-keeper"]["runs"][-1]
        self.assertEqual((run["status"], j["status"]), ("completed", "completed"))
        self.assertEqual(keys(evaluate(w, 20), "STALLED"), [])

    def test_other_labels_do_not_mask(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        w.started_job("artifact-keeper", "ak-docker-runners", at(0), at(18))
        w.started_job("artifact-keeper", "ubuntu-24.04", at(0), at(19))
        r = evaluate(w, 20)
        self.assertEqual(keys(r, "STALLED"), ["STALLED:ak-ci-runners"])

    def test_hosted_stall(self):
        w = World()
        w.queued_job("artifact-keeper-web", "ubuntu-latest", at(0))
        r = evaluate(w, 16)
        self.assertIn("STALLED:hosted", r["fire"])

    def test_nonexistent_label_from_test_stall_workflow(self):
        # PR 4 test (1): a job on ak-nonexistent-runners posts STALLED within 25 min.
        w = World()
        r0 = w.run("ci-ops", at(0), path=".github/workflows/test-stall.yml", event="workflow_dispatch",
                   branch="main")
        w.job("ci-ops", r0, ["ak-nonexistent-runners"], at(0))
        posted_at = None
        state = None
        for m in range(0, 30, 10):  # the */10 schedule, dispatched at :00
            res = evaluate(w, m, state)
            state = res["state"]
            if "STALLED:ak-nonexistent-runners" in res["post"] and posted_at is None:
                posted_at = m
        self.assertEqual(posted_at, 20)

    def test_same_dispatch_on_a_live_pool_is_quiet(self):
        # PR 4 test (1), second half: the same job on ak-ci-runners (which starts) posts nothing.
        w = World()
        r0 = w.run("ci-ops", at(0), path=".github/workflows/test-stall.yml", event="workflow_dispatch",
                   branch="main", status="in_progress")
        w.job("ci-ops", r0, ["ak-ci-runners"], at(0), started=at(1), completed=at(2), conclusion="success")
        for m in (10, 20, 30):
            res = evaluate(w, m)
            self.assertEqual(keys(res, "STALLED") + keys(res, "QUEUE_OLD"), [])

    def test_suppression_hourly_and_rearm_after_recovery(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        r1 = evaluate(w, 20)
        self.assertIn("STALLED:ak-ci-runners", r1["post"])
        r2 = evaluate(w, 30, r1["state"])
        self.assertIn("STALLED:ak-ci-runners", r2["fire"])
        self.assertNotIn("STALLED:ak-ci-runners", r2["post"])
        r3 = evaluate(w, 80, r2["state"])  # 60 min after the first post
        self.assertIn("STALLED:ak-ci-runners", r3["post"])
        # Recovery: the queue drains; the key is forgotten; a new stall posts at once.
        w2 = World()
        r4 = evaluate(w2, 90, r3["state"])
        self.assertNotIn("STALLED:ak-ci-runners", r4["state"]["posted"])
        w2.queued_job("artifact-keeper", "ak-ci-runners", at(90))
        r5 = evaluate(w2, 110, r4["state"])
        self.assertIn("STALLED:ak-ci-runners", r5["post"])


class QueueOldTests(unittest.TestCase):
    def test_self_hosted_threshold_30m(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-docker-runners", at(0))
        w.started_job("artifact-keeper", "ak-docker-runners", at(0), at(29))  # busy, not stalled
        self.assertEqual(keys(evaluate(w, 30), "QUEUE_OLD"), [])
        r = evaluate(w, 31)
        self.assertEqual(keys(r, "QUEUE_OLD"), ["QUEUE_OLD:ak-docker-runners"])
        self.assertEqual(keys(r, "STALLED"), [])

    def test_hosted_threshold_10m(self):
        w = World()
        w.queued_job("artifact-keeper-web", "ubuntu-latest", at(0))
        w.started_job("artifact-keeper-web", "ubuntu-latest", at(0), at(9))
        self.assertEqual(keys(evaluate(w, 10), "QUEUE_OLD"), [])
        self.assertEqual(keys(evaluate(w, 11), "QUEUE_OLD"), ["QUEUE_OLD:hosted"])

    def test_once_per_hour_per_label(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-e2e-runners", at(0))
        w.queued_job("artifact-keeper", "ak-beefy-runners", at(0))
        r1 = evaluate(w, 40)
        self.assertIn("QUEUE_OLD:ak-e2e-runners", r1["post"])
        self.assertIn("QUEUE_OLD:ak-beefy-runners", r1["post"])
        r2 = evaluate(w, 50, r1["state"])
        self.assertEqual([k for k in r2["post"] if k.startswith("QUEUE_OLD")], [])
        r3 = evaluate(w, 100, r2["state"])
        self.assertIn("QUEUE_OLD:ak-e2e-runners", r3["post"])

    def test_message_names_rule_label_numbers_links(self):
        w = World()
        j = w.queued_job("artifact-keeper", "ak-e2e-runners", at(0))
        w.queued_job("artifact-keeper-test", "ak-e2e-runners", at(5))
        text = [a for a in evaluate(w, 40)["alerts"] if a.key == "QUEUE_OLD:ak-e2e-runners"][0].text
        self.assertIn("**QUEUE_OLD** `ak-e2e-runners`", text)
        self.assertIn("2 job(s) queued", text)
        self.assertIn("oldest 40m", text)
        self.assertIn(j["html_url"], text)
        self.assertIn("https://github.com/artifact-keeper/artifact-keeper-test/actions?query=is%3Aqueued", text)


class CanaryTests(unittest.TestCase):
    def test_canary_supplies_demand_on_an_idle_dead_pool(self):
        # No real work anywhere; the pool is dead. The 16:30 canary queues on
        # ak-ci-runners and never starts: STALLED fires at the 16:50 run, and
        # CANARY_STALE does not (the label has a queued job).
        w = World()
        w.canary_ok = False
        for label in ("ak-docker-runners", "ak-e2e-runners"):
            w.canary(label, at(30), started=at(31), completed=at(31))
        w.canary("ak-ci-runners", at(-45), started=at(-44), completed=at(-44))  # last success 94 min before 16:50
        w.canary("ak-ci-runners", at(30))                                  # 16:30 canary never starts
        self.assertEqual(keys(evaluate(w, 40), "STALLED"), [])
        r = evaluate(w, 50)
        self.assertIn("STALLED:ak-ci-runners", r["post"])
        self.assertEqual(keys(r, "CANARY_STALE"), [])
        stalled = [a for a in r["alerts"] if a.key == "STALLED:ak-ci-runners"][0].text
        self.assertIn("ci-ops 1", stalled)

    def test_canary_stale_fires_when_nothing_queued(self):
        w = World()
        w.canary_ok = False
        w.canary("ak-ci-runners", at(-100), started=at(-99), completed=at(-99))   # 99 min ago
        w.canary("ak-docker-runners", at(-30), started=at(-29), completed=at(-29))
        w.canary("ak-e2e-runners", at(-30), started=at(-29), completed=at(-28), conclusion="failure")
        r = evaluate(w, 0)
        self.assertEqual(sorted(keys(r, "CANARY_STALE")),
                         ["CANARY_STALE:ak-ci-runners", "CANARY_STALE:ak-e2e-runners"])

    def test_canary_ok_within_90m(self):
        w = World()
        w.canary_ok = False
        for label in wd.CANARY_LABELS:
            w.canary(label, at(-89), started=at(-89), completed=at(-89))
        self.assertEqual(keys(evaluate(w, 0), "CANARY_STALE"), [])

    def test_canary_workflow_missing_counts_as_stale(self):
        w = World()
        w.canary_ok = False
        r = evaluate(w, 0)
        self.assertEqual(len(keys(r, "CANARY_STALE")), 3)
        self.assertEqual(r["snap"]["errors"], [])


class MainRedTests(unittest.TestCase):
    def test_fires_once_per_sha(self):
        w = World()
        w.success_run("artifact-keeper", "ci.yml", at(-5), conclusion="failure", event="push", head_sha="b" * 40)
        r1 = evaluate(w, 0)
        self.assertEqual(r1["post"], [f"MAIN_RED:artifact-keeper:{'b' * 40}"])
        self.assertIn("/actions/runs/", r1["alerts"][0].text)
        r2 = evaluate(w, 10, r1["state"])
        self.assertEqual(r2["post"], [])
        r3 = evaluate(w, 60 * 30, r2["state"])  # even much later: once per sha
        self.assertNotIn(f"MAIN_RED:artifact-keeper:{'b' * 40}", r3["post"])
        w.success_run("artifact-keeper", "ci.yml", at(15), conclusion="failure", event="push", head_sha="c" * 40)
        r4 = evaluate(w, 20, r2["state"])
        self.assertEqual(r4["post"], [f"MAIN_RED:artifact-keeper:{'c' * 40}"])

    def test_cancelled_newest_run_is_not_red(self):
        w = World()
        w.success_run("artifact-keeper", "ci.yml", at(-5), conclusion="cancelled", event="push")
        self.assertEqual(keys(evaluate(w, 0), "MAIN_RED"), [])

    def test_only_newest_completed_push_run_counts(self):
        w = World()
        w.success_run("artifact-keeper-web", "ci.yml", at(-30), conclusion="failure", event="push")
        w.success_run("artifact-keeper-web", "ci.yml", at(-5), conclusion="success", event="push", head_sha="d" * 40)
        w.success_run("artifact-keeper-iac", "ci.yml", at(-5), conclusion="failure", event="pull_request")
        w.success_run("artifact-keeper-iac", "ci.yml", at(-5), conclusion="failure", event="push", branch="release/1.0")
        self.assertEqual(keys(evaluate(w, 0), "MAIN_RED"), [])

    def test_not_watched_for_other_repos(self):
        w = World()
        w.success_run("artifact-keeper-test", "ci.yml", at(-5), conclusion="failure", event="push")
        self.assertEqual(keys(evaluate(w, 0), "MAIN_RED"), [])


class ForkWaitingTests(unittest.TestCase):
    def _ar(self, w, created):
        r = copy.deepcopy(SHAPES["run_action_required"])
        r.update(id=next(World._ids), created_at=wd.iso(created), updated_at=wd.iso(created),
                 run_started_at=wd.iso(created))
        w.data["repos"]["artifact-keeper"]["runs"].append(r)
        return r

    def test_fires_after_4h_once_per_day(self):
        w = World()
        self._ar(w, at(-3 * 60))
        self.assertEqual(keys(evaluate(w, 0), "FORK"), [])
        self._ar(w, at(-5 * 60))
        r1 = evaluate(w, 0)
        self.assertEqual(r1["post"], ["FORK_WAITING"])
        self.assertIn("1 run(s) in `action_required`", r1["alerts"][0].text)
        self.assertIn(SHAPES["run_action_required"]["head_repository"]["full_name"], r1["alerts"][0].text)
        self.assertEqual(evaluate(w, 60 * 23, r1["state"])["post"], [])
        self.assertEqual(evaluate(w, 60 * 24, r1["state"])["post"], ["FORK_WAITING"])


class DeadmanTests(unittest.TestCase):
    def _world(self, vc_age_h=None, img_age_d=None, vc_missing=False):
        w = World()
        arc = w.data["repos"]["artifact-keeper-arc-runners"]
        arc["runs"], arc["jobs"] = [], {}
        if vc_missing:
            arc["workflows"] = ["runner-images.yml"]
        if vc_age_h is not None:
            w.success_run("artifact-keeper-arc-runners", "runner-version-check.yml", at(-vc_age_h * 60))
        if img_age_d is not None:
            w.success_run("artifact-keeper-arc-runners", "runner-images.yml", at(-img_age_d * 24 * 60))
        return w

    def test_version_check_48h(self):
        self.assertEqual(keys(evaluate(self._world(47, 1), 0), "DEADMAN"), [])
        r = evaluate(self._world(49, 1), 0)
        self.assertEqual(r["post"], ["DEADMAN:runner-version-check"])
        self.assertIn("last success 49h00m ago", r["alerts"][0].text)

    def test_runner_images_8d(self):
        self.assertEqual(keys(evaluate(self._world(1, 7.9), 0), "DEADMAN"), [])
        self.assertEqual(keys(evaluate(self._world(1, 8.1), 0), "DEADMAN"), ["DEADMAN:runner-images"])

    def test_failed_runs_do_not_count_as_success(self):
        w = self._world(49, 1)
        w.success_run("artifact-keeper-arc-runners", "runner-version-check.yml", at(-60), conclusion="failure")
        self.assertEqual(keys(evaluate(w, 0), "DEADMAN"), ["DEADMAN:runner-version-check"])

    def test_workflow_not_found_fires_and_is_not_degraded(self):
        r = evaluate(self._world(None, 1, vc_missing=True), 0)
        self.assertEqual(r["post"], ["DEADMAN:runner-version-check"])
        self.assertIn("not found", r["alerts"][0].text)
        self.assertEqual(r["snap"]["errors"], [])

    def test_daily_repeat_and_rearm(self):
        r1 = evaluate(self._world(49, 1), 0)
        self.assertEqual(evaluate(self._world(59, 1), 10 * 60, r1["state"])["post"], [])
        self.assertEqual(evaluate(self._world(73, 1), 24 * 60, r1["state"])["post"], ["DEADMAN:runner-version-check"])
        r2 = evaluate(self._world(1, 1), 60, r1["state"])
        self.assertNotIn("DEADMAN:runner-version-check", r2["state"]["posted"])


class RateTests(unittest.TestCase):
    def test_threshold(self):
        self.assertEqual(keys(evaluate(World(remaining=1000), 0), "RATE"), [])
        r = evaluate(World(remaining=999), 0)
        self.assertEqual(r["post"], ["RATE"])
        self.assertIn("X-RateLimit-Remaining 999 of 5000", r["alerts"][0].text)


class ApiDegradedTests(unittest.TestCase):
    def _stalled_world(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        w.queued_job("artifact-keeper-web", "ubuntu-latest", at(0))
        return w

    def test_5xx_posts_once_and_skips_queue_checks(self):
        w = self._stalled_world()
        w.data["fail"] = [{"path_re": r"/actions/runs/\d+/jobs$", "status": 502}]
        r1 = evaluate(w, 40)
        self.assertEqual(r1["post"], ["API_DEGRADED"])
        for rule in ("STALLED", "QUEUE_OLD", "CANARY_STALE"):
            self.assertEqual(keys(r1, rule), [], rule)
        self.assertIn("502", r1["alerts"][0].text)
        self.assertNotIn("Bearer", r1["alerts"][0].text)
        # Still degraded: no second post.
        r2 = evaluate(w, 50, r1["state"])
        self.assertEqual(r2["post"], [])
        # Clean run: re-armed, and the queue checks run again.
        del w.data["fail"]
        r3 = evaluate(w, 60, r2["state"])
        self.assertNotIn("API_DEGRADED", r3["state"]["posted"])
        self.assertIn("STALLED:ak-ci-runners", r3["post"])

    def test_error_outside_the_queue_endpoints_still_skips_queue_checks(self):
        # The queue data itself arrived intact; "any 5xx" still means skip.
        w = self._stalled_world()
        w.data["fail"] = [{"path_re": "/workflows/runner-images.yml/runs$", "status": 503}]
        r = evaluate(w, 40)
        self.assertIsNotNone(r["snap"]["jobs"])
        self.assertIn("API_DEGRADED", r["post"])
        for rule in ("STALLED", "QUEUE_OLD", "CANARY_STALE"):
            self.assertEqual(keys(r, rule), [], rule)

    def test_403_is_degraded(self):
        w = self._stalled_world()
        w.data["fail"] = [{"path_re": r"/repos/artifact-keeper/artifact-keeper-web/actions/runs$", "status": 403}]
        r = evaluate(w, 40)
        self.assertIn("API_DEGRADED", r["post"])
        self.assertEqual(keys(r, "STALLED"), [])

    def test_degraded_keeps_queue_suppression_state(self):
        w = self._stalled_world()
        r1 = evaluate(w, 20)
        self.assertIn("STALLED:ak-ci-runners", r1["state"]["posted"])
        w.data["fail"] = [{"path_re": "/jobs$", "status": 500}]
        r2 = evaluate(w, 30, r1["state"])
        self.assertIn("STALLED:ak-ci-runners", r2["state"]["posted"])

    def test_non_queue_rules_still_evaluated_when_their_data_arrived(self):
        w = self._stalled_world()
        w.success_run("artifact-keeper", "ci.yml", at(-5), conclusion="failure", event="push")
        w.data["fail"] = [{"path_re": "/jobs$", "status": 503}]
        r = evaluate(w, 40)
        self.assertIn("API_DEGRADED", r["post"])
        self.assertEqual(len(keys(r, "MAIN_RED")), 1)

    def test_single_transient_5xx_is_retried(self):
        w = self._stalled_world()
        calls = {"n": 0}
        fake = wd.FakeGitHub(copy.deepcopy(w.data), at(20))

        def flaky(method, url, headers, body=None, timeout=20):
            calls["n"] += 1
            if calls["n"] == 1:
                return 503, {}, b"{}"
            return fake(method, url, headers, body, timeout)

        snap = wd.collect(wd.GitHub("t", transport=flaky, sleep=lambda s: None), at(20))
        self.assertEqual(snap["errors"], [])

    def test_network_error_is_degraded(self):
        def down(method, url, headers, body=None, timeout=20):
            raise OSError("connection reset")

        snap = wd.collect(wd.GitHub("t", transport=down, sleep=lambda s: None), at(20))
        self.assertTrue(snap["errors"])
        to_post, _, _, _ = wd.evaluate(snap, {"posted": {}}, at(20))
        self.assertEqual([a.key for a in to_post], ["API_DEGRADED"])


class Incident1ReplayTests(unittest.TestCase):
    """Real data: jobs on ak-ci-runners queued from 16:25:29 on 2026-09-24, the
    last ak-ci start 09-23 18:13. STALLED must post at the 16:50 run, not at
    16:30 or 16:40."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(FIX, "incident1-2026-09-24.json")) as fh:
            cls.world = json.load(fh)

    def _eval(self, hhmm, state=None):
        now = wd.parse_ts(f"2026-09-24T{hhmm}:00Z")
        gh = wd.GitHub("t", transport=wd.FakeGitHub(copy.deepcopy(self.world), now), sleep=lambda s: None)
        snap = wd.collect(gh, now)
        to_post, firing, new_state, _ = wd.evaluate(snap, state or {"posted": {}}, now)
        wd.mark_posted(new_state, to_post, now)
        return snap, to_post, firing, new_state

    def test_stalled_at_1650_not_before(self):
        state = None
        first = None
        for hhmm in ("16:30", "16:40", "16:50", "17:00"):
            snap, to_post, firing, state = self._eval(hhmm, state)
            self.assertEqual(snap["errors"], [])
            if first is None and any(a.key == "STALLED:ak-ci-runners" for a in to_post):
                first = hhmm
        self.assertEqual(first, "16:50")

    def test_1650_details(self):
        snap, to_post, firing, _ = self._eval("16:50")
        stalled = [a for a in firing if a.rule == "STALLED"]
        self.assertEqual([a.key for a in stalled], ["STALLED:ak-ci-runners"])  # docker still starting
        text = stalled[0].text
        self.assertIn("25 job(s) queued", text)
        self.assertIn("oldest 24m", text)
        self.assertIn("09-24 16:25:29Z", text)
        self.assertIn("no start on this label in the last 60m", text)
        # The 09-23 18:13 start exists in the fixture but is outside every window.
        synth = self.world["repos"]["artifact-keeper"]["jobs"]["35990000001"][0]
        self.assertEqual(synth["started_at"], "2026-09-23T18:13:00Z")

    def test_1640_is_below_threshold(self):
        _, _, firing, _ = self._eval("16:40")
        self.assertFalse(any(a.key == "STALLED:ak-ci-runners" for a in firing))

    def test_queue_old_follows_at_1700(self):
        _, to_post, _, _ = self._eval("17:00")
        self.assertIn("QUEUE_OLD:ak-ci-runners", [a.key for a in to_post])


class CollectorTests(unittest.TestCase):
    def test_pagination_follows_link_header(self):
        w = World()
        for i in range(150):
            w.queued_job("artifact-keeper", "ak-ci-runners", at(i / 10))
        r = evaluate(w, 40)
        self.assertEqual(len(r["snap"]["jobs"]), 150 + 3)  # + three canary jobs
        self.assertTrue(any("page=2" in q for q in r["fake"].requests))
        self.assertIn("150 job(s) queued", [a for a in r["alerts"] if a.key == "STALLED:ak-ci-runners"][0].text)

    def test_jobs_requested_with_filter_latest(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        r = evaluate(w, 5)
        job_reqs = [q for q in r["fake"].requests if q.split("?")[0].endswith("/jobs")]
        self.assertTrue(job_reqs)
        self.assertTrue(all("filter=latest" in q for q in job_reqs))

    def test_missing_repo_is_a_warning_not_a_crash(self):
        w = World()
        del w.data["repos"]["artifact-keeper-test"]
        r = evaluate(w, 0)
        self.assertEqual(r["snap"]["errors"], [])
        self.assertTrue(any("artifact-keeper-test" in x for x in r["snap"]["warnings"]))

    def test_call_budget(self):
        # Design: about 20-40 calls per run when quiet.
        r = evaluate(World(), 0)
        self.assertLessEqual(r["gh"].calls, 40)


class DeliveryTests(unittest.TestCase):
    def _capture(self, status=204):
        sent = []

        def transport(method, url, headers, body=None, timeout=20):
            sent.append((url, json.loads(body)))
            return status, {}, b""
        return sent, transport

    def test_native_discord_payload(self):
        sent, tr = self._capture()
        self.assertTrue(wd.post_webhook("https://discord.com/api/webhooks/1/x", "hi", tr, lambda s: None))
        self.assertEqual(sent[0][1]["content"], "hi")
        self.assertEqual(sent[0][1]["allowed_mentions"], {"parse": []})

    def test_slack_compatible_payload(self):
        sent, tr = self._capture()
        wd.post_webhook("https://discord.com/api/webhooks/1/x/slack", "hi", tr, lambda s: None)
        self.assertEqual(sent[0][1], {"text": "hi"})

    def test_chunks_stay_under_discord_limit(self):
        texts = ["x" * 700] * 5 + ["y" * 5000]
        chunks = wd.chunk_messages(texts)
        self.assertTrue(all(len(c) <= 2000 for c, _ in chunks))
        self.assertEqual(sorted(i for _, idx in chunks for i in idx), list(range(6)))

    def test_rate_limited_webhook_retries(self):
        seq = [(429, b'{"retry_after": 0.1}'), (204, b"")]

        def tr(method, url, headers, body=None, timeout=20):
            s, b = seq.pop(0)
            return s, {}, b
        self.assertTrue(wd.post_webhook("https://x/y", "hi", tr, lambda s: None))

    def test_undelivered_alerts_stay_armed(self):
        _, tr = self._capture(status=400)
        alerts = [wd.Alert("RATE", "RATE", "t")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(wd.deliver(alerts, "https://x/y", tr, lambda s: None), [])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "s.json")
        self.env = dict(os.environ)
        for k in ("OPS_WEBHOOK_URL", "GITHUB_STEP_SUMMARY", "GITHUB_RUN_ID"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def _main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = wd.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_fixture_dry_run_end_to_end(self):
        fixture = os.path.join(FIX, "incident1-2026-09-24.json")
        rc, out, _ = self._main("--fixture", fixture, "--now", "2026-09-24T16:50:00Z", "--dry-run",
                                "--state", self.state)
        self.assertEqual(rc, 0)
        self.assertIn("would post [STALLED:ak-ci-runners]", out)
        with open(self.state) as fh:
            self.assertIn("STALLED:ak-ci-runners", json.load(fh)["posted"])
        rc, out, _ = self._main("--fixture", fixture, "--now", "2026-09-24T17:00:00Z", "--dry-run",
                                "--state", self.state)
        self.assertNotIn("would post [STALLED:ak-ci-runners]", out)

    def test_degraded_run_still_exits_0(self):
        w = World()
        w.data["fail"] = [{"path_re": "/actions/runs$", "status": 503}]
        path = os.path.join(self.tmp.name, "w.json")
        with open(path, "w") as fh:
            json.dump(w.data, fh)
        rc, out, _ = self._main("--fixture", path, "--now", ts(0), "--dry-run", "--state", self.state)
        self.assertEqual(rc, 0)
        self.assertIn("would post [API_DEGRADED]", out)

    def test_missing_webhook_fails(self):
        path = os.path.join(FIX, "incident1-2026-09-24.json")
        rc, _, err = self._main("--fixture", path, "--now", "2026-09-24T16:50:00Z", "--state", self.state)
        self.assertEqual(rc, 1)
        self.assertIn("OPS_WEBHOOK_URL", err)

    def test_missing_token_fails(self):
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        rc, _, err = self._main("--dry-run", "--state", self.state)
        self.assertEqual(rc, 1)
        self.assertIn("GH_TOKEN", err)

    def test_corrupt_state_starts_empty(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            fh.write("{not json")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(wd.load_state(self.state)["posted"], {})

    def test_record_round_trip(self):
        w = World()
        w.queued_job("artifact-keeper", "ak-ci-runners", at(0))
        src = os.path.join(self.tmp.name, "w.json")
        rec = os.path.join(self.tmp.name, "rec.json")
        with open(src, "w") as fh:
            json.dump(w.data, fh)
        self._main("--fixture", src, "--now", ts(20), "--dry-run", "--state", self.state, "--record", rec)
        rc, out, _ = self._main("--fixture", rec, "--now", ts(20), "--dry-run",
                                "--state", os.path.join(self.tmp.name, "s2.json"))
        self.assertIn("would post [STALLED:ak-ci-runners]", out)


if __name__ == "__main__":
    unittest.main()

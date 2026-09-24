# ci-ops

Off-node CI health for the artifact-keeper org: a queue watchdog and a runner canary.

Nothing here runs on rocky except the canary jobs themselves. The watchdog runs on
GitHub-hosted runners, reads GitHub's own view of the Actions queue, and posts to the ops
Discord channel. It exists because incident 1 (runner 2.335.1 rejected on 2026-09-24,
`ak-ci-runners` jobs queued from 16:25:29 with nothing starting) went unnoticed for 42
minutes: nothing measured "jobs queued, nothing starting", and the only guard lived on the
node it was guarding. The watchdog is itself dead-manned by healthchecks.io.

Design: `ci-architecture-final.md` section 3.4 (Detection) and section 5, PR 4.

| File | What it does |
|---|---|
| `.github/workflows/ci-watchdog.yml` | Every 10 min on `ubuntu-24.04`: evaluates the rules below and posts to `OPS_WEBHOOK_URL`, then pings `HC_PING_URL` (`/fail` if the evaluation failed). |
| `.github/workflows/runner-canary.yml` | Every 30 min: one `echo "$RUNNER_VERSION"` job on each of `ak-ci-runners`, `ak-docker-runners` and `ak-e2e-runners`. It is synthetic demand, so STALLED can fire on an idle pool. It drives nothing. |
| `.github/workflows/test-stall.yml` | Manual probe job on any label, for the acceptance test below. |
| `.github/workflows/test.yml` | Unit tests on push and PR. |
| `scripts/watchdog.py` | The watchdog. Python 3 standard library only (`urllib`). |
| `scripts/test_watchdog.py` | Offline tests: every rule firing and not firing, and a replay of incident 1. |
| `scripts/fixtures/` | Verbatim Actions API objects: the 2026-09-24 incident snapshot and single-object shapes. |

## What is watched

- **Repos:** `artifact-keeper`, `artifact-keeper-web`, `artifact-keeper-iac`, `artifact-keeper-test`, `ci-ops`.
  DEADMAN also reads `artifact-keeper-arc-runners` (private).
- **Labels:** `ak-ci-runners`, `ak-docker-runners`, `ak-e2e-runners`, `ak-beefy-runners`, and `hosted`.
  `hosted` is any job label matching `ubuntu-*`, `windows-*` or `macos-*`. Any other `ak-*` label that
  appears on a job is watched too, which is how the `ak-nonexistent-runners` test works.
- **Data:** each run lists the runs that are `queued`, the runs that are `in_progress`, and the runs
  created in the last 6 h. It then reads the jobs (`filter=latest`) of every run that is still active
  or was updated in the last 60 min. The design says "runs created in the last hour". The wider window
  matters because a job started 10 minutes ago inside a run created 2 hours ago is still a start. A run
  not updated in 60 min cannot hold a start inside the 60-min window.
- **Cost:** about 30 calls on a quiet run and about 110 on a busy one (measured 2026-09-24 18:05,
  with about 70 active runs). That is under 700 calls/hour, from the App's own budget of 5,000/hour.

## Rules

Definitions, for each label `L`:

- `queued[L]`: jobs with `status == queued` whose labels include `L`.
- `oldest_queued[L]`: `now` minus the earliest `created_at` in `queued[L]`.
- `last_started[L]`: the latest `started_at` among the in-progress or completed jobs on `L` that
  started in the last 60 min, or none. A job counts as started only if it has a `runner_name`. The API
  gives a queued job, and a skipped one, a `started_at` equal to its `created_at` and no runner.

| Rule | Fires when | Repeats |
|---|---|---|
| **STALLED** (L) | `queued[L] > 0` and `oldest_queued[L] > 15m` and (`last_started[L]` is none or more than 15m ago) | Hourly while it persists. Re-arms once the stall clears, so a new stall posts at once. |
| **QUEUE_OLD** (L) | `oldest_queued[L] > 30m` for `ak-*`, `> 10m` for `hosted` | At most once per hour per label (design). |
| **CANARY_STALE** (L) | No successful `runner-canary` job on `L` in 90m, and `queued[L] == 0` (so load is excluded) | Every 6h while it persists. Re-arms on recovery. |
| **MAIN_RED** (repo) | The newest completed `push` run of `ci.yml` on `main` has `conclusion == failure` (backend, web, iac) | Once per `head_sha` (design). |
| **FORK_WAITING** | Any run in `action_required` older than 4h | Once per day (design). |
| **DEADMAN** | `runner-version-check.yml` in artifact-keeper-arc-runners has no success on `main` in 48h, or `runner-images.yml` has none in 8d. A missing workflow counts as firing. | Daily while it persists. Re-arms on recovery. |
| **RATE** | `X-RateLimit-Remaining < 1000` (lowest value seen during the run) | Hourly. |
| **API_DEGRADED** | Any 5xx, 403, 429 or network error from the API, after one retry for 5xx and network errors | Posts once, then re-arms after a clean run. That run skips STALLED, QUEUE_OLD and CANARY_STALE, so nothing pages on stale data. Rules whose own data arrived still run. |

Each post names the rule, the label or repo, and the numbers (count, oldest age and creation time, last
start). It also links the oldest job, the repos' queued-runs pages and the watchdog run.

Suppression state is a small JSON file restored from `actions/cache` under the rolling key
`ci-watchdog-state-<run_id>-<attempt>` (prefix `ci-watchdog-state-`). Each run restores the newest
entry and saves a new one. On a cache miss the state starts empty, and the worst case is one repeated
post. An alert whose post failed is not recorded, so it posts again on the next run.

Exit status and healthchecks.io: the run pings `HC_PING_URL` when the evaluation completed and every
post was delivered, including a run that found the API degraded and said so. It pings
`HC_PING_URL/fail` when the script crashed, the token or webhook is missing, a post failed, or the
job was cancelled. When the pings stop (grace 30 min), healthchecks.io pages.

## Setup (maintainer)

1. **Fork-PR approval.** This public repo runs jobs on the rocky self-hosted labels, including the
   privileged DinD pool. Raise its policy to match the other repos:
   `gh api -X PUT repos/artifact-keeper/ci-ops/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors`
   It was `first_time_contributors_new_to_github` on 2026-09-24.
2. **GitHub App `ak-ci-ops`**, owned by the artifact-keeper org:
   - Repository permissions: **Actions: Read-only** and **Metadata: Read-only**. Nothing else.
     (Design 0.4 later adds organization "Self-hosted runners: Read and write" for ARC in Phase 2. The
     watchdog does not need it.)
   - Webhook: inactive. No events.
   - "Where can this GitHub App be installed?": only on this account.
   - Install it on **All repositories**. The watchdog needs the private
     artifact-keeper-arc-runners repo too.
   - Generate a private key.
3. **Repository settings** in `artifact-keeper/ci-ops` (Settings, Secrets and variables, Actions):

   | Name | Kind | Value |
   |---|---|---|
   | `CI_OPS_APP_ID` | variable | The App's client ID (`Iv23...`, preferred) or its numeric App ID |
   | `CI_OPS_APP_PRIVATE_KEY` | secret | The full `.pem` contents |
   | `CI_OPS_TOKEN` | secret | Fallback only, used while the App does not exist: a fine-grained PAT, resource owner artifact-keeper, all repositories, Actions read (Metadata read is implied). Delete it once the App works. |
   | `OPS_WEBHOOK_URL` | secret | The Discord webhook that Alertmanager's `ops` receiver uses. A URL ending in `/slack` (Alertmanager's form) and the plain webhook URL both work. |
   | `HC_PING_URL` | secret | The healthchecks.io ping URL for a check with period 10 min and grace 30 min |

   ```sh
   gh variable set CI_OPS_APP_ID -R artifact-keeper/ci-ops --body '<client id>'
   gh secret set CI_OPS_APP_PRIVATE_KEY -R artifact-keeper/ci-ops < ak-ci-ops.private-key.pem
   gh secret set OPS_WEBHOOK_URL -R artifact-keeper/ci-ops     # paste when prompted
   gh secret set HC_PING_URL -R artifact-keeper/ci-ops
   ```
4. **Runner group.** The rocky labels must accept jobs from this repo. The public backend repo already
   runs on them, so public repos are allowed. If the ARC runner group is limited to selected
   repositories, add `ci-ops`. Check with
   `gh api orgs/artifact-keeper/actions/runner-groups` (this needs `admin:org`).
5. After merging, run `gh workflow run runner-canary.yml -R artifact-keeper/ci-ops` once, so CANARY_STALE
   has a baseline. Then run `gh workflow run ci-watchdog.yml -R artifact-keeper/ci-ops` and read the
   job summary.

GitHub disables scheduled workflows in a public repo after 60 days without repository activity.
healthchecks.io pages when that happens. Re-enable with `gh workflow enable ci-watchdog.yml -R artifact-keeper/ci-ops`
(and do the same for `runner-canary.yml`).

## Acceptance tests (design section 5, PR 4)

1. **STALLED on a dead label.**
   `gh workflow run test-stall.yml -R artifact-keeper/ci-ops -f label=ak-nonexistent-runners`.
   Within 25 minutes, Discord must show `**STALLED** \`ak-nonexistent-runners\``. It fires at the first
   watchdog run more than 15 minutes after dispatch. GitHub often starts scheduled runs a few minutes
   late, so if it misses 25 minutes, check the `ci-watchdog` run times before you suspect the rules.
   Then cancel the probe:
   `gh run list -R artifact-keeper/ci-ops -w test-stall.yml -L 1 --json databaseId -q '.[0].databaseId' | xargs gh run cancel -R artifact-keeper/ci-ops`.
   Repeat with `-f label=ak-ci-runners`: the job runs and nothing is posted.
2. **The dead-man pages.** `gh workflow disable ci-watchdog.yml -R artifact-keeper/ci-ops` for 40 minutes.
   healthchecks.io must page 40 minutes after the last successful ping (period 10 plus grace 30), so wait
   for the page, not the clock. Then run
   `gh workflow enable ci-watchdog.yml -R artifact-keeper/ci-ops`.
3. **The rate limit holds.** After 24 hours, check that the App's `X-RateLimit-Remaining` never went
   below 3,000. Each watchdog run prints `rate remaining` in its job summary. To list the last day of
   runs:
   `gh run list -R artifact-keeper/ci-ops -w ci-watchdog.yml -L 150 --json databaseId,conclusion`,
   then read the summaries, or grep the logs:
   `gh run view <id> -R artifact-keeper/ci-ops --log | grep 'rate remaining'`.
   RATE also posts on its own below 1,000.

## Running locally

```sh
python3 -m unittest discover -s scripts -v            # offline tests

# Replay incident 1 from the recorded snapshot (no network, prints instead of posting)
python3 scripts/watchdog.py --fixture scripts/fixtures/incident1-2026-09-24.json \
  --now 2026-09-24T16:50:00Z --dry-run --state /tmp/wd-state.json

# Live and read-only, printing instead of posting; --record saves what it fetched as a fixture
GH_TOKEN=$(gh auth token) python3 scripts/watchdog.py --dry-run --state /tmp/wd-live.json --record /tmp/world.json
```

A fixture is a "world" file: runs and jobs exactly as the REST API returns them, keyed by repo.
`FakeGitHub` in `watchdog.py` serves `GET .../actions/runs`, `.../workflows/<file>/runs` and
`.../runs/<id>/jobs` from it, including the `status`, `created`, `branch` and `event` filters and
`Link` pagination. So the collector, the rules and the suppression all run as they do live. With
`"rewind": true`, the world is shown as the API would have shown it at `--now`: later jobs are
removed, and jobs that started later are put back in the queue. `"fail": [{"path_re": ..., "status": 503}]`
injects API errors.

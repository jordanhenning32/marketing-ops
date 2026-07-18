# Shadow Edge Hermes Operations Runbook

Hermes is the dry-run/manual-safe marketing operations system for Shadow Edge Tools. It creates daily campaign packs, queues compliant manual distribution kits, manages consented lead nurture queues, imports measured analytics, and now runs a first-class deterministic agent team.

Live email sending and API publishing remain disabled unless explicit config gates, credentials, official API support, approvals, and compliance all pass.

## Quick Start

From `E:/marketing-ops`:

```bat
python scripts\autopilot.py --dry-run
python scripts\hermes_team.py --dry-run
run-autopilot.bat --dry-run
run-hermes-team.bat --dry-run
```

Run one agent:

```bat
python scripts\hermes_team.py --only scout --dry-run
python scripts\hermes_team.py --only strategy --dry-run
python scripts\hermes_team.py --only creative --dry-run
python scripts\hermes_team.py --only compliance --dry-run
python scripts\hermes_team.py --only distribution --dry-run
python scripts\hermes_team.py --only email --dry-run
python scripts\hermes_team.py --only analytics --dry-run
python scripts\hermes_team.py --only optimizer --dry-run
```

Use `--date YYYY-MM-DD` for deterministic backfills. Use `--force` only when you intentionally want to regenerate today's campaign instead of reusing an existing one.

## Agent Team

The team runner executes agents in this order:

1. Scout Agent: writes `briefings/YYYY-MM-DD.md`; can fetch configured public RSS/http sources with `--network`; falls back to safe manual-check guidance when credentials/network are unavailable.
2. Strategy Agent: chooses/reuses the daily campaign, writes `content/YYYY-MM-DD-slug/00-campaign-brief.md`, and records `strategy-decision.json` with queue/backlog/tracking context.
3. Creative Agent: checks/produces channel assets from the campaign pack, writes `creative-review.json`, and surfaces placeholder brand-voice, missing UTM, duplicate-copy, or source/timestamp risks.
4. Compliance Agent: runs `scripts/compliance.py` and writes both `08-compliance-check.md` and `08-compliance-check.json`.
5. Distribution Agent: refuses failed/unknown compliance; otherwise updates `state/publish-queue.jsonl` and manual kits.
6. Email Agent: refuses failed/unknown compliance; otherwise queues consented lead nurture steps idempotently.
7. Analytics Agent: writes `daily-ops/performance-YYYY-MM-DD.md` from measured/imported data only.
8. Optimizer Agent: writes `daily-ops/optimization-YYYY-MM-DD.md` and creates concrete next tasks.

## Morning control loop

Use these checks before trusting the day’s marketing loop:

```bat
python scripts\hermes_team.py --dry-run
python scripts\task_board.py summary
python scripts\analytics.py template state\manual-metrics\today.csv --date YYYY-MM-DD --campaign-id CAMPAIGN_ID
python scripts\analytics.py validate-csv state\manual-metrics\today.csv
```

Outputs to review:

- `state/team-report-YYYY-MM-DD.md` — agent status, blockers, and top open tasks.
- `state/health.json` — queue aging, task summary, missing credentials, analytics gaps.
- `state/integration-readiness.md` — integration readiness and safe fallback status.
- `state/launch-readiness.md` — launch checklist for the daily command center.
- `state/production-check.md` — local acceptance suite result.
- `state/agent-tasks.jsonl` — persistent task board with owner, severity, priority, next action, notes, and age.

For the final daily cadence, see `docs/daily-operating-contract.md`.

## Brand voice extraction

The placeholder `config/brand-voice.md` must not be overwritten automatically. Generate a reviewed draft instead:

```bat
python scripts\brand_voice_extract.py --source "C:\Users\jorda\OneDrive\Documents\Daily worksheets" --output config\brand-voice-draft.md
```

Jordan reviews `config/brand-voice-draft.md`; only after explicit approval should `config/brand-voice.md` be replaced.

## Agent task board

Agent run log: `state/agent-runs.jsonl`
Agent task board: `state/agent-tasks.jsonl`
Health report: `state/health.json`
Integration readiness: `state/integration-readiness.json` and `state/integration-readiness.md`

## Console Pages

Start the console as before, then use:

- `/team`: agent status and dry-run buttons.
- `/health`: health blockers, stale queues, missing credentials, analytics/brand status.
- `/launch`: launch readiness checklist, area classifications, and external blockers.
- `/approvals`: combined approval inbox for publish queue, email queue, brand voice, and integrations.
- `/agent-runs`: recent agent run log.
- `/agent-tasks`: local autonomous task board with resolve action.
- Existing pages remain: `/hermes`, `/campaigns`, `/leads`, `/api/leads`, `/email-queue`, `/publish-queue`, `/performance`, `/lead/{campaign_id}`.

## Approvals And Gates

Distribution and email are hard-gated:

- Compliance must be `pass` before queueing.
- Dry-run/manual mode creates files and queues only.
- API publishing is not supported until official docs, adapter support, credentials, approval status, live config, and queue approval are all present.
- Email sending remains disabled unless `config/hermes.yaml`, `config/email.yaml`, credentials, consent, unsubscribe handling, and live gates allow it.

Never bypass these gates with browser automation or logged-in scraping.

## Metrics

Import measured CSV metrics through the console performance page or:

```bat
python scripts\analytics.py import-csv state\manual-metrics\example.csv
python scripts\analytics.py report --date YYYY-MM-DD
```

Unavailable metrics are reported as unavailable, not estimated.

## Windows Task Scheduler

Recommended action:

- Program: `E:\marketing-ops\run-hermes-team.bat`
- Arguments: `--dry-run`
- Start in: `E:\marketing-ops`
- Schedule: business-day morning run. Optionally add an afternoon `--only analytics --dry-run` run.

## Current Manual/Blocked Areas

- Brand voice profile is still a placeholder until reviewed source material is extracted.
- Social/video API publishing remains manual-kit only unless adapters are verified against official docs.
- Analytics provider APIs are not configured; CSV/manual import is the safe path.
- Missing credentials are surfaced in `state/health.json` and integration readiness output.

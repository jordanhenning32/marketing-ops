# Daily Operating Contract

This is how the Shadow Edge Hermes marketing team runs every business day.

## Morning run

```bash
python scripts/autopilot.py --dry-run
python scripts/hermes_team.py --dry-run
python scripts/launch_readiness.py
```

Open the command center:
- /autopilot: one-screen operating status and next operator action
- /launch: launch checklist and blockers
- /approvals: items waiting for Jordan
- /health: queues, open tasks, integrations, tracking gaps

Hermes automatically:
- loads repo truth files
- chooses or reuses the daily campaign
- creates channel-native assets
- compliance-checks assets
- queues publish kits/manual actions
- queues consent-based nurture locally
- writes health, integration, team, launch, and performance reports

Hermes does not automatically:
- publish externally
- send email
- replace config/brand-voice.md
- enable paid spend
- claim unavailable analytics as measured

## Midday approval/review

Minimum daily human review, under 10 minutes:
1. Open /launch.
2. Open /approvals.
3. Review brand voice if a draft is waiting.
4. Approve, skip, or mark manual-published queue items.
5. Review email queue; send manually only to consented leads, then mark sent.
6. Check /health for stale tasks or tracking blockers.

## Afternoon analytics import

Export metrics from the available platforms and import them:

```bash
python scripts/analytics.py template state/manual-metrics/today.csv --date YYYY-MM-DD --campaign-id CAMPAIGN_ID
python scripts/analytics.py validate-csv state/manual-metrics/today.csv
python scripts/analytics.py import-csv state/manual-metrics/today.csv
python scripts/hermes_team.py --only analytics --dry-run
```

Unavailable metrics stay unavailable. Do not estimate.

## Optimizer run

After analytics import:

```bash
python scripts/hermes_team.py --only optimizer --dry-run
python scripts/launch_readiness.py
```

The next optimization must be concrete: CTA rewrite, better send window, lead magnet improvement, tracking event, stale state update, or queue cleanup.

## External systems must provide

- public website form or serverless handoff to POST /api/leads
- platform/API approvals and credentials
- analytics exports/provider access
- email provider credentials and consent-compliant templates
- explicit Jordan approval for final brand voice install

## Failure modes and recovery

- Tests fail: run python -m pytest -q, fix by TDD, rerun production check.
- Launch readiness failed: open state/launch-readiness.md and fix failed area only.
- Missing metrics: import CSV or keep status unavailable.
- Stale queue: approve/skip/mark manual published from /approvals.
- Brand voice draft waiting: review it; do not auto-copy.
- Live gate accidentally enabled: set live gates false in config/hermes.yaml and rerun production check.

## Windows Task Scheduler examples

Morning full team dry-run:

```powershell
schtasks /Create /SC DAILY /TN "ShadowEdge Hermes Morning" /TR "cmd /c cd /d E:\marketing-ops && python scripts\hermes_team.py --dry-run && python scripts\launch_readiness.py" /ST 08:30
```

Afternoon analytics-only dry-run:

```powershell
schtasks /Create /SC DAILY /TN "ShadowEdge Hermes Analytics" /TR "cmd /c cd /d E:\marketing-ops && python scripts\hermes_team.py --only analytics --dry-run" /ST 15:30
```

Optional health check:

```powershell
schtasks /Create /SC DAILY /TN "ShadowEdge Hermes Health" /TR "cmd /c cd /d E:\marketing-ops && python scripts\hermes_scheduler.py" /ST 12:00
```

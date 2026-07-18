# Final Operator Handoff

Hermes is now a local, manual-safe marketing operations autopilot for Shadow Edge Tools.

## What Hermes does automatically today

- Runs the daily agent team in dry-run mode.
- Creates or reuses a campaign pack.
- Checks compliance before queueing assets.
- Generates manual publish kits.
- Captures local/public handoff leads through `POST /api/leads`.
- Queues consent-based nurture emails without sending them.
- Generates analytics reports from measured/manual data only.
- Refreshes launch readiness, approvals, health, integration readiness, and autopilot summaries.

## What Jordan reviews daily

1. Open `/autopilot`.
2. Review `/activation` for the go-live gate, credential/config preflight, canary plan, and external blockers.
3. Review `/canary` for recommended first publish candidate, canary preflight, approval ledger, rollback plan, and Jordan go/no-go packet.
4. Review `/launch` for readiness status.
5. Review `/approvals` for selected publish/email actions.
6. Review `/performance` for measured vs unavailable metrics.
7. Import metrics if available.
8. Run `python scripts\queue_hygiene.py report` and archive duplicates only with explicit review.
9. Do not install brand voice unless you explicitly approve the draft.

## What cannot be automated yet

- External credentials.
- Platform/API approvals.
- Public website deployment or connection.
- Analytics provider access.
- Explicit Jordan approval of final brand voice.
- Manual review of queued publish/email items.

## Morning command

```bat
run-autopilot.bat --dry-run
```

Direct Python equivalent:

```bat
python scripts\autopilot.py --dry-run
```

Status-only checks and launch drill:

```bat
python scripts\autopilot.py --only status
python scripts\activation.py status
python scripts\activation.py preflight
python scripts\activation.py drill
python scripts\canary.py status
python scripts\canary.py candidates
python scripts\canary.py preflight --channel x
python scripts\canary.py dry-run --channel x
python scripts\canary.py rollback-plan
python scripts\canary.py approval-ledger
python scripts\canary.py go-no-go
python scripts\autopilot.py --only approvals
python scripts\autopilot.py --only metrics
python scripts\production_check.py
python scripts\launch_drill.py
```

## Console URL

Start the console, then open:

```text
http://localhost:8876/autopilot
```

Core pages:
- `/autopilot`
- `/activation`
- `/canary`
- `/launch`
- `/approvals`
- `/health`
- `/performance`
- `/publish-queue`
- `/email-queue`

## Metrics import flow

Generate a daily template:

```bat
python scripts\analytics.py daily-template --date 2026-05-30 --campaign-id 20260530-risk-control-multiplier
```

Or explicit path:

```bat
python scripts\analytics.py template state\manual-metrics\today.csv --date 2026-05-30 --campaign-id 20260530-risk-control-multiplier
```

Fill the CSV. Keep unknown metrics as `unavailable`; use `measured` only for measured data.

Validate or dry-run before import:

```bat
python scripts\analytics.py validate-csv state\manual-metrics\today.csv
python scripts\analytics.py dry-run-import state\manual-metrics\today.csv
python scripts\metrics_import_drill.py
```

Import:

```bat
python scripts\analytics.py import-csv state\manual-metrics\today.csv
```

## Brand voice approval/install flow

Draft review and approval packet:

```bat
python scripts\brand_voice_review.py status
python scripts\brand_voice_review.py packet
```

Mark the draft approved only after Jordan review:

```bat
python scripts\brand_voice_review.py review approved --reviewer Jordan --notes "approved"
```

Install approved draft explicitly:

```bat
python scripts\brand_voice_review.py install --reviewer Jordan
```

The install command verifies the approved draft hash. If the draft changed after approval, review must happen again.

## Public website form connection

Use:
- `docs/public-lead-capture.md`
- `docs/examples/lead-capture-form.html`

Recommended flow:
1. Deploy a backend/serverless bridge if using `PUBLIC_LEAD_API_TOKEN`.
2. Pass UTM fields through the form.
3. Require consent checkbox.
4. Test success, duplicate, invalid email, missing consent, and token failure with curl.
5. Confirm new leads appear in `/leads` and nurture appears in `/email-queue`.

## Scheduler setup

See `docs/scheduler-setup.md` for copy/paste install, status, and uninstall commands for the morning dry-run autopilot task and afternoon status task. Tests and launch drills never install scheduled tasks automatically.

## Moving to live gated mode later

Use `docs/activation-checklist.md` platform by platform. Do not enable live gates until:
- credentials exist,
- official docs and API capabilities are verified,
- adapter support exists,
- compliance passes,
- Jordan approves activation,
- rollback config is known.

## Recovery if production check fails

1. Run the failing command directly.
2. Read the full error.
3. Fix only the failing local issue.
4. Re-run:

```bat
python -m compileall scripts tests
python -m pytest -q
python scripts\production_check.py
```

5. If failures mention credentials, platform approval, public website deployment, analytics provider access, or brand approval, classify them as external/operator-review blockers, not local code defects.

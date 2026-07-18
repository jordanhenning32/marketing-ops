# Canary Go-Live Plan

Status: ready_manual

The canary launch layer bridges activation-ready to full automation without enabling broad live automation. The rule is one channel, one approved asset, one measured result, and one rollback path.

## Command center
- Console: `/canary`
- Status: `python scripts\canary.py status`
- Candidates: `python scripts\canary.py candidates`
- Preflight: `python scripts\canary.py preflight --channel x`
- Dry run: `python scripts\canary.py dry-run --channel x`
- Approval ledger: `python scripts\canary.py approval-ledger`
- Go/no-go packet: `python scripts\canary.py go-no-go`
- Rollback: `python scripts\canary.py rollback-plan`

## Preconditions before any live action
- Jordan explicitly approves final brand voice or an explicit brand-voice waiver for this canary.
- Jordan approves the selected first live asset in the canary approval ledger.
- The queue item itself is approved and compliance status is `pass`.
- Required platform/API approval is documented.
- Required credentials are present and redacted in checks.
- Live gate is enabled only for the selected channel/action.
- Public website lead capture has an approved deployment and online opt-in smoke test.
- Analytics import/provider access can report measured outcomes without fabrication.
- `python scripts\activation.py drill`, `python scripts\production_check.py`, and `python scripts\launch_drill.py` pass.
- `docs/canary-rollback.md` exists and the rollback command path is known.

## Activation sequence
1. Keep all live gates disabled while reviewing readiness.
2. Generate candidates with `python scripts\canary.py candidates`.
3. Review `/publish-queue` and approve exactly one candidate if it is safe.
4. Record Jordan approval with the exact confirmation phrase.
5. Run `python scripts\canary.py preflight --channel x --queue-key QUEUE_KEY --confirm "JORDAN_APPROVES_FIRST_LIVE_CANARY"`.
6. If the preflight blocks on credentials, platform approval, public site, metrics, or Jordan approval, stop and classify the rest as external.
7. After live execution through the reviewed adapter, measure one result before adding another channel.

## Candidate order
1. Manual/kit publish candidate first.
2. One platform API canary only after credentials and platform approval.
3. One email canary only after consent, unsubscribe, provider credentials, live email gate, and Jordan approval pass.

## Success criteria
- One approved asset publishes/sends through the intended channel only.
- No duplicate queue inflation.
- Lead capture and UTM tracking are measurable or explicitly unavailable.
- No compliance, consent, or unsubscribe failures.
- Audit logs are preserved.

## Rollback
Use `docs/canary-rollback.md` and run:

```bat
python scripts\canary.py rollback-plan
python scripts\activation.py preflight
python scripts\production_check.py
```

Do not delete state/history/content. Preserve append-only logs.

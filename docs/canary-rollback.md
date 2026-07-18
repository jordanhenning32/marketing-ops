# Canary Rollback

Status: ready_manual

## Emergency disable sequence
1. Set `live_publish_enabled: false` in `config/hermes.yaml`.
2. Set `live_email_enabled: false` in `config/hermes.yaml`.
3. Set every platform `api_enabled: false` in `config/platforms.yaml`.
4. Set `send_enabled: false` in `config/email.yaml`.
5. Stop scheduler tasks: `powershell.exe -ExecutionPolicy Bypass -File scripts/scheduler_setup.ps1 -Action Uninstall`.
6. Preserve audit history. Do not delete `state/canary-events.jsonl`, `state/canary-approval-ledger.jsonl`, publish logs, email events, or analytics events.
7. Inspect `state/canary-events.jsonl`, `state/publish-log.jsonl`, `state/email-events.jsonl`, and `state/events.jsonl`.
8. Rerun `python scripts/canary.py rollback-plan`, `python scripts/activation.py preflight`, and `python scripts/production_check.py`.

## Validation
Rollback mode is considered safe only when live publish, live email, platform API flags, and email sending are disabled. Any enabled live gate after rollback is a failed state.

# Activation Checklist

Live integrations remain disabled until credentials, official platform approval, adapter support, compliance review, and live gates all pass. Keep `live_publish_enabled: false`, `live_email_enabled: false`, and platform `api_enabled: false` until every item is checked.

## Activation Manager

Activation manager commands:

```bat
python scripts\activation.py status
python scripts\activation.py check-env
python scripts\activation.py preflight
python scripts\activation.py brand-voice-preflight
python scripts\activation.py metrics-preflight
python scripts\activation.py review-batches
python scripts\activation.py canary-plan
python scripts\activation.py drill
```

Open `/activation` for the command-center view. Missing credentials, public-site deployment, API approvals, and real analytics exports are classified as external/operator blockers, not local code failures. Online checks require explicit opt-in flags.

## X
- Required environment variables: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`
- Config keys: `config/platforms.yaml -> platforms.x`, `config/hermes.yaml -> credentials.x_api_key`
- Setup pack: `docs/integrations/x.md`
- Live gate required: `platforms.x.api_enabled: true` only after approval
- Test command: `python scripts/production_check.py`
- Rollback: set `platforms.x.api_enabled: false` and `publish_mode: manual`

## Stocktwits
- Required environment variables: `STOCKTWITS_ACCESS_TOKEN`
- Config keys: `config/platforms.yaml -> platforms.stocktwits`
- Setup pack: `docs/integrations/stocktwits.md`
- Live gate required: `platforms.stocktwits.api_enabled: true`
- Test command: `python scripts/production_check.py`
- Rollback: set `platforms.stocktwits.api_enabled: false` and `publish_mode: manual`

## TikTok
- Required environment variables: `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`
- Config keys: `config/platforms.yaml -> platforms.tiktok`
- Setup pack: `docs/integrations/tiktok.md`
- Live gate required: official upload approval plus `platforms.tiktok.api_enabled: true`
- Test command: `python scripts/production_check.py`
- Rollback: set `platforms.tiktok.api_enabled: false` and `publish_mode: kit`

## LinkedIn
- Required environment variables: `LINKEDIN_ACCESS_TOKEN`
- Config keys: `config/platforms.yaml -> platforms.linkedin`
- Setup pack: `docs/integrations/linkedin.md`
- Live gate required: `platforms.linkedin.api_enabled: true`
- Test command: `python scripts/production_check.py`
- Rollback: set `platforms.linkedin.api_enabled: false` and `publish_mode: manual`

## YouTube
- Required environment variables: `YOUTUBE_CLIENT_SECRETS_FILE`
- Config keys: `config/platforms.yaml -> platforms.youtube`
- Setup pack: `docs/integrations/youtube.md`
- Live gate required: upload adapter support plus `platforms.youtube.api_enabled: true`
- Test command: `python scripts/production_check.py`
- Rollback: set `platforms.youtube.api_enabled: false` and `publish_mode: kit`

## email provider
- Required environment variables: `EMAIL_PROVIDER_API_KEY`
- Config keys: `config/email.yaml -> provider/send_enabled`, `config/platforms.yaml -> platforms.email`
- Setup pack: `docs/integrations/email.md`
- Live gate required: `send_enabled: true` only after consent/unsubscribe checks pass
- Test command: `python scripts/production_check.py`
- Rollback: set `send_enabled: false` and `platforms.email.api_enabled: false`

## analytics provider
- Required environment variables: provider-specific export/API credentials, not currently required for manual CSV
- Config keys: `config/analytics.yaml`, `config/tracking.yaml`
- Setup pack: `docs/integrations/analytics.md`
- Live gate required: none for manual CSV; provider imports need explicit config
- Test command: `python scripts/analytics.py validate-csv state/manual-metrics/YYYY-MM-DD-template.csv`
- Rollback: set provider `enabled: false` and use manual CSV import

## public website lead form
- Required environment variables: optional `PUBLIC_LEAD_API_TOKEN`
- Config keys: deployment-specific backend/proxy config
- Setup pack: `docs/public-lead-capture.md`, `docs/examples/lead-capture-form.html`
- Live gate required: public site deployment approval and successful test submissions
- Test command: use the curl examples in `docs/public-lead-capture.md`
- Rollback: remove form action or point form back to newsletter/provider fallback

## brand voice final approval
- Required environment variables: none
- Config keys: `config/brand-voice-draft.md`, `config/brand-voice.md`
- Setup pack: `docs/final-operator-handoff.md`
- Live gate required: explicit Jordan approval
- Test command: `python scripts/launch_readiness.py`
- Rollback: restore previous `config/brand-voice.md` from backup/version control; never auto-install

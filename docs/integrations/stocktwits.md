# stocktwits Integration Setup Pack

- Desired capability: Publish compliant Stocktwits posts and import reactions when official access is available.
- Official docs URL: https://api.stocktwits.com/developers/docs
- Docs verified: false
- Required credentials: STOCKTWITS_ACCESS_TOKEN
- Required account/app approval: Required before live API use; verify in official platform console.
- Config keys to update: config/platforms.yaml, config/email.yaml, config/analytics.yaml, config/hermes.yaml
- Tests to run: python -m pytest, python scripts/production_check.py
- Live gate requirements:
  - official docs verified
  - credentials present
  - config gate enabled
  - compliance pass
  - explicit Jordan approval

- Rollback: Set api_enabled/send_enabled/live_* gates back to false and return to manual queue mode.
- Current fallback workflow: manual queue + platform-ready copy

Do not enable live publish/send by default.

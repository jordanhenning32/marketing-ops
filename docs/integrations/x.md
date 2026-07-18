# x Integration Setup Pack

- Desired capability: Publish approved X posts/threads and import engagement metrics.
- Official docs URL: https://developer.x.com/en/docs
- Docs verified: false
- Required credentials: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
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
- Current fallback workflow: manual queue + browser posting

Do not enable live publish/send by default.

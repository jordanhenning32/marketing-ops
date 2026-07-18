# youtube Integration Setup Pack

- Desired capability: Upload reviewed long-form videos/Shorts after OAuth setup and manual approval.
- Official docs URL: https://developers.google.com/youtube/v3
- Docs verified: false
- Required credentials: YOUTUBE_CLIENT_SECRETS_FILE, YOUTUBE_TOKEN_FILE
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
- Current fallback workflow: metadata/description/shorts kit

Do not enable live publish/send by default.

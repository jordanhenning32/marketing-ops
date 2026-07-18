# tiktok Integration Setup Pack

- Desired capability: Prepare upload kits; API upload only after official app approval.
- Official docs URL: https://developers.tiktok.com/doc/content-posting-api-get-started
- Docs verified: false
- Required credentials: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET
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
- Current fallback workflow: upload kit with script/caption/visual notes

Do not enable live publish/send by default.

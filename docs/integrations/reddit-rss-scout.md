# reddit_rss_scout Integration Setup Pack

- Desired capability: Read public RSS/http/Reddit sources for briefing context without logged-in scraping.
- Official docs URL: https://www.reddit.com/dev/api/
- Docs verified: false
- Required credentials: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
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
- Current fallback workflow: manual-check briefing and configured public source list

Do not enable live publish/send by default.

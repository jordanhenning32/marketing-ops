# Tracking Contract

Hermes uses a local, canonical event spine so daily optimization is based on measured data only.

Config: config/tracking.yaml
Event log: state/events.jsonl
Manual metric import: state/manual-metrics/*.csv

Canonical events:
- page_view
- lead_capture
- email_queued
- email_sent_manual
- publish_queued
- publish_approved
- publish_manual
- conversion
- revenue

Required event fields:
- event
- timestamp
- campaign_id
- source

UTM schema:
- utm_source: platform name
- utm_medium: social, shorts, video, email, partner, or organic
- utm_campaign: campaign id
- utm_content: asset id

Metric import fields:
- date
- campaign_id
- metric
- value
- source
- status
- optional: event, timestamp, utm_source, utm_medium, utm_content, notes

Metrics that must be measured or marked unavailable:
- website_sessions_by_source
- landing_page_clicks
- email_captures
- email_opens_clicks_replies_unsubscribes
- x_engagement
- stocktwits_engagement
- tiktok_engagement
- linkedin_engagement
- youtube_watch_metrics
- conversions
- revenue

Commands:

```bash
python scripts/analytics.py template state/manual-metrics/today.csv --date 2026-05-30 --campaign-id 20260530-risk-control-multiplier
python scripts/analytics.py validate-csv state/manual-metrics/today.csv
python scripts/analytics.py import-csv state/manual-metrics/today.csv
python scripts/tracking.py status
```

Platform export requirements:
- Website: sessions by source, landing page clicks, lead form submits.
- Email: queued/sent/open/click/reply/unsubscribe/bounce counts from consented sends.
- X/Stocktwits/TikTok/LinkedIn: impressions/views, reactions, comments/replies, shares/reposts, profile/link clicks.
- YouTube: views, watch time, retention, subscribers, CTR.
- Store/payment system: conversion count and revenue.

Do not estimate missing values. Mark status=unavailable, blocked, or not_configured.

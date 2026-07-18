# Public Lead Capture Handoff

Hermes exposes a local bridge for public website forms:

```text
POST /api/leads
```

Fields:
- `email`: required, valid email address
- `campaign_id`: required
- `utm_source`: platform/source name
- `utm_medium`: `social`, `shorts`, `video`, `email`, `partner`, or `organic`
- `utm_content`: asset id
- `consent`: boolean; nurture is queued only when true
- `landing_page`: public page URL
- `name`: optional contact name
- `product_name` or `interest`: optional product/offer interest
- `message` or `note`: optional note from the form

Optional token:
- Set `PUBLIC_LEAD_API_TOKEN` in the server environment.
- Send header: `x-shadowedge-token: <token>`.
- If no token is configured, the route accepts local/test posts only.
- To allow unauthenticated non-local posts for a deliberate development bridge, set both `PUBLIC_LEAD_ALLOW_UNAUTHENTICATED=1` and `PUBLIC_LEAD_DEV_OVERRIDE=1`.
- Do not use unauthenticated non-local mode for production; it exists only for explicit development bridge tests.
- Prefer adding the token header in a backend/serverless bridge, not public browser JavaScript.

## Local smoke test

Run this before connecting the public site or after changing the lead form:

```bash
python scripts/lead_capture_smoke.py
python scripts/activation.py test-public-lead
```

The smoke test uses temporary state by default. It checks a valid consented lead, duplicate submission, invalid email, missing consent, and token-behavior expectations without polluting production leads.

## Optional public-site activation test

Online testing is opt-in only. Configure `PUBLIC_LEAD_TEST_URL` and `PUBLIC_LEAD_API_TOKEN`, then run:

```bash
python scripts/activation.py test-public-lead --online
```

Missing public URL/token is an external activation blocker, not a local code failure. Do not run online tests against production forms unless the test lead is clearly marked and approved.

## Copy/paste HTML form

See `docs/examples/lead-capture-form.html` for a complete plain HTML + fetch example with consent checkbox and UTM passthrough.

Minimal fetch body:

```js
await fetch('http://localhost:8876/api/leads', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    email: emailValue,
    campaign_id: '20260530-risk-control-multiplier',
    utm_source: new URLSearchParams(location.search).get('utm_source') || 'website',
    utm_medium: new URLSearchParams(location.search).get('utm_medium') || 'organic',
    utm_content: new URLSearchParams(location.search).get('utm_content') || 'lead-form',
    consent: consentCheckbox.checked,
    landing_page: location.href
  })
})
```

## Serverless/backend bridge sketch

Use this pattern when `PUBLIC_LEAD_API_TOKEN` is configured:

```js
export default async function handler(req, res) {
  const upstream = await fetch(process.env.HERMES_LEAD_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-shadowedge-token': process.env.PUBLIC_LEAD_API_TOKEN
    },
    body: JSON.stringify(req.body)
  });
  const body = await upstream.json();
  res.status(upstream.status).json(body);
}
```

Do not expose private tokens in static public JavaScript.

For the `shadow-edge-tools` Next.js site, configure the server-only environment
variables:

```bash
MARKETING_OPS_LEAD_API_URL=https://<your-marketing-ops-host>/api/leads
MARKETING_OPS_LEAD_API_TOKEN=<same value as PUBLIC_LEAD_API_TOKEN here>
```

The public site route posts to marketing-ops from the server so the token is not
visible in browser JavaScript.

## curl tests

Success:

```bash
curl -sS -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -d '{"email":"trader@example.com","campaign_id":"20260530-risk-control-multiplier","utm_source":"website","utm_medium":"organic","utm_content":"lead-form","consent":true,"landing_page":"https://shadowedgetools.com/risk-control"}'
```

Duplicate:

```bash
curl -sS -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -d '{"email":"trader@example.com","campaign_id":"20260530-risk-control-multiplier","utm_source":"website","utm_medium":"organic","utm_content":"lead-form","consent":true}'
```

Invalid email:

```bash
curl -i -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -d '{"email":"not-an-email","campaign_id":"20260530-risk-control-multiplier","consent":true}'
```

Missing consent, captured but no nurture:

```bash
curl -sS -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -d '{"email":"no-consent@example.com","campaign_id":"20260530-risk-control-multiplier","utm_source":"website","utm_medium":"organic","utm_content":"lead-form","consent":false}'
```

Optional token failure, when `PUBLIC_LEAD_API_TOKEN` is set:

```bash
curl -i -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -H 'x-shadowedge-token: wrong-token' \
  -d '{"email":"token-test@example.com","campaign_id":"20260530-risk-control-multiplier","consent":true}'
```

Token success:

```bash
curl -sS -X POST http://localhost:8876/api/leads \
  -H 'Content-Type: application/json' \
  -H "x-shadowedge-token: $PUBLIC_LEAD_API_TOKEN" \
  -d '{"email":"token-ok@example.com","campaign_id":"20260530-risk-control-multiplier","consent":true}'
```

## Expected response

```json
{
  "ok": true,
  "created": true,
  "email": "trader@example.com",
  "lead_id": "lead-...",
  "email_queue_created": true,
  "consent": true
}
```

Duplicate submissions return `created: false` and remain safe.

## Connection test checklist

- [ ] Public form requires valid email.
- [ ] Consent checkbox is required for nurture.
- [ ] UTM parameters pass through.
- [ ] `landing_page` records the source page.
- [ ] Success curl creates a lead in `/leads`.
- [ ] Duplicate curl does not create unsafe duplicate lead state.
- [ ] Missing consent does not queue nurture.
- [ ] Optional token failure returns 401 when token is configured.
- [ ] Confirmation/nurture appears in `/email-queue` only for consented leads.
- [ ] No live email is sent by this endpoint.

## Local verification

```bash
python -m pytest tests/test_launch_completion.py::test_public_lead_api_success_duplicate_validation_and_token -q
python scripts/launch_readiness.py
```

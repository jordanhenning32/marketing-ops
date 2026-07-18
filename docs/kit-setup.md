# Kit (ConvertKit) setup — real email capture

Goal: a live, public page that captures an email, auto-delivers the risk-control
checklist, and runs the 5-email nurture sequence. No website required.

Assets (already written, just paste them in):
- Lead magnet: `content/lead-magnets/risk-control-checklist.md`
- Email copy: `content/email-sequences/risk-control-nurture.md`
- Sequence spec: `config/email.yaml`

You do the clicks (your account/login). Everything you paste is ready.

---

## 0. Make the checklist a PDF (5 min)

Kit delivers a file as the signup incentive. Turn the markdown into a PDF:
- Open `risk-control-checklist.md` in any markdown editor (VS Code preview, Typora,
  or paste into a Google Doc) → Export/Print to PDF.
- Name it `shadow-edge-risk-control-checklist.pdf`.
- Keep it one or two pages so it's printable.

## 1. Create the Kit account (10 min)

1. Sign up at kit.com (free plan covers up to 10k subscribers).
2. Set the sending identity to match `config/email.yaml`:
   - From name: **Shadow Edge Tools**
   - From email: **ops@shadowedgetools.com**  *(you must be able to receive at the
     domain to verify it — do the domain authentication step Kit prompts; it's what
     keeps you out of spam)*
   - Reply-to: **support@shadowedgetools.com**

## 2. Build the landing page (15 min)

1. Grow → Landing Pages & Forms → **Create a landing page** (pick a simple template).
2. Headline idea (Jordan voice): *"The pre-trade checklist that closes the gap
   between your rule and your click."* Subhead: *"For NT8 futures & prop traders.
   Free. Printable."*
3. Form fields: **email only.** (Less friction = more signups. You can ask name
   later.) Keep the consent language honest — they're opting into Shadow Edge updates.
4. Incentive / "what subscribers receive": upload the checklist PDF. Kit emails it
   automatically on confirm.
5. Turn ON **double opt-in** (confirmed subscriptions). It protects deliverability
   and your compliance posture — worth the small drop in raw signups.
6. Publish → copy the public URL (looks like `https://yourname.kit.com/xxxx`).

## 3. Build the nurture automation (20 min)

Kit calls these **Sequences** (the emails) + a **Visual Automation** (the trigger).

1. Grow → Sequences → **New sequence** → "Risk-control nurture".
2. Add 5 emails. Paste subject + body from `risk-control-nurture.md`. Set the wait
   between each to match `email.yaml`:

   | # | Email id      | Subject                                              | Wait after previous |
   |---|---------------|------------------------------------------------------|---------------------|
   | 1 | confirmation  | Confirmed: your Shadow Edge risk-control checklist    | 0 days (immediate)  |
   | 2 | risk-rule     | One rule before the first order                      | 1 day               |
   | 3 | drawdown-guard| Where drawdown discipline breaks                     | 2 days (→ day 3)    |
   | 4 | order-lock    | Removing one bad-click failure mode                  | 2 days (→ day 5)    |
   | 5 | demo          | See the Shadow Edge workflow                         | 2 days (→ day 7)    |

   In email #1, replace `[LINK TO CHECKLIST PDF]` with Kit's hosted file link (or
   rely on the incentive delivery and adjust the copy).
3. Automations → **New automation**: trigger = *"Joins a form"* (your landing page)
   → action = *"Subscribe to sequence"* (Risk-control nurture). Set it live.

## 4. Put the link where the traffic is

The landing page URL is your one capture link. Drop it in:
- YouTube video descriptions (first line).
- X / Stocktwits / Reddit **profile bios** (per our funnel posture — link in bio,
  not in replies).
- The end card / pinned comment of Shorts.

Use a UTM on each so attribution works later, e.g.
`...kit.com/xxxx?utm_source=youtube&utm_content=<video-id>`.

## 5. Later: sync to this app for attribution (deferred)

Kit fires a webhook on each new subscriber. Once this app is reachable from the
internet (deployed or tunneled), point that webhook at `POST /api/leads` so the
app records which video/post produced each signup. Until then, Kit's own UTM
reporting is enough. The bridge already exists — see `docs/public-lead-capture.md`.

---

## Done = real capture

When step 3 is live, a stranger who clicks your link gives an email, instantly
gets the checklist, and walks the 7-day sequence — automatically, deliverable,
compliant, with unsubscribe handled. That's the "real way to capture email."

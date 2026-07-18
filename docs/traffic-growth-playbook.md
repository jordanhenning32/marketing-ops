# Traffic growth playbook — ready to execute

The code side of the funnel is fixed and the site is technically sound. Traffic is
now a **discovery + consistency** problem, which is won on live surfaces. This is
everything prepped to near-zero friction. Honors the locked engagement posture:
value-first, **no product links in posts**, profile-based funnel, disclose
affiliation if the tool is named, forums are listen-only for automation (you may
engage there by hand as a disclosed vendor).

---

## 0. The three highest-ROI actions (only you can trigger — here are the exact steps)

**A. Backfill your 16 YouTube videos (2 clicks) — do this first.**
Turns 482 existing views from a dead end into a tracked, email-capturing funnel.
1. Console → `/youtube` → **Reconnect** (grants the edit scope via Google consent).
2. Click **🔗 Preview link backfill** (safe, read-only) → then **✓ Apply backfill**.

**B. Publish the optimized NinjaTrader Ecosystem listings.**
Your highest purchase-intent channel, currently ~2 sessions/30d.
- Paste `docs/ninjatrader-ecosystem-listing.md` (BB 1597 + DG 1598) into vendor admin, add the screenshots from its shot-list, link a demo video.

**C. Set every social profile's bio link to the tracked checklist.**
The community funnel is profile-based — the bio link *is* the CTA. Set it on X,
YouTube (channel links), TikTok, Instagram, LinkedIn to:
`https://shadowedgetools.com/checklist?utm_source=bio&utm_medium=social&utm_campaign=profile`

---

## 1. Ready-to-paste community content (value-first, no links)

### Reddit — r/FuturesTrading, r/Daytrading (strictest: zero promo, no product mention, no link)
Post as a real trader sharing a lesson. The funnel is: helpful post → profile → bio.

**Post 1 — discipline story (text post)**
> **Title:** The number that finally kept me from blowing my funded account wasn't my win rate
>
> Took me three blown evals to figure out my problem was never entries — it was that I had no hard line for "done for the day." I'd hit my daily loss limit, tell myself one more, and give back a good week in 20 minutes.
>
> What fixed it: I wrote my max daily loss and max trades on a sticky note before the session, and I treat hitting either as a *hard stop* — flat, platform closed, walk away. Boring. But my trailing drawdown stopped shrinking and I passed the next eval.
>
> Curious how others enforce the daily stop — do you use a physical rule, a platform lockout, or just willpower? Willpower alone never worked for me.

**Post 2 — risk tip (text post)**
> **Title:** If you size by "number of contracts" instead of dollars-at-risk, you're taking a different bet every trade
>
> Simple thing that took me too long: 2 contracts with a 6-tick stop on MNQ is a completely different risk than 2 contracts with a 20-tick stop. Same "size," 3x the risk. On a funded account with a trailing drawdown, that inconsistency is what quietly walks you into a breach.
>
> Now I pick a fixed $ risk per trade first, then let the stop distance decide the contract count. My drawdown curve got way smoother once every trade risked the same dollars. Anyone else switch from contract-based to dollar-based sizing — did it change your consistency?

### StockTwits — $ES_F / $NQ_F (trader-native, 1–3 sentences; may name the tool softly as proof, no inline URL)
> Watching $NQ_F chop the IB range. The edge today isn't the entry — it's not revenge-trading the fakeout. I hard-cap my daily loss and let the account lock when I hit it; discipline > prediction. (I build risk tools for NT8, so I'm biased, but the sticky-note version works too.)

> $ES_F The traders I know who keep their funded accounts all have one thing in common: a pre-set daily loss line they actually respect. Not a bigger brain — a harder stop.

### X / Twitter (punchy, hook-first, 0–2 hashtags, CTA = "free checklist in bio")
> Blown 3 funded accounts? It's almost never your strategy.
>
> It's that you had no hard line for "stop for the day" — so one revenge trade gave back a good week.
>
> Pick a max daily loss. Treat hitting it as flat-and-done. Boring, but it's the whole game.
>
> Free pre-trade risk checklist in bio 👇

### LinkedIn (professional, story-driven, ~120 words, CTA = checklist in bio)
> Three blown prop-firm evaluations taught me the same lesson three times: funded accounts don't die from bad entries. They die from one undisciplined afternoon.
>
> The fix wasn't a better setup. It was a hard rule — a fixed dollar risk per trade, a pre-set daily loss limit, and treating that limit as a full stop for the day. Flat, platform closed, done.
>
> The moment I made the stop non-negotiable, my trailing drawdown stopped shrinking and I passed. Consistency came from constraints, not from prediction.
>
> I put the pre-trade checklist I use in my bio if it's useful to anyone grinding evals right now.

### Forums (futures.io / EliteTrader) — MANUAL only, as a disclosed vendor, value-first, no links
Don't drop links (fastest way to a domain ban). Participate in existing risk/discipline
threads with genuinely useful answers; let your disclosed vendor profile do the work.
Answer "how do you enforce your daily loss limit / trailing drawdown" questions with
specifics. One real, helpful answer per week beats ten promo posts.

---

## 2. Two-week distribution sequence

| Day | Action | Surface |
|---|---|---|
| 1 | Backfill 16 videos (§0A); set all bio links (§0C) | YouTube + profiles |
| 2 | Publish both Ecosystem listings + screenshots (§0B) | NT Ecosystem |
| 3 | Reddit Post 1 (discipline story) | r/Daytrading |
| 4 | StockTwits take #1 | $NQ_F |
| 5 | X post + LinkedIn post | X, LinkedIn |
| 7 | Answer 1–2 real risk/discipline threads (no links) | futures.io |
| 8 | Reddit Post 2 (risk tip) | r/FuturesTrading |
| 10 | Record ONE long-form walkthrough (§3) | YouTube |
| 12 | StockTwits take #2 + X post | $ES_F, X |
| 14 | Check GA4 acquisition: youtube / nt_ecosystem / social should climb | Performance tab |

Consistency is the lever — 3–4 value touches/week for a few weeks compounds; a single burst does nothing.

## 3. Fix the content mix (the Shorts problem)

Shorts grow reach but don't drive clicks — viewers swipe past descriptions. The
pipeline already auto-builds a long-form YouTube kit whenever your source recording
is >180s (`video_pipeline.should_prepare_long_form`), so the only missing input is a
longer recording. Record **one 5–10 minute walkthrough** — e.g. "How I size every
trade to a fixed dollar risk in NinjaTrader 8" or "Setting a trailing-drawdown
guardrail so you never breach a funded account." Long-form descriptions get *read*,
so the tracked `/checklist` link (now injected automatically) actually gets clicked.
Keep cutting Shorts from it for reach → they funnel viewers to subscribe → the
long-form + pinned comment converts.

## 4. How you'll know it's working

Everything above is tagged so the Performance tab / GA4 tells the story:
- `utm_source=youtube` sessions on `/checklist` → the backfill + new uploads working.
- `sessionSource=ninjatraderecosystem.com` climbing from 2 → double digits → listing working.
- `utm_source=bio` sessions → community/profile funnel working.
Re-check GA4 acquisition every 2 weeks; adjust toward whatever channel moves.

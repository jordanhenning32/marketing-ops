"""Shadow Edge Tools — Operations Tracker.

Reads markdown state files under `marketing-ops/state/`, computes today's
overdue / due-today actions, drafts follow-up messages from templates, and
writes a one-page Daily Ops markdown file under `marketing-ops/daily-ops/`.

No LLM required for v1 — everything is deterministic and template-driven.
The Console (FastAPI) imports `compute_dashboard()` to render live state.

Run directly:
    python scripts/ops_tracker.py

Or via run-ops.bat at the project root.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from cli_io import configure_utf8_stdio

STATE_DIR = ROOT / "state"
OUT_DIR = ROOT / "daily-ops"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class PipelineItem:
    name: str
    stage: str
    last_contact: str
    next_action: str
    due: date | None
    raw_line: str
    email: str = ""

    @property
    def is_overdue(self) -> bool:
        return self.due is not None and self.due < date.today()

    @property
    def is_due_today(self) -> bool:
        return self.due == date.today()


@dataclass
class GoalState:
    target_customers: int = 1000
    target_date: date | None = None
    customers: int = 0
    leads: int = 0
    long_forms_recorded: int = 0
    nt_ecosystem_status: str = "not_applied"
    partnerships_active: int = 0
    influencers_seeded: int = 0
    affiliates: int = 0
    price: int = 249
    checkout_live: bool = False
    lead_capture_live: bool = False


@dataclass
class State:
    goal: GoalState = field(default_factory=GoalState)
    nt_ecosystem: dict = field(default_factory=dict)
    partnerships: list[PipelineItem] = field(default_factory=list)
    influencers: list[PipelineItem] = field(default_factory=list)
    affiliates_status: str = "not_launched"
    content_calendar: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
KV_RE = re.compile(r"\*\*(?P<k>[A-Za-z _]+):\*\*\s*(?P<v>.+)")


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s or s in {"—", "-", "TBD", "?"}:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _kv_block(text: str) -> dict[str, str]:
    """Extract `**Key:** Value` lines into a dict."""
    out: dict[str, str] = {}
    for m in KV_RE.finditer(text):
        out[m.group("k").strip().lower()] = m.group("v").strip()
    return out


def _parse_pipeline_line(line: str) -> PipelineItem | None:
    """Parse a bullet of the form:
    - **Name** — stage: `x` — last_contact: ... — next_action: ... — due: YYYY-MM-DD
    """
    # Strip leading "- " / "* " bullet marker only (not **)
    body = re.sub(r"^\s*[-*]\s+", "", line).rstrip()
    if not body or not body.startswith("**"):
        return None

    # Name is the first **bold** group
    name_match = re.match(r"\*\*(?P<name>.+?)\*\*", body)
    if not name_match:
        return None
    name = name_match.group("name").strip()
    rest = body[name_match.end():]

    def grab(key: str) -> str:
        m = re.search(rf"{key}:\s*`?([^`—]+?)`?\s*(?=—|$)", rest)
        return m.group(1).strip() if m else ""

    stage = grab("stage")
    email = grab("email")
    last_contact = grab("last_contact")
    next_action = grab("next_action")
    due_str = grab("due")

    return PipelineItem(
        name=name,
        stage=stage or "unknown",
        last_contact=last_contact,
        next_action=next_action,
        due=_parse_date(due_str),
        raw_line=line,
        email=email,
    )


def _parse_pipeline_section(text: str, header: str = "## Pipeline") -> list[PipelineItem]:
    items: list[PipelineItem] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            in_section = line.strip().lower() == header.lower()
            continue
        if not in_section:
            continue
        if line.strip().startswith("-"):
            item = _parse_pipeline_line(line)
            if item:
                items.append(item)
    return items


def parse_goal(text: str) -> GoalState:
    g = GoalState()
    kv = _kv_block(text)

    # target customers / date
    m = re.search(r"\*\*Target customers:\*\*\s*(\d+)", text)
    if m:
        g.target_customers = int(m.group(1))
    g.target_date = _parse_date(kv.get("target date", ""))

    # current state counters
    g.customers = _int_after(text, r"\*\*Customers:\*\*\s*(\d+)")
    g.leads = _int_after(text, r"\*\*Lead-magnet emails:\*\*\s*(\d+)")
    g.long_forms_recorded = _int_after(text, r"\*\*Long-forms recorded:\*\*\s*(\d+)")

    m = re.search(r"\*\*Effective price:\*\*\s*\$?([\d,]+)", text)
    if m:
        g.price = int(m.group(1).replace(",", ""))
    g.checkout_live = bool(re.search(r"\*\*Checkout live:\*\*\s*(yes|true|live)", text, re.I))
    g.lead_capture_live = bool(re.search(r"\*\*Lead capture live:\*\*\s*(yes|true|live)", text, re.I))
    return g


def _int_after(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def parse_nt_ecosystem(text: str) -> dict:
    kv = _kv_block(text)
    next_action_match = re.search(
        r"##\s*Next action\s*\n+\*\*Action:\*\*\s*(?P<action>.+?)\n+\*\*Due:\*\*\s*(?P<due>.+)",
        text,
        flags=re.IGNORECASE,
    )
    next_action = ""
    due = None
    if next_action_match:
        next_action = next_action_match.group("action").strip()
        due = _parse_date(next_action_match.group("due"))
    return {
        "status": kv.get("status", "unknown"),
        "stage": kv.get("stage", ""),
        "applied_date": _parse_date(kv.get("applied date", "")),
        "approved_date": _parse_date(kv.get("approved date", "")),
        "next_action": next_action,
        "due": due,
    }


def parse_content_calendar(text: str) -> dict:
    recorded = 0
    planned = 0
    for line in text.splitlines():
        if line.strip().startswith("(none recorded yet)"):
            recorded = 0
        # crude heuristic: count numbered planned items under "## Planned long-forms"
    planned_section = re.search(
        r"## Planned long-forms.*?(?=##|\Z)", text, flags=re.DOTALL
    )
    if planned_section:
        planned = len(re.findall(r"^\s*\d+\.", planned_section.group(0), flags=re.MULTILINE))
    # If anyone marks a recorded long-form, count "✅" or "DONE" rows
    recorded += len(re.findall(r"✅|\bDONE\b", text))
    return {"recorded": recorded, "planned": planned}


def parse_affiliates_status(text: str) -> str:
    kv = _kv_block(text)
    return kv.get("status", "not_launched")


def load_state() -> State:
    s = State()
    s.goal = parse_goal(_read(STATE_DIR / "goal.md"))
    s.nt_ecosystem = parse_nt_ecosystem(_read(STATE_DIR / "nt-ecosystem.md"))
    s.partnerships = _parse_pipeline_section(_read(STATE_DIR / "partnerships.md"))
    s.influencers = _parse_pipeline_section(_read(STATE_DIR / "influencers.md"))
    s.affiliates_status = parse_affiliates_status(_read(STATE_DIR / "affiliates.md"))
    s.content_calendar = parse_content_calendar(_read(STATE_DIR / "content-calendar.md"))

    # roll up summary counters
    s.goal.partnerships_active = sum(
        1 for p in s.partnerships
        if p.stage in {"replied", "meeting", "partner"}
    )
    s.goal.influencers_seeded = sum(
        1 for p in s.influencers if p.stage in {"key_sent", "posted"}
    )
    s.goal.long_forms_recorded = s.content_calendar.get("recorded", 0)
    s.goal.nt_ecosystem_status = s.nt_ecosystem.get("status", "unknown")
    return s


# ---------------------------------------------------------------------------
# Follow-up templates (no LLM needed for v1)
# ---------------------------------------------------------------------------

PROP_FIRM_INTRO_TEMPLATE = """\
Subject: Shadow Edge Tools — risk-management add-on for {firm} traders

Hi {firm} team,

I'm Jordan, founder of Shadow Edge Tools. I build NinjaTrader 8 add-ons
specifically designed to help prop traders stay inside firm rules.

The two products:

- **Drawdown Guardian** — live trailing-drawdown + daily-loss tracker with
  built-in {firm} preset rules, optional auto-flatten, and a Discipline Score.
- **Bracket Boss** — risk-based position sizing, auto-protective brackets,
  and an Order Lock for "after the red trade" sessions.

Both ship as standard NinjaScript with no DLLs and use only standard bracket
orders, so they're compatible with any firm that allows automated brackets.

Would your team be open to:

1. A 15-minute demo on a sim account
2. Free lifetime keys for your support / risk teams to evaluate
3. A discussion of approved-vendor or partner status for {firm} traders

Happy to send a 3-minute video walkthrough first if that's easier.

Best,
Jordan
shadowedgetools.com
"""


PROP_FIRM_FOLLOWUP_TEMPLATE = """\
Subject: Re: Shadow Edge Tools — quick follow-up

Hi {firm} team,

Quick follow-up on my note from {last_contact} — happy to share more
details, send a demo video, or jump on a 15-minute call whenever it
fits your schedule.

If now isn't a fit, no problem. A pointer to the right contact for
3rd-party tool partnerships would also be appreciated.

Best,
Jordan
shadowedgetools.com
"""


INFLUENCER_INTRO_TEMPLATE = """\
Hey {handle} — saw your recent post about {hook}.

I built two NinjaTrader 8 tools specifically for prop traders who get
caught by trailing drawdown and daily-loss rules. I'd like to send you
free lifetime access — no strings, no obligation to post.

If you find them useful and want to share that's great, but the offer
stands either way. Just want builders like you actually using them and
giving honest feedback.

Reply with the email you'd like the keys sent to and I'll set it up.

— Jordan, Shadow Edge Tools
"""


def draft_partnership_followup(p: PipelineItem) -> str:
    if p.stage in {"not_started", "researching"}:
        return PROP_FIRM_INTRO_TEMPLATE.format(firm=p.name)
    if p.stage in {"reached_out"}:
        return PROP_FIRM_FOLLOWUP_TEMPLATE.format(
            firm=p.name, last_contact=p.last_contact or "earlier"
        )
    return f"(no template for stage `{p.stage}` — write manually)"


# ---------------------------------------------------------------------------
# Daily ops markdown generation
# ---------------------------------------------------------------------------

def _items_overdue(items: list[PipelineItem]) -> list[PipelineItem]:
    return [i for i in items if i.is_overdue]


def _items_due_today(items: list[PipelineItem]) -> list[PipelineItem]:
    return [i for i in items if i.is_due_today]


def days_until(target: date | None) -> int | None:
    if target is None:
        return None
    return (target - date.today()).days


def compute_dashboard(state: State | None = None) -> dict:
    """Returns a dashboard-ready dict for the Console."""
    if state is None:
        state = load_state()

    days_left = days_until(state.goal.target_date)
    customers_remaining = max(0, state.goal.target_customers - state.goal.customers)
    sales_per_day_required = (
        round(customers_remaining / days_left, 2)
        if days_left and days_left > 0 else None
    )

    overdue_partners = _items_overdue(state.partnerships)
    due_today_partners = _items_due_today(state.partnerships)
    overdue_inf = _items_overdue(state.influencers)
    due_today_inf = _items_due_today(state.influencers)

    nt_due = state.nt_ecosystem.get("due")
    nt_overdue = nt_due is not None and nt_due < date.today()
    nt_due_today = nt_due == date.today()

    return {
        "today": date.today().isoformat(),
        "goal": {
            "target_customers": state.goal.target_customers,
            "target_date": state.goal.target_date.isoformat()
            if state.goal.target_date else None,
            "customers": state.goal.customers,
            "customers_remaining": customers_remaining,
            "days_remaining": days_left,
            "sales_per_day_required": sales_per_day_required,
            "leads": state.goal.leads,
            "long_forms_recorded": state.goal.long_forms_recorded,
            "long_forms_planned": state.content_calendar.get("planned", 0),
            "partnerships_active": state.goal.partnerships_active,
            "influencers_seeded": state.goal.influencers_seeded,
            "nt_ecosystem_status": state.goal.nt_ecosystem_status,
        },
        "nt_ecosystem": {
            "status": state.nt_ecosystem.get("status", "unknown"),
            "next_action": state.nt_ecosystem.get("next_action", ""),
            "due": nt_due.isoformat() if nt_due else None,
            "overdue": nt_overdue,
            "due_today": nt_due_today,
        },
        "partnerships": {
            "total": len(state.partnerships),
            "overdue": [p.__dict__ | {"due": p.due.isoformat() if p.due else None} for p in overdue_partners],
            "due_today": [p.__dict__ | {"due": p.due.isoformat() if p.due else None} for p in due_today_partners],
            "by_stage": _group_by_stage(state.partnerships),
        },
        "influencers": {
            "total": len(state.influencers),
            "overdue": [p.__dict__ | {"due": p.due.isoformat() if p.due else None} for p in overdue_inf],
            "due_today": [p.__dict__ | {"due": p.due.isoformat() if p.due else None} for p in due_today_inf],
            "by_stage": _group_by_stage(state.influencers),
        },
        "affiliates_status": state.affiliates_status,
        "content_calendar": state.content_calendar,
    }


def _group_by_stage(items: list[PipelineItem]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i.stage] = out.get(i.stage, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Live signal readers — turn the system's real state into today's sales moves.
# All offline + defensive: a missing/empty file degrades to a sensible default.
# ---------------------------------------------------------------------------
def _safe_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _days_ago(iso: str | None) -> int | None:
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return max(0, (date.today() - d).days)


def _video_activity() -> dict:
    """Most recent finished video job + how many have shipped."""
    last_iso = None
    done = 0
    for jp in (ROOT / "content" / "jobs").glob("*.json"):
        j = _safe_json(jp)
        if j.get("kind") == "video" and j.get("state") == "done":
            done += 1
            fin = j.get("finished_at") or j.get("started_at")
            if fin and (last_iso is None or fin > last_iso):
                last_iso = fin
    return {"days_ago": _days_ago(last_iso), "done": done}


def _youtube_signal() -> dict:
    rows = list(_iter_jsonl(STATE_DIR / "youtube-channel-stats.jsonl"))
    if not rows:
        return {}
    latest = rows[-1]
    prior = rows[-2] if len(rows) >= 2 else {}
    delta = None
    if isinstance(latest.get("views"), int) and isinstance(prior.get("views"), int):
        delta = latest["views"] - prior["views"]
    return {"subs": latest.get("subscribers"), "views": latest.get("views"),
            "videos": latest.get("videos"), "delta_views": delta}


def _editor_signal() -> dict:
    g = _safe_json(STATE_DIR / "editor-guidance.json")
    if not g:
        return {}
    ranking = g.get("theme_ranking") or []
    lead = next((r for r in ranking if r.get("theme") == g.get("lead_theme")),
                ranking[0] if ranking else {})
    deemph = next((r for r in ranking if r.get("theme") == g.get("de_emphasize")), {})
    titles = g.get("winning_titles") or []
    top_videos = g.get("top_videos") or []
    top_video = top_videos[0] if top_videos else {}
    feedback = g.get("feedback_training") or g.get("performance_feedback") or {}
    pattern_lines = feedback.get("prompt_lines") or []
    winning_pattern = next(
        (
            re.sub(r"^Best observed pattern:\s*", "", str(line)).strip().rstrip(".")
            for line in pattern_lines
            if str(line).startswith("Best observed pattern:")
        ),
        None,
    )
    return {"lead_theme": g.get("lead_theme"), "lead_avg": lead.get("avg_views"),
            "deemph_theme": g.get("de_emphasize"),
            "top_video_title": top_video.get("title") or (titles[0] if titles else None),
            "top_video_views": top_video.get("views"),
            "top_video_delta": top_video.get("delta_views"),
            "winning_pattern": winning_pattern}


def _engagement_signal() -> dict:
    opp: dict = {}
    for r in _iter_jsonl(STATE_DIR / "listening-opportunities.jsonl"):
        if r.get("id"):
            opp[r["id"]] = r
    queue: dict = {}
    for r in _iter_jsonl(STATE_DIR / "engagement-queue.jsonl"):
        if r.get("id"):
            queue[r["id"]] = r
    drafted = sum(1 for r in queue.values() if r.get("status") == "drafted")
    scans = [r for r in _iter_jsonl(STATE_DIR / "engagement-log.jsonl") if r.get("event") == "scan"]
    last_scan = scans[-1] if scans else {}
    return {"surfaced": len(opp), "drafted_waiting": drafted,
            "last_scan_days": _days_ago(last_scan.get("ran_at"))}


def _real_lead_count() -> int:
    """Leads that are not synthetic fixtures (example.com / test- / fixture-)."""
    n = 0
    for r in _iter_jsonl(STATE_DIR / "leads.jsonl"):
        email = str(r.get("email", "")).lower()
        if "@" not in email:
            continue
        local, _, domain = email.partition("@")
        if domain in {"example.com", "example.org", "example.net"} or local.startswith(("test-", "fixture-")):
            continue
        n += 1
    return n


def _email_provider() -> str:
    m = re.search(r"^provider:\s*(\S+)", _read(ROOT / "config" / "email.yaml"), re.M)
    return m.group(1) if m else "none"


def render_daily_ops_markdown(dash: dict, state: State) -> str:
    """One-page daily brief: turn live system signals into today's sales moves."""
    today = dash["today"]
    goal = dash["goal"]
    vid = _video_activity()
    yt = _youtube_signal()
    ed = _editor_signal()
    eng = _engagement_signal()
    real_leads = _real_lead_count()

    lines: list[str] = [f"# Daily Ops — {today}", ""]

    # ---- Today's sales moves ------------------------------------------
    lines.append("## 🎯 Today's sales moves")
    step = 1

    # 1) Ship a video — the top reach lever, steered by what's winning.
    if vid["days_ago"] is None:
        move = "**Record & ship your first video** — nothing shipped yet; this is the #1 reach lever"
    elif vid["days_ago"] == 0:
        move = "**A video already shipped today** — line up tomorrow's so the streak holds"
    else:
        move = f"**Ship a video** — last one **{vid['days_ago']}d ago**"
    if ed.get("lead_theme"):
        move += f". Lead with **{ed['lead_theme']}**"
        signal_bits = []
        if ed.get("top_video_views") is not None:
            top_signal = f"top clip {ed['top_video_views']} views"
            if isinstance(ed.get("top_video_delta"), int) and ed["top_video_delta"] > 0:
                top_signal += f", +{ed['top_video_delta']}"
            signal_bits.append(top_signal)
        if ed.get("lead_avg") is not None:
            signal_bits.append(f"theme avg {ed['lead_avg']} views/clip")
        if signal_bits:
            move += f" ({'; '.join(signal_bits)})"
        if ed.get("deemph_theme"):
            move += f"; de-emphasize {ed['deemph_theme']}"
    lines.append(f"{step}. ▶ {move}.  → `/content`")
    if ed.get("top_video_title"):
        view_note = f" ({ed['top_video_views']} views)" if ed.get("top_video_views") is not None else ""
        lines.append(f"   - Title style that wins: _{ed['top_video_title']}_{view_note}")
    if ed.get("winning_pattern"):
        lines.append(f"   - Pattern to repeat: {ed['winning_pattern']}")
    step += 1

    # 2) Work the listening queue — free reach already surfaced.
    if eng.get("drafted_waiting"):
        emove = f"**Post {eng['drafted_waiting']} drafted repl{'y' if eng['drafted_waiting'] == 1 else 'ies'}** awaiting review"
    elif eng.get("surfaced"):
        emove = f"**Work the listening queue** — {eng['surfaced']} opportunities surfaced, none drafted yet"
    else:
        emove = "**Run a listening scan** to surface trader conversations"
    if eng.get("last_scan_days") is not None:
        emove += f" (last scan {eng['last_scan_days']}d ago)"
    lines.append(f"{step}. 💬 {emove} — free reach.  → `/engagement`")
    step += 1

    # 3) One partner per day (rotate) — not a wall of 8 overdue.
    todays_partner = pick_todays_partner(dash)
    if todays_partner:
        to = f" → **{todays_partner['email']}**" if todays_partner.get("email") else ""
        lines.append(f"{step}. ✉ **Email one partner: {todays_partner['name']}**{to} — draft at the bottom, edit & send.")
        step += 1
    lines.append("")

    # ---- Momentum (live signals, not hand-typed) ----------------------
    lines.append("## 📈 Momentum (live signals)")
    if yt:
        delta = f" (+{yt['delta_views']} since last pull)" if yt.get("delta_views") else ""
        lines.append(f"- YouTube: **{yt.get('subs', '?')}** subs · **{yt.get('views', '?')}** views{delta} · {yt.get('videos', '?')} videos")
    lines.append(f"- Engagement: {eng.get('surfaced', 0)} surfaced · {eng.get('drafted_waiting', 0)} drafted & waiting")
    if real_leads:
        lines.append(f"- Leads: **{real_leads}** captured")
    elif state.goal.lead_capture_live:
        lines.append("- Leads: capture **LIVE** (Kit checklist funnel) · app-side attribution not yet wired")
    else:
        lines.append("- Leads: **0** captured  ⚠ capture not live — reach is leaking")
    sales_line = f"- Sales: **{goal['customers']} / {goal['target_customers']}** (${state.goal.price} each)"
    if goal.get("days_remaining") is not None:
        sales_line += f" · {goal['days_remaining']} days left"
    lines.append(sales_line)
    lines.append("")

    # ---- Funnel breaks — fix these or reach can't convert -------------
    lines.append("## ⚠ Funnel — fix these or reach can't convert")
    if state.goal.checkout_live:
        lines.append(f"- [x] **Working ${state.goal.price} checkout**")
    else:
        lines.append(f"- [ ] **Working ${state.goal.price} checkout** ← BLOCKER: the site is up but can't take payment, so a sale is impossible today. Fix this first.")
    if real_leads or state.goal.lead_capture_live:
        lines.append("- [x] **Lead capture live** — Kit checklist landing page (point CTAs at it)")
    else:
        lines.append("- [ ] **Lead capture live** — publish the Kit checklist landing page + point CTAs at it")
    lines.append(f"- [ ] **Email nurture can send** — provider is `{_email_provider()}` (off until Kit is configured)")
    lines.append("- [ ] **Attribution** — wire Kit signups → `POST /api/leads` so they show here (view → click → lead → sale)")
    lines.append("")

    # ---- Pipeline summary (planning detail, condensed) ----------------
    p = dash["partnerships"]
    i = dash["influencers"]
    lines.append("## Pipeline")
    lines.append(f"- Partnerships: {p['total']} tracked, {len(p['overdue'])} overdue · {p['by_stage']}")
    lines.append(f"- Influencers: {i['total']} tracked · {i['by_stage']}")
    lines.append(f"- Content: {dash['content_calendar'].get('recorded', 0)} recorded / {dash['content_calendar'].get('planned', 0)} planned · Affiliates: {dash['affiliates_status']}")

    # ---- Today's single outreach draft --------------------------------
    if todays_partner:
        draft = draft_partnership_followup(_pipeline_item_from_dict(todays_partner))
        lines.append("")
        lines.append(f"## ✉ Outreach draft — {todays_partner['name']} (edit before sending)")
        if todays_partner.get("email"):
            lines.append(f"**To:** {todays_partner['email']}")
        lines.append("")
        lines.append("```")
        lines.append(draft.rstrip())
        lines.append("```")

    return "\n".join(lines)


def _pipeline_item_from_dict(d: dict) -> PipelineItem:
    """Reverse of __dict__ helper used in compute_dashboard()."""
    return PipelineItem(
        name=d["name"],
        stage=d["stage"],
        last_contact=d.get("last_contact", ""),
        next_action=d.get("next_action", ""),
        due=_parse_date(d.get("due", "")) if d.get("due") else None,
        raw_line=d.get("raw_line", ""),
        email=d.get("email", ""),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def pick_todays_partner(dash: dict) -> dict | None:
    """Deterministically rotate through partners needing outreach — one per day."""
    partners = dash["partnerships"]["overdue"] + dash["partnerships"]["due_today"]
    if not partners:
        return None
    return partners[date.today().toordinal() % len(partners)]


def _format_pipeline_line(it: PipelineItem) -> str:
    due = it.due.isoformat() if it.due else ""
    return (
        f"- **{it.name}** — stage: `{it.stage}` — "
        f"email: {it.email or '—'} — "
        f"last_contact: {it.last_contact or '—'} — "
        f"next_action: {it.next_action or '—'} — due: {due}"
    )


def mark_partner_contacted(name: str, *, when: str | None = None, followup_days: int = 7) -> bool:
    """Advance a partner to `reached_out`, stamp last_contact, and schedule the
    next follow-up. Returns True if the partner was found and the file updated."""
    path = STATE_DIR / "partnerships.md"
    text = _read(path)
    if not text:
        return False
    when = when or date.today().isoformat()
    next_due = (date.today() + timedelta(days=followup_days)).isoformat()
    out_lines: list[str] = []
    changed = False
    for line in text.splitlines():
        if not changed and line.strip().startswith("-"):
            item = _parse_pipeline_line(line)
            if item and item.name == name:
                item.stage = "reached_out"
                item.last_contact = when
                item.next_action = "Follow up on first outreach"
                item.due = _parse_date(next_due)
                out_lines.append(_format_pipeline_line(item))
                changed = True
                continue
        out_lines.append(line)
    if changed:
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changed


def run() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    dash = compute_dashboard(state)
    md = render_daily_ops_markdown(dash, state)
    out_path = OUT_DIR / f"{date.today().isoformat()}.md"
    out_path.write_text(md, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    configure_utf8_stdio()
    out = run()
    print(f"Daily ops written: {out}")
    sys.exit(0)

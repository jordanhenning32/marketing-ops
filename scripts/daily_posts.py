"""Daily social posts — one platform-native draft per platform, per day.

Most of these platforms have no usable write API, so the flow is: AI drafts a
post tailored to each platform; you copy it, paste it on the site, and tick it
off on /today. Posts are steered by the editor's winning theme and rotate a
content pillar each day so they don't get samey.

POSTURE: Reddit is value-first ONLY — no product name, no link, no pitch — to
respect the no-promo rule for that community (promo there can get the domain
banned). X / LinkedIn / Stocktwits lead with a LOW-FRICTION call to action: the
free Pre-Trade Risk-Control Checklist (the lead magnet, captured via the link in
the profile bio), NOT a hard $249 pitch — cold social traffic converts to a free
checklist, then the email nurture sells. They MAY still softly name the product
and reference the site (shadowedgetools.com) as proof, but the checklist is the
ask. Posts carry no inline URL (link lives in the bio); YouTube is secondary
proof, not the destination.

Store: state/daily-posts/<YYYY-MM-DD>.json — a list of post records.

CLI:
    python scripts/daily_posts.py generate        # (re)generate today's pending posts
    python scripts/daily_posts.py generate --force # regenerate all, even posted
    python scripts/daily_posts.py show
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from cli_io import configure_utf8_stdio

STATE_DIR = ROOT / "state"
POSTS_DIR = STATE_DIR / "daily-posts"
GUIDANCE_PATH = STATE_DIR / "editor-guidance.json"

try:
    from video_script import PRODUCT_CONTEXT
except Exception:  # pragma: no cover - fallback keeps the module importable
    PRODUCT_CONTEXT = (
        "Shadow Edge Tools makes risk-management add-ons for NinjaTrader 8 prop/futures "
        "traders: Drawdown Guardian (locks you out at your daily stop) and Bracket Boss "
        "(forces a stop/bracket onto every order)."
    )

# Platforms you post to (edit to add/remove). Reddit is value-first only.
ACTIVE_PLATFORMS = ["x", "linkedin", "stocktwits", "reddit"]

# One pillar per day, rotated, so the posts vary.
PILLARS = [
    ("discipline-story", "a 2-3 sentence story about the moment discipline breaks (revenge trade, a blown afternoon, almost giving back a good week)"),
    ("risk-tip", "one concrete, immediately usable risk-management tip a futures trader can apply today"),
    ("myth-bust", "bust one common myth prop/futures traders believe about risk, sizing, or discipline"),
    ("question", "an open question that invites traders to share how they personally handle risk or discipline"),
]

PLATFORM_RULES = {
    "x": (
        "Platform: X (Twitter). ONE punchy post, max ~270 characters, strong first-line hook, "
        "0-2 hashtags. Primary call to action: invite readers to grab the FREE Pre-Trade "
        "Risk-Control Checklist via the link in your bio (a low-friction first step) — NOT a hard "
        "purchase pitch. You MAY softly name the product/site (shadowedgetools.com) as proof; a full "
        "walkthrough on YouTube is fine to mention. Do NOT include a URL (the link lives in your bio)."
    ),
    "linkedin": (
        "Platform: LinkedIn. A short professional post (~100-130 words, 3-5 short paragraphs), "
        "story-driven and credible, strong first-line hook. Primary call to action: point readers to "
        "the FREE Pre-Trade Risk-Control Checklist via the link in your bio (low-friction first step) "
        "rather than a hard pitch. You MAY mention the product softly and reference the site "
        "(shadowedgetools.com) as proof, and/or invite a follow. At most 3 hashtags. No inline URL."
    ),
    "stocktwits": (
        "Platform: Stocktwits. A very short trader-native take (1-3 sentences). Reference a relevant "
        "futures ticker like $ES_F or $NQ_F where it fits naturally. Casual trader voice. Primary call "
        "to action: nudge readers to the FREE Pre-Trade Risk-Control Checklist via the link in your bio "
        "(low-friction first step); you MAY name the product/site (shadowedgetools.com) as proof. No inline URL."
    ),
    "reddit": (
        "Platform: Reddit (trading subreddits). STRICT value-first contribution: a genuinely helpful "
        "insight, lesson, or question that stands entirely on its own. Do NOT name the product, do NOT "
        "include any link, do NOT pitch or hint at anything for sale. Sound like a real trader sharing "
        "hard-won experience. This builds profile credibility only — promo here gets the domain banned."
    ),
}


def _load_guidance() -> dict:
    try:
        return json.loads(GUIDANCE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _todays_pillar(d: date) -> tuple[str, str]:
    return PILLARS[d.toordinal() % len(PILLARS)]


def _today_path(d: date) -> Path:
    return POSTS_DIR / f"{d.isoformat()}.json"


def load_posts(d: date | None = None) -> list[dict]:
    try:
        return json.loads(_today_path(d or date.today()).read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_posts(posts: list[dict], d: date) -> None:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    _today_path(d).write_text(json.dumps(posts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize_post_guidance(post_guidance: str | None) -> str:
    return (post_guidance or "").strip()[:2000]


def _generate_one(platform: str, pillar: tuple[str, str], guidance: dict, *, post_guidance: str = "") -> dict:
    import llm_router

    theme = guidance.get("lead_theme") or "drawdown-guardian"
    pillar_key, pillar_desc = pillar
    post_guidance = _normalize_post_guidance(post_guidance)
    post_guidance_block = ""
    if post_guidance:
        post_guidance_block = (
            "\n\nDaily post direction from Jordan:\n"
            "Use this to choose the topic, feature emphasis, examples, and hook for this run. "
            "Follow it unless it conflicts with the platform rules above.\n"
            f"{post_guidance}\n"
        )
    system = (
        "You write short, platform-native social posts for a futures-trading software brand. "
        + PRODUCT_CONTEXT
        + " Audience: prop/funded futures traders with elite BS-detectors — value-first, sound like a "
        "real trader, never hypey or ad-like. Output ONLY the post text — no preamble, no surrounding "
        "quotes, no markdown headers."
    )
    user = (
        f"{PLATFORM_RULES[platform]}\n\n"
        f"Today's angle ({pillar_key}): {pillar_desc}.\n"
        f"Tie it to the theme that's resonating right now: {theme} "
        "(emotional discipline / not blowing the account), without being repetitive.\n"
        f"{post_guidance_block}"
        "Write today's post now."
    )
    result = llm_router.complete("drafting", system=system, user=user, max_tokens=500)
    post = {
        "platform": platform,
        "pillar": pillar_key,
        "text": result.text.strip(),
        "status": "pending",
        "posted_url": "",
        "posted_at": "",
        "provider": result.provider,
        "model": result.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "post_guidance_applied": bool(post_guidance),
    }
    if post_guidance:
        post["post_guidance"] = post_guidance
    return post


def generate_daily_posts(platforms: list[str] | None = None, *, d: date | None = None,
                         force: bool = False, only: str | None = None,
                         post_guidance: str = "") -> dict:
    """Generate today's posts. Keeps already-posted/skipped drafts unless `force`;
    `only` regenerates a single platform (leaving the rest untouched)."""
    d = d or date.today()
    platforms = platforms or ACTIVE_PLATFORMS
    guidance = _load_guidance()
    post_guidance = _normalize_post_guidance(post_guidance)
    pillar = _todays_pillar(d)
    existing = {p["platform"]: p for p in load_posts(d)}
    out: list[dict] = []
    generated = 0
    errors: list[str] = []
    for plat in platforms:
        prev = existing.get(plat)
        if only:
            regen = plat == only
        else:
            regen = force or prev is None or prev.get("status") == "pending"
        if not regen and prev is not None:
            out.append(prev)
            continue
        try:
            out.append(_generate_one(plat, pillar, guidance, post_guidance=post_guidance))
            generated += 1
        except Exception as exc:  # noqa: BLE001 - keep the others; surface the reason
            errors.append(f"{plat}: {exc}")
            if prev is not None:
                out.append(prev)
    _save_posts(out, d)
    return {
        "date": d.isoformat(),
        "generated": generated,
        "pillar": pillar[0],
        "errors": errors,
        "posts": out,
        "post_guidance_applied": bool(post_guidance),
    }


def set_post_status(platform: str, action: str, *, posted_url: str = "", d: date | None = None) -> bool:
    d = d or date.today()
    posts = load_posts(d)
    changed = False
    for p in posts:
        if p.get("platform") == platform:
            if action == "posted":
                p["status"] = "posted"
                p["posted_url"] = posted_url
                p["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            elif action == "skipped":
                p["status"] = "skipped"
            elif action == "pending":
                p["status"] = "pending"
                p["posted_url"] = ""
                p["posted_at"] = ""
            else:
                raise ValueError(f"unsupported post action: {action}")
            changed = True
            break
    if changed:
        _save_posts(posts, d)
    return changed


def posting_streak() -> int:
    """Consecutive days (ending today) with at least one post marked posted.
    Today not-yet-posted does not break the streak."""
    streak = 0
    cur = date.today()
    for _ in range(366):
        posts = load_posts(cur)
        posted = bool(posts) and any(p.get("status") == "posted" for p in posts)
        if posted:
            streak += 1
        elif cur != date.today():
            break
        cur = cur - timedelta(days=1)
    return streak


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Generate daily platform-native social posts.")
    sub = parser.add_subparsers(dest="cmd")
    gen = sub.add_parser("generate")
    gen.add_argument("--force", action="store_true")
    gen.add_argument("--only", default=None)
    sub.add_parser("show")
    args = parser.parse_args(argv)

    if args.cmd == "show":
        for p in load_posts():
            print(f"[{p['status']:7}] {p['platform']:10} ({p.get('pillar','')}) — {p['text'][:80]}")
        return 0

    res = generate_daily_posts(force=getattr(args, "force", False), only=getattr(args, "only", None))
    print(f"Generated {res['generated']} post(s) for {res['date']} (pillar: {res['pillar']})")
    if res["errors"]:
        print("Errors: " + "; ".join(res["errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

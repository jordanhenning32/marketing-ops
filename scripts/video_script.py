"""Next-video script generator.

Turns the editor's winning-theme signal (state/editor-guidance.json) into a
record-ready vertical-Short script, so a solo founder only has to film. Uses the
tiered LLM router (multi-provider fallback) so it keeps working if one provider
is down. Offline-safe: a missing signal degrades to the known lead theme; a
missing API key surfaces a clear error.

CLI:
    python scripts/video_script.py            # generate + write state/next-video-script.md
    python scripts/video_script.py --show      # print the latest stored script
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from cli_io import configure_utf8_stdio

STATE_DIR = ROOT / "state"
GUIDANCE_PATH = STATE_DIR / "editor-guidance.json"
SCRIPT_PATH = STATE_DIR / "next-video-script.md"

PRODUCT_CONTEXT = (
    "Shadow Edge Tools makes risk-management add-ons for NinjaTrader 8 futures / prop-firm "
    "traders. Two products: Drawdown Guardian (locks the trader out at their daily stop, "
    "optional auto-flatten, a live discipline score) and Bracket Boss (forces a stop/bracket "
    "onto every order, risk-based position sizing, an order lock after a red trade). "
    "Positioning: put the rule where the mistake happens — risk controls at the execution "
    "moment, not in a notebook. Audience: funded/prop futures traders with elite BS-detectors, "
    "so it must be value-first and sound like a real trader, never an ad."
)


def _load_guidance() -> dict:
    try:
        return json.loads(GUIDANCE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_prompt(guidance: dict) -> tuple[str, str]:
    ranking = guidance.get("theme_ranking") or []
    lead = guidance.get("lead_theme") or (ranking[0].get("theme") if ranking else "drawdown-guardian")
    deemph = guidance.get("de_emphasize")
    titles = guidance.get("winning_titles") or []

    system = (
        "You are a short-form video scriptwriter for a futures-trading software brand. "
        "Write a record-ready script for a 30-45 second vertical Short the founder will film "
        "himself, talking to camera. " + PRODUCT_CONTEXT + " "
        "Hard rules: open on the emotional stakes (the bad afternoon, revenge trading, almost "
        "blowing the account) in the first 2 seconds, BEFORE any mechanics. Short, punchy spoken "
        "lines that read well on a muted phone (captions are burned in). No emojis. No hype words. "
        "End on a soft, profile-based CTA — never a hard sell and never a link."
    )

    user_parts = [f"Lead with this winning theme: {lead}."]
    if deemph:
        user_parts.append(f"De-emphasize: {deemph} (these clips underperform).")
    if titles:
        user_parts.append(
            "Titles that have worked — match this punchy, outcome-first style:\n- "
            + "\n- ".join(titles[:3])
        )
    user_parts.append(
        "Output EXACTLY this markdown structure and nothing else:\n"
        "## Title\n(one punchy, outcome-first title with 2-3 hashtags)\n\n"
        "## Hook (0-2s)\n(one spoken line)\n\n"
        "## Script (read on camera)\n(6-10 short spoken lines, one line each)\n\n"
        "## On-screen text\n(3-5 short caption phrases)\n\n"
        "## CTA\n(one soft spoken line)\n\n"
        "## Shot notes\n(2-3 bullets on what to show)"
    )
    return system, "\n\n".join(user_parts)


def generate_next_video_script(*, persist: bool = True) -> dict:
    """Generate a record-ready Short script from the current editor signal."""
    import llm_router

    guidance = _load_guidance()
    system, user = build_prompt(guidance)
    result = llm_router.complete("drafting", system=system, user=user, max_tokens=1200)

    theme = guidance.get("lead_theme") or "drawdown-guardian"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        f"# Next Video Script — {date.today().isoformat()}\n\n"
        f"> Theme: **{theme}** · drafted by {result.provider}/{result.model} · {stamp}\n"
        f"> Edit freely, then record. The pipeline burns captions + auto-uploads on approval.\n\n"
    )
    markdown = header + result.text.strip() + "\n"

    if persist:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPT_PATH.write_text(markdown, encoding="utf-8")

    return {
        "ok": True,
        "path": str(SCRIPT_PATH),
        "theme": theme,
        "provider": result.provider,
        "model": result.model,
        "markdown": markdown,
    }


def latest_script() -> str:
    try:
        return SCRIPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Generate the next video script from the editor signal.")
    parser.add_argument("--show", action="store_true", help="print the latest stored script and exit")
    args = parser.parse_args(argv)

    if args.show:
        text = latest_script()
        sys.stdout.write(text or "(no script generated yet)\n")
        return 0

    res = generate_next_video_script()
    print(f"Wrote {res['path']} via {res['provider']}/{res['model']} (theme: {res['theme']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

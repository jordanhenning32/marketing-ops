"""Scout — discovery + research helpers.

Two affiliate-focused capabilities used by the /affiliates page:

  research_affiliate_url(url) -> dict
      Fetch a candidate program's page, extract structured details (commission,
      cookie window, payout, signup URL) with Claude, return a dict ready to
      be pre-filled into the affiliates 'Add' form.

  discover_affiliate_candidates(...) -> list[dict]
      Scan a few prop-trading / day-trading subreddits via Reddit's public JSON
      search endpoint for affiliate / referral / promo-code mentions. Pull out
      candidate program names, dedupe against the tracked list, return a list
      of candidates with the source posts.

This module also serves as the foundation for the future daily Scout briefing
(prop firm news, conversations to engage). For now only the affiliate paths
are wired up.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

# Load env (Anthropic key, etc.)
try:
    from dotenv import load_dotenv
    env_root = Path(__file__).resolve().parent.parent
    explicit_env = os.environ.get("MARKETING_OPS_ENV_FILE")
    for candidate in (
        Path(explicit_env) if explicit_env else None,
        env_root / ".env",
        env_root.parent / ".ENV",
        env_root.parent / ".env",
    ):
        if candidate and candidate.exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import affiliates as affiliates_mod  # noqa: E402

ROOT = _HERE.parent
SCOUT_CACHE_DIR = ROOT / "briefings" / "scout-cache"

REQUEST_HEADERS = {
    "User-Agent": (
        "ShadowEdgeOps/0.1 (research bot, contact: ops@shadowedgetools.com)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Reddit's API enforces a specific User-Agent format:
#   <platform>:<app id>:<version> (by /u/<reddit username>)
REDDIT_USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "windows:shadowedgeops:0.1 (by /u/shadowedgetools)",
)
REDDIT_TOKEN_CACHE: dict = {}  # in-memory; survives within one process


# ---------------------------------------------------------------------------
# URL research
# ---------------------------------------------------------------------------

@dataclass
class AffiliateResearchResult:
    url: str
    name: str = ""
    commission: str = ""
    cookie_days: Optional[int] = None
    payout_method: str = ""
    signup_url: str = ""
    minimum_payout: Optional[str] = None
    notes: str = ""
    extracted_confidence: float = 0.0
    raw_excerpt: str = ""
    error: Optional[str] = None
    fetched_at: str = ""


def _fetch_html(url: str, timeout: float = 20.0) -> str:
    with httpx.Client(
        headers=REQUEST_HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def _html_to_text(html: str) -> str:
    """Strip nav/scripts/styles and return concatenated text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:18000]  # generous slice; Claude has plenty of context


_RESEARCH_SYSTEM = """\
You extract affiliate-program details from a single web page for a trading-tools
business that wants to apply to the program. Be CONSERVATIVE: if a field is not
explicitly stated, leave it blank. Never invent commission rates or terms.

Return STRICT JSON in this exact shape:

{
  "name": "(the program/brand name, e.g. 'Apex Trader Funding')",
  "commission": "(verbatim or near-verbatim — e.g. '20% recurring' or '$50 per signup, tiered')",
  "cookie_days": (integer days or null),
  "payout_method": "(e.g. 'PayPal', 'ACH', 'wire', or '' if not stated)",
  "signup_url": "(direct link to apply if mentioned, otherwise '')",
  "minimum_payout": "(e.g. '$100' or '' if not stated)",
  "notes": "(1–3 sentences of useful context — tier structure, restrictions, niche fit, red flags)",
  "extracted_confidence": (float 0.0–1.0 — your confidence the page actually describes a real affiliate program with concrete terms),
  "raw_excerpt": "(the most relevant 400-char excerpt from the page, verbatim)"
}

If the page is not actually an affiliate-program page, set name to a best guess,
extracted_confidence to 0.1 or lower, and notes to "(page does not appear to
describe an affiliate program)".

Output ONLY the JSON object. No prose, no markdown fence.
"""


def _claude_extract_affiliate(url: str, page_text: str) -> dict:
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY missing. Add it to E:/marketing-ops/.env (no BOM)."
        )
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=os.environ.get("SCOUT_MODEL", "claude-sonnet-4-6"),
        max_tokens=1500,
        system=_RESEARCH_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"URL: {url}\n\nPAGE TEXT:\n{page_text}",
        }],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    raw = raw.strip()
    # Strip ```json fences if model added them despite the instruction
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model returned non-JSON: {exc}\nRaw: {raw[:500]}")


def research_affiliate_url(url: str) -> AffiliateResearchResult:
    """Fetch URL, extract affiliate details, return structured result."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = AffiliateResearchResult(url=url, fetched_at=datetime.now().isoformat(timespec="seconds"))
    try:
        html = _fetch_html(url)
        text = _html_to_text(html)
        if len(text) < 100:
            result.error = "Page returned little/no text. Login wall, JS-rendered, or blocked."
            return result
        extracted = _claude_extract_affiliate(url, text)
    except httpx.HTTPError as exc:
        result.error = f"Fetch failed: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"Extract failed: {exc}"
        return result

    result.name = (extracted.get("name") or "").strip()
    result.commission = (extracted.get("commission") or "").strip()
    cd = extracted.get("cookie_days")
    if isinstance(cd, int):
        result.cookie_days = cd
    elif isinstance(cd, str) and cd.isdigit():
        result.cookie_days = int(cd)
    result.payout_method = (extracted.get("payout_method") or "").strip()
    result.signup_url = (extracted.get("signup_url") or "").strip() or url
    result.minimum_payout = (extracted.get("minimum_payout") or "") or None
    result.notes = (extracted.get("notes") or "").strip()
    try:
        result.extracted_confidence = float(extracted.get("extracted_confidence", 0))
    except (TypeError, ValueError):
        result.extracted_confidence = 0.0
    result.raw_excerpt = (extracted.get("raw_excerpt") or "").strip()
    return result


# ---------------------------------------------------------------------------
# Reddit discovery (public JSON, no auth)
# ---------------------------------------------------------------------------

DEFAULT_SUBREDDITS = [
    "FuturesTrading",
    "Daytrading",
    "propfirm",
    "RealDayTrading",
    "Trading",
]

DEFAULT_SEARCH_TERMS = [
    "affiliate",
    "referral code",
    "promo code",
    "discount code",
    "%off",
]


@dataclass
class RedditPost:
    subreddit: str
    title: str
    permalink: str
    created_utc: float
    score: int
    num_comments: int
    author: str
    self_text_excerpt: str

    @property
    def reddit_url(self) -> str:
        return f"https://www.reddit.com{self.permalink}"


class RedditAuthError(RuntimeError):
    """Raised when Reddit credentials are missing or invalid."""


def _get_reddit_token() -> str:
    """Fetch (and cache for ~50 min) an app-only OAuth token via client_credentials.

    Setup: user creates a 'script' type app at reddit.com/prefs/apps,
    sets REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET in marketing-ops/.env.
    No Reddit account login is required — read-only app credentials only.
    """
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not csec:
        raise RedditAuthError(
            "Reddit API credentials missing. Create a 'script' app at "
            "reddit.com/prefs/apps and set REDDIT_CLIENT_ID + "
            "REDDIT_CLIENT_SECRET in marketing-ops/.env."
        )

    cached = REDDIT_TOKEN_CACHE.get("token")
    expires_at = REDDIT_TOKEN_CACHE.get("expires_at", 0)
    if cached and time.time() < expires_at:
        return cached

    auth = httpx.BasicAuth(cid, csec)
    headers = {"User-Agent": REDDIT_USER_AGENT}
    data = {
        "grant_type": "client_credentials",
        "device_id": "DO_NOT_TRACK_THIS_DEVICE",
    }
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        resp = client.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=auth,
            data=data,
            headers=headers,
        )
    if resp.status_code != 200:
        raise RedditAuthError(
            f"Token request failed (HTTP {resp.status_code}). "
            "Verify REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are correct."
        )
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise RedditAuthError(f"No access_token in response: {payload}")
    expires_in = int(payload.get("expires_in", 3600))
    REDDIT_TOKEN_CACHE["token"] = token
    REDDIT_TOKEN_CACHE["expires_at"] = time.time() + max(60, expires_in - 300)
    return token


def _reddit_search(subreddit: str, query: str, limit: int = 25, t: str = "year") -> list[RedditPost]:
    """Search a subreddit via Reddit's OAuth API (oauth.reddit.com).

    Raises RedditAuthError if credentials are missing or invalid; we let it
    bubble up so the caller can surface a helpful UI message.
    """
    token = _get_reddit_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/json",
    }
    url = f"https://oauth.reddit.com/r/{subreddit}/search"
    params = {
        "q": query,
        "restrict_sr": "on",
        "sort": "new",
        "limit": str(limit),
        "t": t,
    }
    posts: list[RedditPost] = []
    with httpx.Client(headers=headers, timeout=httpx.Timeout(15.0)) as client:
        resp = client.get(url, params=params)
        if resp.status_code == 401:
            REDDIT_TOKEN_CACHE.clear()
            raise RedditAuthError("Reddit token rejected (401). Re-check credentials.")
        if resp.status_code != 200:
            return posts
        try:
            data = resp.json()
        except ValueError:
            return posts
    for child in data.get("data", {}).get("children", []) or []:
        d = child.get("data") or {}
        body = d.get("selftext") or ""
        posts.append(RedditPost(
            subreddit=d.get("subreddit", subreddit),
            title=d.get("title", "")[:280],
            permalink=d.get("permalink", ""),
            created_utc=d.get("created_utc", 0.0),
            score=d.get("score", 0),
            num_comments=d.get("num_comments", 0),
            author=d.get("author", ""),
            self_text_excerpt=body[:500],
        ))
    return posts


def reddit_credentials_status() -> dict:
    """Lightweight check the UI can call to decide whether to show a setup card."""
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csec = os.environ.get("REDDIT_CLIENT_SECRET")
    return {
        "configured": bool(cid and csec),
        "user_agent": REDDIT_USER_AGENT,
    }


_DISCOVER_SYSTEM = """\
You analyze a list of Reddit posts that mention affiliate / referral / promo
codes in trading-related subreddits. Identify SPECIFIC affiliate programs (prop
firms, broker tools, charting tools, education platforms) that a futures-trading
tools business should consider applying to as an affiliate.

For each unique program you can identify:
- Use the brand's canonical name (e.g. "Apex Trader Funding" not "apex" or "ATF")
- Provide an `affiliate_url_guess` ONLY if you're confident — otherwise leave ""
- Cite which post(s) referenced it via post indices

Return STRICT JSON ONLY (no prose, no markdown fence) in this shape:

{
  "candidates": [
    {
      "name": "Apex Trader Funding",
      "category": "prop_firm",
      "why_relevant": "1-2 sentence rationale tied to our futures-prop-trader audience",
      "affiliate_url_guess": "",
      "source_post_indices": [0, 4]
    }
  ]
}

Categories you may use: "prop_firm" | "broker" | "charting_tool" | "education"
| "ninjatrader_addon" | "indicator" | "other". Skip programs that are clearly
not relevant to futures day traders. Skip duplicates of the same brand.
"""


def _claude_extract_candidates(posts: list[RedditPost], known_names: set[str]) -> list[dict]:
    if not posts:
        return []
    from anthropic import Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing.")

    # Render compact post summaries the model can index by integer
    rendered = []
    for i, p in enumerate(posts):
        rendered.append(
            f"[{i}] r/{p.subreddit} · score={p.score} · {p.title}\n"
            f"    {p.self_text_excerpt[:300]}"
        )
    posts_block = "\n".join(rendered)

    client = Anthropic(api_key=api_key)
    user = (
        f"Programs we already track (skip these or note the existing entry):\n"
        f"{', '.join(sorted(known_names)) or '(none)'}\n\n"
        f"Posts (numbered):\n{posts_block}"
    )
    msg = client.messages.create(
        model=os.environ.get("SCOUT_MODEL", "claude-sonnet-4-6"),
        max_tokens=2500,
        system=_DISCOVER_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data.get("candidates", []) or []


@dataclass
class DiscoverResult:
    ran_at: str
    subreddits: list[str]
    search_terms: list[str]
    posts_seen: int
    candidates: list[dict] = field(default_factory=list)
    error: Optional[str] = None


def discover_affiliate_candidates(
    *,
    subreddits: Optional[list[str]] = None,
    terms: Optional[list[str]] = None,
    limit_per_query: int = 15,
    timeframe: str = "year",
) -> DiscoverResult:
    subs = subreddits or DEFAULT_SUBREDDITS
    qterms = terms or DEFAULT_SEARCH_TERMS
    out = DiscoverResult(
        ran_at=datetime.now().isoformat(timespec="seconds"),
        subreddits=list(subs),
        search_terms=list(qterms),
        posts_seen=0,
    )

    posts: list[RedditPost] = []
    seen_perma = set()
    auth_error: Optional[str] = None
    for sub in subs:
        if auth_error:
            break
        for term in qterms:
            try:
                batch = _reddit_search(sub, term, limit=limit_per_query, t=timeframe)
            except RedditAuthError as exc:
                auth_error = str(exc)
                break
            except Exception:  # noqa: BLE001
                batch = []
            for p in batch:
                if p.permalink in seen_perma:
                    continue
                seen_perma.add(p.permalink)
                posts.append(p)
            time.sleep(1.2)  # be polite to Reddit's rate limit

    if auth_error:
        out.error = auth_error
        return out

    out.posts_seen = len(posts)
    if not posts:
        out.error = "No posts returned for these search terms in the chosen timeframe."
        return out

    # Build the known-names set for dedupe
    state = affiliates_mod.load_state()
    known = {a.name.strip().lower() for a in state.affiliates if not a.archived}
    # Also add common aliases the seeded entries use
    aliases = {
        "apex trader funding": ["apex", "atf"],
        "topstep": ["topstep trader", "topsteptrader", "tst"],
        "myfundedfutures": ["mff", "myff"],
        "tradeify": [],
    }
    for canonical, alist in aliases.items():
        if canonical in known:
            for a in alist:
                known.add(a)

    try:
        cands = _claude_extract_candidates(posts, known)
    except Exception as exc:  # noqa: BLE001
        out.error = f"Extraction failed: {exc}"
        return out

    # Annotate each candidate with the actual referenced posts
    for c in cands:
        idxs = c.get("source_post_indices") or []
        c["sources"] = []
        for i in idxs:
            if isinstance(i, int) and 0 <= i < len(posts):
                p = posts[i]
                c["sources"].append({
                    "subreddit": p.subreddit,
                    "title": p.title,
                    "url": p.reddit_url,
                    "score": p.score,
                })
        # Final dedupe pass: drop if name matches anything in known
        if c.get("name", "").strip().lower() in known:
            c["already_tracked"] = True
    out.candidates = cands
    return out


# ---------------------------------------------------------------------------
# Cache (so the discovery page can show the last result without re-running)
# ---------------------------------------------------------------------------

CACHE_PATH = SCOUT_CACHE_DIR / "affiliate-discovery.json"


def save_discovery(result: DiscoverResult) -> None:
    SCOUT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def load_discovery() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Shadow Edge Scout")
    sub = p.add_subparsers(dest="cmd")

    research = sub.add_parser("research", help="Extract affiliate details from a URL")
    research.add_argument("url")

    discover = sub.add_parser("discover", help="Scan Reddit for affiliate-program candidates")
    discover.add_argument("--timeframe", default="year", choices=["day", "week", "month", "year", "all"])

    args = p.parse_args()
    if args.cmd == "research":
        r = research_affiliate_url(args.url)
        print(json.dumps(asdict(r), indent=2))
    elif args.cmd == "discover":
        r = discover_affiliate_candidates(timeframe=args.timeframe)
        save_discovery(r)
        print(json.dumps(asdict(r), indent=2))
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

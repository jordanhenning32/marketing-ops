"""Affiliate-applications tracker.

JSON-backed state at `state/affiliate-applications.json`. The console renders
a list of affiliate cards each with a checklist, metadata, and notes. Users
can add new applications, toggle individual checklist items, and edit fields
in-place.

Distinct from `state/partnerships.md`:
- Partnerships: someone else partners with US (e.g., Apex lists Shadow Edge as
  an approved vendor)
- Affiliates: WE join their affiliate program to earn commission on referrals

Public surface:
    load_state() -> AffiliateState
    save_state(state)
    create(name, url=None, commission=None, ...) -> Affiliate
    update(id, **patches) -> Affiliate | None
    toggle_check(id, item_id) -> Affiliate | None
    add_checklist_item(id, label) -> Affiliate | None
    remove_checklist_item(id, item_id) -> Affiliate | None
    archive(id) / unarchive(id) / delete(id)
    summary() -> dict (counts for the dashboard)
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "affiliate-applications.json"

_LOCK = threading.Lock()

DEFAULT_CHECKLIST_TEMPLATE = [
    ("research", "Researched program terms & commission"),
    ("prereqs", "Eligibility prerequisites met (channel / audience / site)"),
    ("ftc", "FTC disclosure live on website"),
    ("application", "Application submitted"),
    ("approval", "Approval received"),
    ("link", "Affiliate link / code generated"),
    ("content", "Featured in a content piece (blog / video / email)"),
    ("conversion", "First conversion tracked"),
]

# Stage values (mapped to UI pill colors)
VALID_STATUSES = [
    "not_started",
    "researching",
    "applied",
    "approved",
    "active",
    "declined",
    "archived",
]

VALID_PRIORITIES = ["high", "medium", "low"]


@dataclass
class ChecklistItem:
    id: str
    label: str
    done: bool = False
    done_at: Optional[str] = None


@dataclass
class Affiliate:
    id: str
    name: str
    url: str = ""
    commission: str = ""
    cookie_days: Optional[int] = None
    payout_method: str = ""
    contact: str = ""
    priority: str = "medium"
    status: str = "not_started"
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""

    def progress(self) -> tuple[int, int]:
        done = sum(1 for c in self.checklist if c.done)
        return done, len(self.checklist)

    def progress_pct(self) -> int:
        d, t = self.progress()
        return int(round((d / t) * 100)) if t else 0


@dataclass
class AffiliateState:
    version: int = 1
    affiliates: list[Affiliate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s\-]", "", (s or "").strip().lower())
    s = re.sub(r"\s+", "-", s)
    return s[:50].strip("-") or f"affiliate-{int(datetime.now().timestamp())}"


def _default_checklist() -> list[ChecklistItem]:
    return [ChecklistItem(id=k, label=v) for k, v in DEFAULT_CHECKLIST_TEMPLATE]


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_state() -> AffiliateState:
    with _LOCK:
        if not STATE_PATH.exists():
            return AffiliateState(affiliates=[])
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return AffiliateState(affiliates=[])
        affs: list[Affiliate] = []
        for entry in raw.get("affiliates", []):
            cl = [ChecklistItem(**c) for c in entry.get("checklist", [])]
            entry_clean = {k: v for k, v in entry.items() if k != "checklist"}
            affs.append(Affiliate(**entry_clean, checklist=cl))
        return AffiliateState(version=raw.get("version", 1), affiliates=affs)


def save_state(state: AffiliateState) -> None:
    with _LOCK:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": state.version,
            "affiliates": [asdict(a) for a in state.affiliates],
        }
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _by_id(state: AffiliateState, aff_id: str) -> Optional[Affiliate]:
    for a in state.affiliates:
        if a.id == aff_id:
            return a
    return None


def _ensure_unique_id(state: AffiliateState, base: str) -> str:
    existing = {a.id for a in state.affiliates}
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def create(
    name: str,
    *,
    url: str = "",
    commission: str = "",
    cookie_days: Optional[int] = None,
    payout_method: str = "",
    contact: str = "",
    priority: str = "medium",
    status: str = "not_started",
    notes: str = "",
) -> Affiliate:
    state = load_state()
    aff_id = _ensure_unique_id(state, _slugify(name))
    if priority not in VALID_PRIORITIES:
        priority = "medium"
    if status not in VALID_STATUSES:
        status = "not_started"
    aff = Affiliate(
        id=aff_id,
        name=name.strip(),
        url=url.strip(),
        commission=commission.strip(),
        cookie_days=cookie_days,
        payout_method=payout_method.strip(),
        contact=contact.strip(),
        priority=priority,
        status=status,
        notes=notes,
        checklist=_default_checklist(),
        created_at=_now(),
        updated_at=_now(),
    )
    state.affiliates.append(aff)
    save_state(state)
    return aff


_ALLOWED_UPDATE_FIELDS = {
    "name", "url", "commission", "cookie_days", "payout_method", "contact",
    "priority", "status", "notes", "tags",
}


def update(aff_id: str, **patches) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff:
        return None
    for k, v in patches.items():
        if k not in _ALLOWED_UPDATE_FIELDS:
            continue
        if k == "priority" and v not in VALID_PRIORITIES:
            continue
        if k == "status" and v not in VALID_STATUSES:
            continue
        if k == "cookie_days":
            try:
                v = int(v) if v not in (None, "", "—") else None
            except (TypeError, ValueError):
                v = None
        if k == "tags" and isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        setattr(aff, k, v)
    aff.updated_at = _now()
    save_state(state)
    return aff


def toggle_check(aff_id: str, item_id: str) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff:
        return None
    for item in aff.checklist:
        if item.id == item_id:
            item.done = not item.done
            item.done_at = _now() if item.done else None
            break
    else:
        return aff  # item not found, no change
    aff.updated_at = _now()
    # Auto-bump status forward when canonical milestones complete
    if item_id == "application" and item.done and aff.status in {"not_started", "researching"}:
        aff.status = "applied"
    elif item_id == "approval" and item.done and aff.status in {"not_started", "researching", "applied"}:
        aff.status = "approved"
    elif item_id == "content" and item.done and aff.status in {"approved"}:
        aff.status = "active"
    save_state(state)
    return aff


def add_checklist_item(aff_id: str, label: str) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff or not label.strip():
        return aff
    item_id = _slugify(label)
    existing_ids = {c.id for c in aff.checklist}
    n = 2
    while item_id in existing_ids:
        item_id = f"{_slugify(label)}-{n}"
        n += 1
    aff.checklist.append(ChecklistItem(id=item_id, label=label.strip()))
    aff.updated_at = _now()
    save_state(state)
    return aff


def remove_checklist_item(aff_id: str, item_id: str) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff:
        return None
    aff.checklist = [c for c in aff.checklist if c.id != item_id]
    aff.updated_at = _now()
    save_state(state)
    return aff


def archive(aff_id: str) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff:
        return None
    aff.archived = True
    aff.updated_at = _now()
    save_state(state)
    return aff


def unarchive(aff_id: str) -> Optional[Affiliate]:
    state = load_state()
    aff = _by_id(state, aff_id)
    if not aff:
        return None
    aff.archived = False
    aff.updated_at = _now()
    save_state(state)
    return aff


def delete(aff_id: str) -> bool:
    state = load_state()
    before = len(state.affiliates)
    state.affiliates = [a for a in state.affiliates if a.id != aff_id]
    save_state(state)
    return len(state.affiliates) != before


# ---------------------------------------------------------------------------
# Aggregates / summary
# ---------------------------------------------------------------------------

def summary() -> dict:
    state = load_state()
    total = len(state.affiliates)
    active = [a for a in state.affiliates if not a.archived]
    by_status: dict[str, int] = {}
    total_done = 0
    total_items = 0
    for a in active:
        by_status[a.status] = by_status.get(a.status, 0) + 1
        d, t = a.progress()
        total_done += d
        total_items += t
    return {
        "total": total,
        "active": len(active),
        "archived": total - len(active),
        "by_status": by_status,
        "checklist_done": total_done,
        "checklist_total": total_items,
        "checklist_pct": int(round((total_done / total_items) * 100)) if total_items else 0,
    }


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

SEED_AFFILIATES = [
    {"name": "Apex Trader Funding",  "url": "https://apextraderfunding.com/affiliate-program",  "priority": "high"},
    {"name": "Topstep",              "url": "https://www.topstep.com/affiliates",               "priority": "high"},
    {"name": "MyFundedFutures",      "url": "https://myfundedfutures.com/affiliates",           "priority": "high"},
    {"name": "Tradeify",             "url": "https://tradeify.co/affiliates",                   "priority": "medium"},
    {"name": "Take Profit Trader",   "url": "https://takeprofittrader.com/affiliate",           "priority": "medium"},
    {"name": "Earn2Trade",           "url": "https://earn2trade.com/affiliate",                 "priority": "medium"},
    {"name": "Bulenox",              "url": "https://bulenox.com/affiliate",                    "priority": "low"},
    {"name": "TradeDay",             "url": "https://tradeday.com/affiliate",                   "priority": "low"},
    {"name": "TradingView",          "url": "https://www.tradingview.com/affiliate-program/",   "priority": "medium"},
    {"name": "NinjaTrader (vendor)", "url": "https://ninjatraderecosystem.com/become-a-vendor",  "priority": "high"},
]


def seed_if_empty() -> int:
    """Populate the state file with starter entries on first run. Returns count added."""
    state = load_state()
    if state.affiliates:
        return 0
    n = 0
    for entry in SEED_AFFILIATES:
        create(**entry)
        n += 1
    return n

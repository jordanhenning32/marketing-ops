"""Explicit brand voice approval workflow.

The team may create config/brand-voice-draft.md, but only this module's
install command copies the approved draft over config/brand-voice.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from hermes_store import CONFIG_DIR, STATE_DIR, utc_now_iso, write_text

DRAFT_PATH = CONFIG_DIR / "brand-voice-draft.md"
FINAL_PATH = CONFIG_DIR / "brand-voice.md"
REVIEW_PATH = STATE_DIR / "brand-voice-review.json"
VALID_STATUSES = {"draft_missing", "draft_ready", "approved", "rejected", "needs_changes"}


def _source_count() -> int:
    if not DRAFT_PATH.exists():
        return 0
    text = DRAFT_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.lower().startswith("sources scanned:"):
            import re
            m = re.search(r"(\d+)", line)
            if m:
                return int(m.group(1))
    return 1


def _sha256_path(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draft_sha256() -> str:
    return _sha256_path(DRAFT_PATH)


def _load() -> dict[str, Any]:
    if REVIEW_PATH.exists():
        try:
            return json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _with_metadata(payload: dict[str, Any], *, touch: bool) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("draft_path", str(DRAFT_PATH))
    payload.setdefault("final_path", str(FINAL_PATH))
    payload["source_count"] = _source_count()
    if touch:
        payload["updated_at"] = utc_now_iso()
    else:
        payload.setdefault("updated_at", "")
    return payload


def _write(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _with_metadata(payload, touch=True)
    write_text(REVIEW_PATH, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return payload


def review_status(*, persist: bool = True) -> dict[str, Any]:
    payload = _load()
    if not DRAFT_PATH.exists():
        payload["status"] = "draft_missing"
    else:
        payload.setdefault("status", "draft_ready")
    payload.setdefault("reviewer", "")
    payload.setdefault("reviewed_at", "")
    payload.setdefault("notes", "")
    payload.setdefault("installed_at", "")
    final_text = FINAL_PATH.read_text(encoding="utf-8") if FINAL_PATH.exists() else ""
    final_hash = _sha256_path(FINAL_PATH)
    installed_hash = payload.get("installed_sha256") or payload.get("draft_sha256")
    if "placeholder" in final_text.lower() or (payload.get("installed") and installed_hash and final_hash != installed_hash):
        payload["installed"] = False
    else:
        payload.setdefault("installed", False)
    if persist:
        return _write(payload)
    return _with_metadata(payload, touch=False)


def mark_draft_ready(*, reviewer: str = "", notes: str = "") -> dict[str, Any]:
    if not DRAFT_PATH.exists():
        raise FileNotFoundError(DRAFT_PATH)
    payload = review_status()
    payload.update({"status": "draft_ready", "reviewer": reviewer, "notes": notes, "reviewed_at": ""})
    return _write(payload)


def mark_reviewed(status: str, *, reviewer: str, notes: str = "") -> dict[str, Any]:
    if status not in {"approved", "rejected", "needs_changes"}:
        raise ValueError("status must be approved, rejected, or needs_changes")
    if not reviewer:
        raise ValueError("reviewer is required")
    payload = review_status()
    if payload.get("status") == "draft_missing":
        raise FileNotFoundError(DRAFT_PATH)
    payload.update({"status": status, "reviewer": reviewer, "notes": notes, "reviewed_at": utc_now_iso()})
    if status == "approved":
        payload["draft_sha256"] = _draft_sha256()
    return _write(payload)


def install_approved_draft(*, reviewer: str = "") -> dict[str, Any]:
    payload = review_status()
    if payload.get("status") != "approved":
        raise PermissionError("brand voice draft must be explicitly approved before install")
    if not DRAFT_PATH.exists():
        raise FileNotFoundError(DRAFT_PATH)
    approved_hash = payload.get("draft_sha256")
    current_hash = _draft_sha256()
    if not approved_hash:
        raise PermissionError("brand voice approval is missing draft hash; review it again before install")
    if approved_hash != current_hash:
        raise PermissionError("brand voice draft changed after approval; review it again before install")
    FINAL_PATH.write_text(DRAFT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    payload.update({"installed": True, "installed_at": utc_now_iso(), "installed_by": reviewer or payload.get("reviewer", ""), "installed_sha256": _sha256_path(FINAL_PATH)})
    return _write(payload)


def write_approval_packet() -> dict[str, Any]:
    status = review_status()
    packet = {
        "generated_at": utc_now_iso(),
        "status": status.get("status"),
        "draft_path": str(DRAFT_PATH),
        "final_path": str(FINAL_PATH),
        "draft_sha256": _draft_sha256(),
        "final_sha256": _sha256_path(FINAL_PATH),
        "source_summary": f"Sources scanned: {status.get('source_count', _source_count())}",
        "what_changes_when_installed": "config/brand-voice.md will be replaced with the approved draft text. Future Hermes content will use that installed voice instead of the placeholder/fallback profile.",
        "rollback_instructions": "Rollback by restoring config/brand-voice.md from backup/version control or copying the previous text back into the file, then run production_check.",
        "approval_command": "python scripts/brand_voice_review.py review approved --reviewer Jordan --notes \"approved for production\"",
        "install_command": "python scripts/brand_voice_review.py install --reviewer Jordan",
        "install_guard": "Install requires status=approved and the current draft hash to match the approved draft_sha256.",
        "installed": bool(status.get("installed")),
    }
    packet_json = STATE_DIR / "brand-voice-approval-packet.json"
    packet_md = STATE_DIR / "brand-voice-approval-packet.md"
    write_text(packet_json, json.dumps(packet, indent=2, ensure_ascii=False) + "\n")
    lines = [
        "# Brand Voice Approval Packet",
        "",
        f"Status: {packet['status']}",
        f"Draft: `{packet['draft_path']}`",
        f"Install target: `{packet['final_path']}`",
        f"Draft SHA256: `{packet['draft_sha256']}`",
        f"Current installed SHA256: `{packet['final_sha256']}`",
        "",
        "## Source Summary",
        f"- {packet['source_summary']}",
        "",
        "## What Changes When Installed",
        packet["what_changes_when_installed"],
        "",
        "## Rollback",
        packet["rollback_instructions"],
        "",
        "## Approve/Install Commands",
        f"- Approve: `{packet['approval_command']}`",
        f"- Install: `{packet['install_command']}`",
        "",
        "Do not install unless Jordan explicitly approves.",
    ]
    write_text(packet_md, "\n".join(lines) + "\n")
    return packet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    ready = sub.add_parser("ready")
    ready.add_argument("--reviewer", default="")
    ready.add_argument("--notes", default="")
    reviewed = sub.add_parser("review")
    reviewed.add_argument("status", choices=["approved", "rejected", "needs_changes"])
    reviewed.add_argument("--reviewer", required=True)
    reviewed.add_argument("--notes", default="")
    packet = sub.add_parser("packet")
    install = sub.add_parser("install")
    install.add_argument("--reviewer", default="")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        payload = review_status()
    elif args.cmd == "ready":
        payload = mark_draft_ready(reviewer=args.reviewer, notes=args.notes)
    elif args.cmd == "review":
        payload = mark_reviewed(args.status, reviewer=args.reviewer, notes=args.notes)
    elif args.cmd == "packet":
        payload = write_approval_packet()
    else:
        payload = install_approved_draft(reviewer=args.reviewer)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

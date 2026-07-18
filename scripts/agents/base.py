"""Common deterministic agent interface and task board for Shadow Edge Hermes."""
from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from hermes_store import STATE_DIR, append_jsonl, read_jsonl, upsert_jsonl, utc_now_iso, write_jsonl

RUNS_PATH = STATE_DIR / "agent-runs.jsonl"
TASKS_PATH = STATE_DIR / "agent-tasks.jsonl"
VALID_TASK_STATUSES = {"open", "in_progress", "waiting", "blocked", "resolved", "wont_do"}


@dataclass
class AgentResult:
    name: str
    status: str = "ok"
    outputs: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketingAgent:
    name = "base"
    required_inputs: list[str] = []
    outputs: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "required_inputs": self.required_inputs, "outputs": self.outputs, "blockers": []}

    def run(self, context: dict[str, Any]) -> AgentResult:  # pragma: no cover
        raise NotImplementedError

    def safe_run(self, context: dict[str, Any]) -> AgentResult:
        started = utc_now_iso()
        try:
            result = self.run(context)
            if not isinstance(result, AgentResult):
                result = AgentResult(self.name, "ok", {"raw": result})
        except Exception as exc:  # noqa: BLE001
            result = AgentResult(self.name, "failed", {}, [f"{type(exc).__name__}: {exc}"], [task(self.name, "Fix failed agent run", str(exc), severity="high")])
            result.outputs["traceback"] = traceback.format_exc(limit=5)
        append_run(result, context, started)
        for item in result.tasks:
            upsert_task(item)
        return result


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(created_at: str | None) -> int:
    dt = _parse_iso(created_at)
    if not dt:
        return 0
    return max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)


def task(agent: str, title: str, reason: str, *, severity: str = "medium", status: str = "open", owner: str = "CODEX", priority: int = 3, next_action: str = "") -> dict[str, Any]:
    key = f"{agent}|{title}".lower().replace(" ", "-")
    now = utc_now_iso()
    return {
        "task_key": key,
        "agent": agent,
        "source_agent": agent,
        "title": title,
        "reason": reason,
        "severity": severity,
        "priority": priority,
        "status": status,
        "owner": owner,
        "next_action": next_action or reason,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "resolved_at": "" if status != "resolved" else now,
        "notes": [],
        "age_days": 0,
    }


def _normalize_task(row: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utc_now_iso()
    existing = existing or {}
    status = row.get("status") or existing.get("status") or "open"
    if status not in VALID_TASK_STATUSES:
        status = "open"
    created = existing.get("created_at") or row.get("created_at") or now
    notes = existing.get("notes") or []
    if row.get("note"):
        notes = [*notes, {"at": now, "note": row["note"]}]
    merged = {**existing, **row}
    merged.update({
        "agent": merged.get("agent") or merged.get("source_agent") or "unknown",
        "source_agent": merged.get("source_agent") or merged.get("agent") or "unknown",
        "title": merged.get("title") or merged.get("task_key") or "Untitled task",
        "reason": merged.get("reason") or "",
        "severity": merged.get("severity") or "medium",
        "priority": int(merged.get("priority") or 3),
        "owner": merged.get("owner") or "CODEX",
        "status": status,
        "next_action": merged.get("next_action") or merged.get("reason") or "review task",
        "created_at": created,
        "updated_at": now,
        "last_seen_at": now,
        "resolved_at": merged.get("resolved_at") or (now if status == "resolved" else ""),
        "notes": notes,
        "age_days": _age_days(created),
    })
    return merged


def upsert_task(row: dict[str, Any]) -> dict[str, Any]:
    rows = read_jsonl(TASKS_PATH)
    key = row.get("task_key") or f"{row.get('agent','unknown')}|{row.get('title','untitled')}".lower().replace(" ", "-")
    row = {**row, "task_key": key}
    for idx, existing in enumerate(rows):
        if existing.get("task_key") == key:
            normalized = _normalize_task(row, existing)
            rows[idx] = normalized
            write_jsonl(TASKS_PATH, rows)
            return normalized
    normalized = _normalize_task(row)
    rows.append(normalized)
    write_jsonl(TASKS_PATH, rows)
    return normalized


def append_task(row: dict[str, Any]) -> None:
    upsert_task(row)


def update_task_status(task_key: str, status: str, note: str = "") -> dict[str, Any]:
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"invalid task status: {status}")
    now = utc_now_iso()
    rows = read_jsonl(TASKS_PATH)
    for row in rows:
        if row.get("task_key") == task_key:
            row["status"] = status
            row["updated_at"] = now
            row["last_seen_at"] = now
            if status == "resolved":
                row["resolved_at"] = now
            elif status == "open":
                row["resolved_at"] = ""
            if note:
                row.setdefault("notes", []).append({"at": now, "note": note})
            return upsert_task(row)
    raise KeyError(task_key)


def annotate_task(task_key: str, note: str) -> dict[str, Any]:
    rows = read_jsonl(TASKS_PATH)
    for row in rows:
        if row.get("task_key") == task_key:
            row.setdefault("notes", []).append({"at": utc_now_iso(), "note": note})
            return upsert_task(row)
    raise KeyError(task_key)


def append_run(result: AgentResult, context: dict[str, Any], started_at: str | None = None) -> None:
    append_jsonl(RUNS_PATH, {"timestamp": utc_now_iso(), "started_at": started_at or utc_now_iso(), "agent": result.name, "status": result.status, "date": context.get("date"), "mode": "dry_run" if context.get("dry_run", True) else "live", "outputs": result.outputs, "blockers": result.blockers})


def latest_runs() -> list[dict[str, Any]]:
    return read_jsonl(RUNS_PATH)


def latest_tasks(status: str | None = None) -> list[dict[str, Any]]:
    rows = [_normalize_task(r) for r in read_jsonl(TASKS_PATH)]
    return [r for r in rows if not status or r.get("status") == status]

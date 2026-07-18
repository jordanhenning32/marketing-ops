"""Lightweight background-job system for the Ops Console.

Jobs run in dedicated threads. Each job writes its status to a JSON file in
`content/jobs/<id>.json` so the GUI can poll for progress without holding a
long HTTP connection open.

Public surface:
    job = create_job(kind="video", source_filename=name, meta={...})
    run_job(job_id, target=callable, args=(...,))     # spawns thread
    get_job(job_id)                                   # current state
    list_jobs()                                       # all jobs newest first
    update_job(job_id, **patches)                     # called from worker
    log_job(job_id, line)                             # human progress line

Workers should call `update_job(...)` and `log_job(...)` to advance state.
The console's job view page polls /content/job/<id> for the latest JSON.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "content" / "jobs"

_JOB_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_TERMINAL_STATES = {"done", "failed"}


def _lock_for(job_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if job_id not in _JOB_LOCKS:
            _JOB_LOCKS[job_id] = threading.Lock()
        return _JOB_LOCKS[job_id]


def _discard_lock(job_id: str, lock: threading.Lock | None = None) -> None:
    with _LOCKS_GUARD:
        if lock is None or _JOB_LOCKS.get(job_id) is lock:
            _JOB_LOCKS.pop(job_id, None)


@dataclass
class Job:
    id: str
    kind: str                       # "video" | "transcript"
    state: str                      # "queued" | "running" | "done" | "failed"
    current_step: str               # human-readable
    progress_pct: int               # 0-100
    started_at: str
    finished_at: str | None
    error: str | None
    source_filename: str
    result_slug: str | None         # output folder name when done
    meta: dict[str, Any]            # arbitrary extra data
    log: list[str] = field(default_factory=list)

    def to_path(self) -> Path:
        return JOBS_DIR / f"{self.id}.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save(job: Job) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with _lock_for(job.id):
        job.to_path().write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")


def _load(job_id: str) -> Job | None:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    with _lock_for(job_id):
        try:
            return Job(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None


def create_job(*, kind: str, source_filename: str, meta: dict | None = None) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        state="queued",
        current_step="Queued",
        progress_pct=0,
        started_at=_now(),
        finished_at=None,
        error=None,
        source_filename=source_filename,
        result_slug=None,
        meta=meta or {},
        log=[f"[{_now()}] Job created"],
    )
    _save(job)
    return job


def get_job(job_id: str) -> Job | None:
    return _load(job_id)


def list_jobs(limit: int = 50) -> list[Job]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[Job] = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(Job(**json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def update_job(job_id: str, **patches) -> Job | None:
    job = _load(job_id)
    if not job:
        return None
    for k, v in patches.items():
        if hasattr(job, k):
            setattr(job, k, v)
    _save(job)
    if job.state in _TERMINAL_STATES:
        _discard_lock(job_id)
    return job


def log_job(job_id: str, line: str) -> None:
    job = _load(job_id)
    if not job:
        return
    job.log.append(f"[{_now()}] {line}")
    # cap log length so the JSON file doesn't bloat
    job.log = job.log[-200:]
    _save(job)
    if job.state in _TERMINAL_STATES:
        _discard_lock(job_id)


def _runner(job_id: str, target: Callable, args: tuple, kwargs: dict) -> None:
    update_job(job_id, state="running")
    log_job(job_id, "Worker started")
    try:
        target(job_id, *args, **kwargs)
        # The target is expected to call update_job(state='done') itself when
        # it finishes, since it's the one that knows the result_slug. We just
        # double-check here.
        job = get_job(job_id)
        if job and job.state == "running":
            update_job(job_id, state="done", finished_at=_now(), progress_pct=100)
            log_job(job_id, "Done")
    except Exception as exc:  # noqa: BLE001
        log_job(job_id, f"FAILED: {exc}")
        log_job(job_id, traceback.format_exc())
        update_job(
            job_id,
            state="failed",
            error=str(exc),
            finished_at=_now(),
        )


def run_job(job_id: str, target: Callable, *, args: tuple = (), kwargs: dict | None = None) -> None:
    """Spawn a background thread to run `target(job_id, *args, **kwargs)`.

    The target should advance progress via update_job/log_job and finish by
    calling update_job(job_id, state='done', result_slug=..., progress_pct=100).
    """
    t = threading.Thread(
        target=_runner,
        args=(job_id, target, args, kwargs or {}),
        daemon=True,
        name=f"job-{job_id}",
    )
    t.start()


def cleanup_old_jobs(keep_days: int = 30) -> int:
    """Delete job JSONs older than N days. Returns count deleted."""
    cutoff = time.time() - keep_days * 86400
    n = 0
    for p in JOBS_DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                _discard_lock(p.stem)
                n += 1
            except OSError:
                pass
    return n


def delete_job(job_id: str) -> bool:
    """Delete a single job's status file. Returns True if a file was removed."""
    lock = _lock_for(job_id)
    with lock:
        try:
            (JOBS_DIR / f"{job_id}.json").unlink()
            removed = True
        except (FileNotFoundError, OSError):
            removed = False
    _discard_lock(job_id, lock)
    return removed


def delete_jobs_by_state(state: str) -> int:
    """Delete every job currently in the given state. Returns count removed."""
    return sum(1 for job in list_jobs(limit=1000) if job.state == state and delete_job(job.id))


def reconcile_stale_jobs() -> int:
    """Mark jobs still 'running'/'queued' (orphaned by a console restart) as failed.

    Worker threads don't survive a process restart, so any job left in a live
    state at startup is dead. This stops them spinning forever and makes them
    dismissable. Returns the number reconciled.
    """
    n = 0
    for job in list_jobs(limit=1000):
        if job.state in ("running", "queued"):
            update_job(
                job.id,
                state="failed",
                error="Interrupted — the console restarted while this job was running.",
                current_step="Interrupted",
                finished_at=_now(),
            )
            n += 1
    return n

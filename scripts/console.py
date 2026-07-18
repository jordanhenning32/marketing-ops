"""Shadow Edge Operations Console — FastAPI app.

A single-process web console that:
- Renders today's marketing dashboard
- Lets Jordan view + edit state files in-browser
- Triggers agent runs (ops tracker for v1; scout & content multiplier later)

Run:
    python scripts/console.py            # boots on http://localhost:8876/autopilot

The launcher .bat opens the browser automatically.
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

# Load the repo env plus the parent Shadow Edge env. Later files only fill
# missing values, so marketing-ops/.env can still override shared settings.
try:
    from dotenv import load_dotenv
    _env_root = Path(__file__).resolve().parent.parent
    for candidate in (
        _env_root / ".env",
        _env_root.parent / ".ENV",
        _env_root.parent / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Ensure scripts/ is importable when run directly
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import affiliates as affiliates_mod  # noqa: E402
import content_multiplier  # noqa: E402
import jobs as jobs_mod  # noqa: E402
import ops_tracker  # noqa: E402
try:
    import scout  # noqa: E402
except Exception as exc:  # noqa: BLE001
    class _ScoutUnavailable:
        def __init__(self, error):
            self.error = str(error)
        def research_affiliate_url(self, *_args, **_kwargs):
            return {"ok": False, "error": self.error}
        def discover_affiliate_candidates(self, *_args, **_kwargs):
            return {"ok": False, "error": self.error, "candidates": []}
        def save_discovery(self, *_args, **_kwargs):
            return None
        def load_discovery(self):
            return {"candidates": [], "error": self.error}
        def reddit_credentials_status(self):
            return {"ok": False, "missing": ["bs4/beautifulsoup4 or scout dependencies"], "error": self.error}
    scout = _ScoutUnavailable(exc)  # type: ignore[assignment]
try:
    import video_pipeline  # noqa: E402
except Exception as exc:  # noqa: BLE001
    class _VideoPipelineUnavailable:
        def __init__(self, error):
            self.error = str(error)
        def process_video_job(self, *_args, **_kwargs):
            raise RuntimeError(f"video pipeline unavailable: {self.error}")
    video_pipeline = _VideoPipelineUnavailable(exc)  # type: ignore[assignment]
try:
    import youtube_auth  # noqa: E402
except Exception as exc:  # noqa: BLE001
    class _YouTubeAuthUnavailable:
        CLIENT_SECRET_PATH = _ROOT / "config" / "youtube-client-secret.json"
        def __init__(self, error):
            self.error = str(error)
        def get_status(self):
            return {"connected": False, "error": self.error}
        def has_client_secret(self):
            return False
        def start_auth(self, *_args, **_kwargs):
            raise RuntimeError(self.error)
        def finish_auth(self, *_args, **_kwargs):
            raise RuntimeError(self.error)
        def is_connected(self):
            return False
        def disconnect(self):
            return None
    youtube_auth = _YouTubeAuthUnavailable(exc)  # type: ignore[assignment]
try:
    import youtube_upload  # noqa: E402
except Exception as exc:  # noqa: BLE001
    class _YouTubeUploadUnavailable:
        def __init__(self, error):
            self.error = str(error)
        def upload_clip_job(self, *_args, **_kwargs):
            raise RuntimeError(f"youtube upload unavailable: {self.error}")
    youtube_upload = _YouTubeUploadUnavailable(exc)  # type: ignore[assignment]

ROOT = _ROOT
STATE_DIR = ROOT / "state"
DAILY_OPS_DIR = ROOT / "daily-ops"
BRIEFINGS_DIR = ROOT / "briefings"
CONTENT_DIR = ROOT / "content"
INCOMING_DIR = CONTENT_DIR / "incoming"
INCOMING_VIDEOS_DIR = CONTENT_DIR / "incoming-videos"
CLIPS_BASE_DIR = CONTENT_DIR
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
FAVICON_PATH = STATIC_DIR / "shadow-edge.ico"
ALLOWED_TRANSCRIPT_EXTS = {".txt", ".srt", ".docx"}
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
CONSOLE_ID = "shadow-edge-marketing-ops"
DEFAULT_CONSOLE_PORT = 8876
DEFAULT_HOME_PATH = "/today"
_listening_scan_lock = threading.Lock()
# Live state for the background listening scan, surfaced via /listening/scan/status
# so the UI can show a progress bar + elapsed timer instead of "refresh in a moment".
_scan_state: dict = {
    "running": False, "started_epoch": None, "finished_epoch": None,
    "summary": None, "error": None, "source": None,
}


def _trigger_listening_scan(only: str | None = None, draft: bool = True) -> bool:
    """Start a listening scan in a background thread. Returns False if one is
    already running. Shared by /listening/scan and /today/scan."""
    import listening
    if not _listening_scan_lock.acquire(blocking=False):
        return False

    _scan_state.update({
        "running": True, "started_epoch": time.time(), "finished_epoch": None,
        "summary": None, "error": None, "source": only or "all",
    })

    def _run():
        try:
            _scan_state["summary"] = listening.run_scan(only=only, draft=draft)
        except Exception as exc:  # noqa: BLE001 - background best-effort; surface in the log + status
            _scan_state["error"] = str(exc)
            listening.append_jsonl(listening.ENGAGEMENT_LOG_PATH, {
                "event": "scan_error",
                "source": only or "all",
                "draft": draft,
                "error": str(exc),
                "at": listening.utc_now_iso(),
            })
        finally:
            _scan_state["finished_epoch"] = time.time()
            _scan_state["running"] = False
            _listening_scan_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def _video_upload_blockers(provider: str) -> list[str]:
    """Return human-actionable blockers before accepting a large video upload."""
    blockers: list[str] = []
    pipeline_error = getattr(video_pipeline, "error", None)
    if pipeline_error:
        blockers.append(
            f"Video pipeline unavailable: {pipeline_error}. Restart with start-console.bat so .venv dependencies are loaded."
        )

    if provider == "local":
        if importlib.util.find_spec("faster_whisper") is None:
            blockers.append("Local Whisper is not installed. Restart with start-console.bat to install requirements into .venv.")
    elif provider == "assemblyai":
        if not os.environ.get("ASSEMBLYAI_API_KEY"):
            blockers.append("ASSEMBLYAI_API_KEY missing. Add it to marketing-ops/.env or the parent .ENV, or use Local Whisper.")
    else:
        blockers.append(f"Unknown transcribe provider: {provider}")

    if importlib.util.find_spec("anthropic") is None:
        blockers.append("Anthropic SDK is not installed. Restart with start-console.bat to install requirements into .venv.")
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        blockers.append("ANTHROPIC_API_KEY missing. Add it to marketing-ops/.env or the parent .ENV before running the full video pipeline.")

    return blockers

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _watcher_thread
    if not os.environ.get("CONSOLE_DISABLE_WATCHER"):
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(target=_watcher_loop, daemon=True, name="content-watcher")
        _watcher_thread.start()
    try:
        yield
    finally:
        _watcher_stop.set()


app = FastAPI(title="Shadow Edge Operations Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# cache_size=0 works around a Python 3.14 + Jinja2 cache-key incompatibility
# (TypeError: cannot use 'tuple' as a dict key) when starlette passes globals.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.cache = None
templates.env.auto_reload = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EDITABLE_STATE_FILES = [
    "goal.md",
    "nt-ecosystem.md",
    "partnerships.md",
    "influencers.md",
    "affiliates.md",
    "content-calendar.md",
]

EDITABLE_CONFIG_FILES = [
    "config/brand-voice-draft.md",
    "config/guardrails.md",
    "config/monitored.yaml",
]


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _write_text(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _safe_resolve(rel_path: str, allowed: list[str]) -> Path | None:
    """Resolve a relative path against ROOT, but only if it's in the allow list."""
    if rel_path not in allowed:
        return None
    p = (ROOT / rel_path).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        return None
    return p


def _content_run_folder(slug: str) -> Path | None:
    """Return content/<slug> only for a single safe path segment."""
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        return None
    base = CONTENT_DIR.resolve()
    folder = (CONTENT_DIR / slug).resolve()
    try:
        folder.relative_to(base)
    except ValueError:
        return None
    return folder


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    return RedirectResponse(url=DEFAULT_HOME_PATH, status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(FAVICON_PATH, media_type="image/x-icon")


@app.get("/console-id")
def console_id():
    return JSONResponse(
        {
            "app": CONSOLE_ID,
            "name": "Shadow Edge Marketing Ops Console",
            "home_path": DEFAULT_HOME_PATH,
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": CONSOLE_ID,
        "home_path": DEFAULT_HOME_PATH,
    }


def _outreach_summary():
    """Live listening/engagement counts for the dashboard. None if unavailable."""
    try:
        import listening
        queue = listening.queue_items()
        counts: dict[str, int] = {}
        for item in queue:
            counts[item.get("status", "drafted")] = counts.get(item.get("status", "drafted"), 0) + 1
        drafts_pending = counts.get("drafted", 0)
        approved_pending = counts.get("approved", 0)
        return {
            "drafts_pending": drafts_pending,
            "approved_pending": approved_pending,
            "work_pending": drafts_pending + approved_pending,
            "posted": counts.get("posted", 0),
            "opportunities": len(listening.list_opportunities()),
            "last_scan": listening.last_scan_summary(),
        }
    except Exception:  # noqa: BLE001 - dashboard must render even if listening is down
        return None


@app.post("/run/ops")
def run_ops():
    ops_tracker.run()
    return RedirectResponse(url="/", status_code=303)


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline_view(request: Request):
    state = ops_tracker.load_state()
    return templates.TemplateResponse(
        request,
        "pipeline.html",
        {
            "active": "pipeline",
            "partnerships": state.partnerships,
            "influencers": state.influencers,
            "today": date.today().isoformat(),
        },
    )


@app.get("/state", response_class=HTMLResponse)
def state_index(request: Request, file: str | None = None):
    target_rel = file or "goal.md"
    files = [{"rel": f, "label": f} for f in EDITABLE_STATE_FILES]
    files += [{"rel": f, "label": f} for f in EDITABLE_CONFIG_FILES]

    allowed = EDITABLE_STATE_FILES + EDITABLE_CONFIG_FILES
    if target_rel in EDITABLE_STATE_FILES:
        path = STATE_DIR / target_rel
    else:
        path = ROOT / target_rel
    if target_rel not in allowed:
        path = STATE_DIR / "goal.md"
        target_rel = "goal.md"

    return templates.TemplateResponse(
        request,
        "state.html",
        {
            "active": "state",
            "files": files,
            "current": target_rel,
            "content": _read_text(path),
        },
    )


@app.post("/state/save")
def state_save(rel: str = Form(...), content: str = Form(...)):
    allowed = EDITABLE_STATE_FILES + EDITABLE_CONFIG_FILES
    if rel not in allowed:
        return RedirectResponse(url="/state", status_code=303)
    if rel in EDITABLE_STATE_FILES:
        path = STATE_DIR / rel
    else:
        path = ROOT / rel
    _write_text(path, content)
    # Also re-run ops tracker so the dashboard reflects the edit
    ops_tracker.run()
    return RedirectResponse(url=f"/state?file={rel}", status_code=303)


@app.get("/today", response_class=HTMLResponse)
def today_view(request: Request, d: str | None = None, msg: str = "", err: str = ""):
    """Render the daily-ops brief + interactive control panel for a given date."""
    target_date = d or date.today().isoformat()
    path = DAILY_OPS_DIR / f"{target_date}.md"
    md = _read_text(path)
    # collect all available dates for the picker
    available = sorted(
        [p.stem for p in DAILY_OPS_DIR.glob("*.md")],
        reverse=True,
    )
    is_today = target_date == date.today().isoformat()
    try:
        partner = ops_tracker.pick_todays_partner(ops_tracker.compute_dashboard())
    except Exception:  # noqa: BLE001 - the panel must render even if state is malformed
        partner = None
    try:
        import video_script
        script_md = video_script.latest_script()
    except Exception:  # noqa: BLE001
        script_md = ""
    try:
        import daily_posts
        social_posts = daily_posts.load_posts()
        post_streak = daily_posts.posting_streak()
    except Exception:  # noqa: BLE001 - the panel must render even if generation is down
        social_posts = []
        post_streak = 0
    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "active": "today",
            "date": target_date,
            "md": md,
            "exists": path.exists(),
            "available": available,
            "is_today": is_today,
            "partner": partner,
            "outreach": _outreach_summary(),
            "script_md": script_md,
            "social_posts": social_posts,
            "post_streak": post_streak,
            "posts_pending": sum(1 for p in social_posts if p.get("status") == "pending"),
            "msg": msg,
            "err": err,
        },
    )


@app.post("/today/generate")
def today_generate():
    from urllib.parse import quote_plus
    ops_tracker.run()
    return RedirectResponse("/today?msg=" + quote_plus("Brief regenerated from current state."), status_code=303)


@app.post("/today/scan")
def today_scan():
    from urllib.parse import quote_plus
    if _trigger_listening_scan(only=None, draft=True):
        return RedirectResponse("/today?msg=" + quote_plus("Listening scan started — drafted replies will land in Engagement for review."), status_code=303)
    return RedirectResponse("/today?msg=" + quote_plus("A scan is already running — check Engagement shortly."), status_code=303)


@app.post("/today/video-script")
def today_video_script():
    from urllib.parse import quote_plus
    try:
        import video_script
        res = video_script.generate_next_video_script()
        return RedirectResponse("/today?msg=" + quote_plus(f"Next-video script drafted ({res['provider']}/{res['model']}) — see below."), status_code=303)
    except Exception as exc:  # noqa: BLE001 - surface the reason to the operator
        return RedirectResponse("/today?err=" + quote_plus(f"Script generation failed: {exc}"), status_code=303)


@app.post("/today/partner-done")
def today_partner_done(name: str = Form(...)):
    from urllib.parse import quote_plus
    try:
        found = ops_tracker.mark_partner_contacted(name)
        ops_tracker.run()
        note = f"Marked {name} emailed — next follow-up scheduled in 7 days." if found else f"Could not find partner '{name}'."
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/today?err=" + quote_plus(str(exc)), status_code=303)
    return RedirectResponse("/today?msg=" + quote_plus(note), status_code=303)


@app.post("/today/generate-posts")
def today_generate_posts(only: str = Form(""), post_guidance: str = Form("")):
    from urllib.parse import quote_plus
    try:
        import daily_posts
        post_guidance = (post_guidance or "").strip()[:2000]
        res = daily_posts.generate_daily_posts(only=only or None, post_guidance=post_guidance)
        if res.get("generated"):
            note = f"Generated {res['generated']} post(s) for today (pillar: {res['pillar']})."
            if post_guidance:
                note += " Direction applied."
        else:
            note = "Nothing to generate — today's posts already exist."
        if res.get("errors"):
            note += " Some failed: " + "; ".join(res["errors"])
    except Exception as exc:  # noqa: BLE001 - surface the reason to the operator
        return RedirectResponse("/today?err=" + quote_plus(f"Post generation failed: {exc}"), status_code=303)
    return RedirectResponse("/today?msg=" + quote_plus(note), status_code=303)


@app.post("/today/post-done")
def today_post_done(platform: str = Form(...), action: str = Form("posted"), posted_url: str = Form("")):
    from urllib.parse import quote_plus
    try:
        import daily_posts
        ok = daily_posts.set_post_status(platform, action, posted_url=posted_url.strip())
        note = f"{platform.capitalize()}: {action}." if ok else f"No {platform} post found for today."
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/today?err=" + quote_plus(str(exc)), status_code=303)
    return RedirectResponse("/today?msg=" + quote_plus(note), status_code=303)


@app.get("/docs/{doc_name}", response_class=HTMLResponse)
def docs_page(request: Request, doc_name: str):
    allowed = {
        "final-operator-handoff": ROOT / "docs" / "final-operator-handoff.md",
        "activation-checklist": ROOT / "docs" / "activation-checklist.md",
        "public-lead-capture": ROOT / "docs" / "public-lead-capture.md",
        "tracking-contract": ROOT / "docs" / "tracking-contract.md",
        "canary-rollback": ROOT / "docs" / "canary-rollback.md",
        "full-automation-graduation": ROOT / "docs" / "full-automation-graduation.md",
    }
    path = allowed.get(doc_name)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="doc not found")
    html = f"<html><body><main><div class='render-md'>{path.read_text(encoding='utf-8')}</div><script src='https://cdn.jsdelivr.net/npm/marked/marked.min.js'></script><script>document.querySelectorAll('.render-md').forEach((el)=>{{el.innerHTML=marked.parse(el.textContent);}});</script></main></body></html>"
    return HTMLResponse(html)


@app.get("/affiliates", response_class=HTMLResponse)
def affiliates_index(request: Request, show_archived: int = 0):
    state = affiliates_mod.load_state()
    summary = affiliates_mod.summary()
    items = state.affiliates if show_archived else [a for a in state.affiliates if not a.archived]
    # Sort: by priority desc, then status (not_started first), then name
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    status_rank = {
        "approved": 0, "active": 1, "applied": 2, "researching": 3,
        "not_started": 4, "declined": 5, "archived": 6,
    }
    items_sorted = sorted(
        items,
        key=lambda a: (priority_rank.get(a.priority, 99), status_rank.get(a.status, 99), a.name.lower()),
    )
    return templates.TemplateResponse(
        request,
        "affiliates.html",
        {
            "active": "affiliates",
            "items": items_sorted,
            "summary": summary,
            "show_archived": bool(show_archived),
            "valid_statuses": affiliates_mod.VALID_STATUSES,
            "valid_priorities": affiliates_mod.VALID_PRIORITIES,
        },
    )


@app.post("/affiliates/add")
def affiliates_add(
    name: str = Form(...),
    url: str = Form(""),
    commission: str = Form(""),
    cookie_days: str = Form(""),
    contact: str = Form(""),
    priority: str = Form("medium"),
    notes: str = Form(""),
):
    if not name.strip():
        return RedirectResponse(url="/affiliates", status_code=303)
    cd = None
    try:
        cd = int(cookie_days) if cookie_days.strip() else None
    except ValueError:
        cd = None
    aff = affiliates_mod.create(
        name=name,
        url=url,
        commission=commission,
        cookie_days=cd,
        contact=contact,
        priority=priority,
        notes=notes,
    )
    return RedirectResponse(url=f"/affiliates#a-{aff.id}", status_code=303)


@app.post("/affiliates/{aff_id}/check/{item_id}")
def affiliates_toggle_check(aff_id: str, item_id: str):
    aff = affiliates_mod.toggle_check(aff_id, item_id)
    if not aff:
        return JSONResponse({"error": "not found"}, status_code=404)
    item = next((c for c in aff.checklist if c.id == item_id), None)
    return {
        "ok": True,
        "done": bool(item and item.done),
        "done_at": item.done_at if item else None,
        "status": aff.status,
        "progress_done": aff.progress()[0],
        "progress_total": aff.progress()[1],
        "progress_pct": aff.progress_pct(),
    }


@app.post("/affiliates/{aff_id}/update")
def affiliates_update(
    aff_id: str,
    name: str = Form(""),
    url: str = Form(""),
    commission: str = Form(""),
    cookie_days: str = Form(""),
    payout_method: str = Form(""),
    contact: str = Form(""),
    priority: str = Form(""),
    status: str = Form(""),
    notes: str = Form(""),
):
    patches: dict = {}
    if name.strip():
        patches["name"] = name.strip()
    if url:
        patches["url"] = url.strip()
    if commission:
        patches["commission"] = commission.strip()
    if cookie_days:
        patches["cookie_days"] = cookie_days
    if payout_method:
        patches["payout_method"] = payout_method.strip()
    if contact:
        patches["contact"] = contact.strip()
    if priority:
        patches["priority"] = priority
    if status:
        patches["status"] = status
    # Notes can be empty (clearing)
    patches["notes"] = notes
    aff = affiliates_mod.update(aff_id, **patches)
    if not aff:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url=f"/affiliates#a-{aff_id}", status_code=303)


@app.post("/affiliates/{aff_id}/checklist/add")
def affiliates_add_checklist(aff_id: str, label: str = Form(...)):
    aff = affiliates_mod.add_checklist_item(aff_id, label)
    if not aff:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url=f"/affiliates#a-{aff_id}", status_code=303)


@app.post("/affiliates/{aff_id}/checklist/{item_id}/remove")
def affiliates_remove_checklist(aff_id: str, item_id: str):
    aff = affiliates_mod.remove_checklist_item(aff_id, item_id)
    if not aff:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url=f"/affiliates#a-{aff_id}", status_code=303)


@app.post("/affiliates/{aff_id}/archive")
def affiliates_archive(aff_id: str):
    affiliates_mod.archive(aff_id)
    return RedirectResponse(url="/affiliates", status_code=303)


@app.post("/affiliates/{aff_id}/unarchive")
def affiliates_unarchive(aff_id: str):
    affiliates_mod.unarchive(aff_id)
    return RedirectResponse(url="/affiliates?show_archived=1", status_code=303)


@app.post("/affiliates/{aff_id}/delete")
def affiliates_delete(aff_id: str, confirm: str = Form("")):
    if confirm != "DELETE":
        return RedirectResponse(url="/affiliates", status_code=303)
    affiliates_mod.delete(aff_id)
    return RedirectResponse(url="/affiliates", status_code=303)


# --- Scout-driven discovery & research ---

@app.post("/affiliates/research-url")
def affiliates_research_url(url: str = Form(...)):
    """Fetch a URL, extract program details with Claude, return JSON.

    The UI prefills the Add-affiliate form with the result.
    """
    if not url.strip():
        return JSONResponse({"error": "URL is required."}, status_code=400)
    try:
        result = scout.research_affiliate_url(url.strip())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {
        "ok": result.error is None,
        "error": result.error,
        "name": result.name,
        "url": result.signup_url or result.url,
        "commission": result.commission,
        "cookie_days": result.cookie_days,
        "payout_method": result.payout_method,
        "minimum_payout": result.minimum_payout,
        "notes": result.notes,
        "extracted_confidence": result.extracted_confidence,
        "raw_excerpt": result.raw_excerpt,
    }


def _scout_discover_job(job_id: str, timeframe: str = "year") -> None:
    """Job target for Reddit-based discovery. Caches the result so the page
    can show last-run results without re-running the scan.
    """
    jobs_mod.update_job(job_id, current_step="Searching Reddit", progress_pct=10)
    jobs_mod.log_job(job_id, f"Starting discovery (timeframe={timeframe})")
    result = scout.discover_affiliate_candidates(timeframe=timeframe)
    jobs_mod.update_job(job_id, current_step="Caching results", progress_pct=90)
    scout.save_discovery(result)
    if result.error:
        jobs_mod.log_job(job_id, f"WARN: {result.error}")
    jobs_mod.log_job(job_id, f"Saw {result.posts_seen} posts, {len(result.candidates)} candidates")
    jobs_mod.update_job(
        job_id,
        state="done",
        current_step="Done",
        progress_pct=100,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        result_slug="discovery",
    )


@app.post("/affiliates/discover-reddit")
def affiliates_discover_reddit(timeframe: str = Form("year")):
    job = jobs_mod.create_job(
        kind="affiliate-discover",
        source_filename=f"reddit-scan-{timeframe}",
        meta={"timeframe": timeframe},
    )
    jobs_mod.run_job(
        job.id,
        _scout_discover_job,
        kwargs={"timeframe": timeframe},
    )
    return JSONResponse({
        "ok": True,
        "job_id": job.id,
        "redirect": f"/content/job/{job.id}/view",
    })


@app.get("/affiliates/discover", response_class=HTMLResponse)
def affiliates_discover_page(request: Request):
    cached = scout.load_discovery()
    state = affiliates_mod.load_state()
    known_lower = {a.name.strip().lower() for a in state.affiliates if not a.archived}
    candidates = []
    if cached:
        for c in cached.get("candidates", []):
            c = dict(c)
            c["already_tracked"] = c.get("name", "").strip().lower() in known_lower
            candidates.append(c)
    reddit_creds = scout.reddit_credentials_status()
    return templates.TemplateResponse(
        request,
        "affiliates_discover.html",
        {
            "active": "affiliates",
            "cached": cached,
            "candidates": candidates,
            "reddit_creds": reddit_creds,
        },
    )


@app.post("/affiliates/discover/import")
def affiliates_discover_import(
    name: str = Form(...),
    affiliate_url_guess: str = Form(""),
    why_relevant: str = Form(""),
    category: str = Form(""),
):
    """One-click create a tracker entry from a discovery candidate."""
    if not name.strip():
        return RedirectResponse(url="/affiliates/discover", status_code=303)
    notes = ""
    if category:
        notes += f"[{category}] "
    if why_relevant:
        notes += why_relevant.strip()
    aff = affiliates_mod.create(
        name=name.strip(),
        url=affiliate_url_guess.strip(),
        priority="medium",
        notes=notes,
    )
    return RedirectResponse(url=f"/affiliates#a-{aff.id}", status_code=303)


# ---------------------------------------------------------------------------
# Content Multiplier routes
# ---------------------------------------------------------------------------

def _run_distribution(folder) -> dict:
    """Aggregate a run's distribution completeness across the 5 target channels,
    summed over every clip. YouTube + TikTok come from upload metadata; Instagram,
    X, LinkedIn from the manual marks in 00-distribution-status.json. This is the
    same scorecard the run detail page shows, rolled up to the run level."""
    auto = ("youtube", "tiktok")
    channels = auto + ("instagram", "x", "linkedin")
    indices = []
    mani_path = folder / "00-pipeline-manifest.json"
    if mani_path.exists():
        try:
            mani = json.loads(mani_path.read_text(encoding="utf-8"))
            indices = [int(c["index"]) for c in mani.get("clips", []) if "index" in c]
        except (json.JSONDecodeError, ValueError, TypeError):
            indices = []
    if not indices:
        yt_dir = folder / "distribution" / "youtube"
        if yt_dir.is_dir():
            indices = [int(d.name) for d in yt_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    indices = sorted(set(indices))
    if not indices:
        # No clips to distribute -> not a video run for scorecard purposes.
        return {"done": 0, "total": 0, "fully": False, "is_video": False}
    status = {}
    sp = folder / "00-distribution-status.json"
    if sp.exists():
        try:
            status = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {}
    done = 0
    for idx in indices:
        marks = status.get(str(idx), {})
        for ch in channels:
            if ch in auto:
                mp = folder / "distribution" / ch / f"{idx:02d}" / "metadata.json"
                ok = False
                if mp.exists():
                    try:
                        pm = json.loads(mp.read_text(encoding="utf-8"))
                        ok = bool(pm.get("uploaded_at") or pm.get("platform_url"))
                    except json.JSONDecodeError:
                        ok = False
                done += 1 if ok else 0
            else:
                done += 1 if marks.get(ch) else 0
    total = len(indices) * len(channels)
    return {"done": done, "total": total, "fully": total > 0 and done >= total, "is_video": True}


def _run_attribution(campaign_id: str) -> dict:
    """Clicks + leads attributed to a campaign (= a content run), joining the
    existing lead-capture + manual-metric data by campaign_id."""
    result = {"leads": 0, "clicks": 0}
    if not campaign_id:
        return result
    try:
        import leads as leads_mod
        result["leads"] = sum(
            1 for l in leads_mod.list_leads()
            if any((t or {}).get("campaign_id") == campaign_id
                   for t in (l.get("latest_touch"), l.get("first_touch")))
        )
    except Exception:
        pass
    try:
        from hermes_store import read_jsonl
        from analytics import PERFORMANCE_PATH
        clicks = 0
        for row in read_jsonl(PERFORMANCE_PATH):
            if row.get("campaign_id") == campaign_id and row.get("metric") == "landing_page_clicks":
                try:
                    clicks += int(float(row.get("value") or 0))
                except (ValueError, TypeError):
                    pass
        result["clicks"] = clicks
    except Exception:
        pass
    return result


def _content_runs() -> list[dict]:
    """List all content multiplier runs (folders) sorted newest first."""
    if not CONTENT_DIR.exists():
        return []
    runs = []
    for entry in CONTENT_DIR.iterdir():
        if not entry.is_dir() or entry.name == "incoming":
            continue
        # Only real multiplier runs belong here — skip content-support folders that
        # also live under content/ (lead-magnets/, email-sequences/, jobs/,
        # incoming-videos/). A run always has a meta file, a pipeline manifest, a
        # distribution/ dir, or numbered draft files (01-..., 02-...).
        is_run = (
            (entry / "00-meta.json").exists()
            or (entry / "00-pipeline-manifest.json").exists()
            or (entry / "distribution").is_dir()
            or any(
                f.is_file() and f.suffix == ".md" and f.name[:1].isdigit()
                for f in entry.iterdir()
            )
        )
        if not is_run:
            continue
        meta_path = entry / "00-meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        files = sorted(
            f.name for f in entry.iterdir()
            if f.is_file() and f.suffix == ".md" and not f.name.startswith("00-")
        )
        # Full distribution roll-up across all 5 channels x clips (the same
        # scorecard the detail page shows) -> the badge means "fully out".
        dist = _run_distribution(entry)
        runs.append({
            "slug": entry.name,
            "files": files,
            "file_count": len(files),
            "meta": meta,
            "mtime": entry.stat().st_mtime,
            "is_video_run": dist["is_video"],
            "published": dist["fully"],
            "dist_done": dist["done"],
            "dist_total": dist["total"],
        })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _lead_assets(subdir: str) -> list[dict]:
    """Readable lead-gen assets (markdown/text) under content/<subdir>/ — the
    lead magnets and nurture email sequences that feed the Leads pipeline."""
    d = CONTENT_DIR / subdir
    out = []
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in (".md", ".txt"):
                try:
                    content = f.read_text(encoding="utf-8")
                except OSError:
                    content = ""
                out.append({"name": f.name, "content": content, "size": f.stat().st_size})
    return out


@app.get("/content", response_class=HTMLResponse)
def content_index(request: Request):
    runs = _content_runs()
    incoming_files = []
    if INCOMING_DIR.exists():
        for f in sorted(INCOMING_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_TRANSCRIPT_EXTS:
                incoming_files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                })
    all_jobs = jobs_mod.list_jobs(limit=200)
    # Runs that already have a successful upload -> don't nag to publish them.
    published_slugs = {
        j.result_slug or (j.meta or {}).get("slug")
        for j in all_jobs
        if j.kind == "youtube-upload" and j.state == "done"
    }
    published_slugs.discard(None)
    # Worklist order: in-progress first, then failures, then a few recent
    # completions -- old done jobs are history, not activity.
    _active = [j for j in all_jobs if j.state in ("running", "queued")]
    _failed = [j for j in all_jobs if j.state == "failed"]
    _done = [j for j in all_jobs if j.state == "done"]
    _ordered = _active + _failed + _done[:4]
    recent_jobs = [
        {
            "id": j.id,
            "kind": j.kind,
            "state": j.state,
            "current_step": j.current_step,
            "progress_pct": j.progress_pct,
            "source_filename": j.source_filename,
            "started_at": j.started_at,
            "result_slug": j.result_slug,
            "platform_url": (j.meta or {}).get("platform_url"),
            "error": j.error,
            "published": bool(j.result_slug and j.result_slug in published_slugs),
        }
        for j in _ordered
    ]
    done_hidden = max(0, len(_done) - 4)
    failed_count = sum(1 for j in all_jobs if j.state == "failed")
    publish_count = sum(
        1 for j in all_jobs
        if j.state == "done"
        and j.kind in ("video", "transcript")
        and not (j.result_slug and j.result_slug in published_slugs)
    )
    runs_need_publish = sum(1 for r in runs if r.get("is_video_run") and not r.get("published"))
    yt_status = youtube_auth.get_status()
    return templates.TemplateResponse(
        request,
        "content_index.html",
        {
            "active": "content",
            "runs": runs,
            "incoming_files": incoming_files,
            "incoming_path": str(INCOMING_DIR),
            "recent_jobs": recent_jobs,
            "done_hidden": done_hidden,
            "failed_count": failed_count,
            "publish_count": publish_count,
            "runs_need_publish": runs_need_publish,
            "yt_status": yt_status,
        },
    )


@app.post("/content/upload")
async def content_upload(
    file: UploadFile = File(...),
    slug: str = Form(""),
    product: str = Form("both"),
    premium: str = Form(""),
):
    """Receive a transcript upload, save to incoming/, and run the multiplier."""
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "transcript.txt").name
    if Path(filename).suffix.lower() not in ALLOWED_TRANSCRIPT_EXTS:
        return JSONResponse(
            {"error": f"Unsupported file type. Allowed: {sorted(ALLOWED_TRANSCRIPT_EXTS)}"},
            status_code=400,
        )

    target = INCOMING_DIR / filename
    if target.exists():
        ts = int(time.time())
        target = INCOMING_DIR / f"{Path(filename).stem}-{ts}{Path(filename).suffix}"

    payload = await file.read()
    target.write_bytes(payload)

    # Run the multiplier synchronously — UI shows a "Working" overlay
    result = content_multiplier.process_transcript(
        target,
        slug=(slug or None),
        product=product,
        premium=bool(premium),
    )

    # Move source to .processed/ regardless of success
    processed = INCOMING_DIR / ".processed"
    processed.mkdir(parents=True, exist_ok=True)
    target.rename(processed / target.name)

    if result.error:
        return JSONResponse(
            {"error": result.error, "out_dir": str(result.out_dir)},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "slug": result.out_dir.name,
        "redirect": f"/content/{result.out_dir.name}",
    })


@app.post("/content/scan")
def content_scan():
    """Process every transcript currently in content/incoming/."""
    results = content_multiplier.scan_incoming()
    summary = [
        {
            "out_dir": str(r.out_dir.name),
            "files": r.files,
            "error": r.error,
        }
        for r in results
    ]
    return {"processed": summary}


@app.post("/content/job/{job_id}/delete")
def content_job_delete(job_id: str):
    """Remove a finished or failed job from the activity list."""
    job = jobs_mod.get_job(job_id)
    if job and job.state in ("done", "failed"):
        jobs_mod.delete_job(job_id)
    return RedirectResponse("/content", status_code=303)


@app.post("/content/jobs/clear-failed")
def content_jobs_clear_failed():
    """Remove all failed jobs from the activity list in one click."""
    jobs_mod.delete_jobs_by_state("failed")
    return RedirectResponse("/content", status_code=303)


@app.get("/content/{slug}", response_class=HTMLResponse)
def content_detail(request: Request, slug: str):
    folder = _content_run_folder(slug)
    if folder is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    if not folder.exists() or not folder.is_dir():
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    files = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix not in {".md", ".txt", ".json", ".srt"}:
            continue
        files.append({
            "name": f.name,
            "ext": f.suffix,
            "content": f.read_text(encoding="utf-8", errors="replace"),
        })

    meta_path = folder / "00-meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    # Pipeline manifest (only present for video-pipeline runs)
    manifest_path = folder / "00-pipeline-manifest.json"
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    # Per-clip distribution kits — load caption.txt for each platform per clip
    clips_for_view = []
    long_form_for_view = None
    if manifest and manifest.get("clips"):
        for c in manifest["clips"]:
            dist = {}
            for platform, rel in (c.get("distribution") or {}).items():
                cap_path = folder / rel / "caption.txt"
                title_path = folder / rel / "title.txt"
                meta_path_p = folder / rel / "metadata.json"
                cap = cap_path.read_text(encoding="utf-8") if cap_path.exists() else ""
                ttl = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
                pm = {}
                if meta_path_p.exists():
                    try:
                        pm = json.loads(meta_path_p.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass
                dist[platform] = {
                    "rel": rel,
                    "title": ttl,
                    "caption": cap,
                    "uploaded_at": pm.get("uploaded_at"),
                    "platform_url": pm.get("platform_url"),
                    "website_url": pm.get("website_url"),
                }
            clips_for_view.append({
                "index": c["index"],
                "title": c["title"],
                "start": c["start"],
                "end": c["end"],
                "duration_sec": c["duration_sec"],
                "clip_url": f"/content/clip/{slug}/{c['clip']}",
                "distribution": dist,
            })
    if manifest and (manifest.get("long_form") or {}).get("enabled"):
        lf = manifest["long_form"]
        dist = {}
        for platform, rel in (lf.get("distribution") or {}).items():
            cap_path = folder / rel / "caption.txt"
            title_path = folder / rel / "title.txt"
            meta_path_p = folder / rel / "metadata.json"
            cap = cap_path.read_text(encoding="utf-8") if cap_path.exists() else ""
            ttl = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
            pm = {}
            if meta_path_p.exists():
                try:
                    pm = json.loads(meta_path_p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            dist[platform] = {
                "rel": rel,
                "title": ttl,
                "caption": cap,
                "uploaded_at": pm.get("uploaded_at"),
                "platform_url": pm.get("platform_url"),
                "website_url": pm.get("website_url"),
            }
        long_form_for_view = {
            "title": lf.get("title") or "Long-form YouTube video",
            "duration_sec": lf.get("duration_sec"),
            "distribution": dist,
            "website_url": lf.get("website_url"),
        }

    # Per-clip distribution scorecard across the 5 target channels.
    dist_status = {}
    status_path = folder / "00-distribution-status.json"
    if status_path.exists():
        try:
            dist_status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            dist_status = {}
    _SCORECARD = [
        ("youtube", "YouTube", True), ("tiktok", "TikTok", True),
        ("instagram", "Instagram", False), ("x", "X", False), ("linkedin", "LinkedIn", False),
    ]
    for clip in clips_for_view:
        marks = dist_status.get(str(clip["index"]), {})
        sc = []
        for ch, label, auto in _SCORECARD:
            done = bool((clip["distribution"].get(ch) or {}).get("uploaded_at")) if auto else bool(marks.get(ch))
            sc.append({"channel": ch, "label": label, "auto": auto, "done": done})
        clip["scorecard"] = sc
        clip["distributed"] = sum(1 for s in sc if s["done"])
        clip["channels_total"] = len(_SCORECARD)
        clip["pct"] = round(100 * clip["distributed"] / len(_SCORECARD))

    yt_status = youtube_auth.get_status()
    import tiktok_auth
    tt_connected = tiktok_auth.is_connected()
    campaign_id = meta.get("campaign_id") or slug
    cta_url = f"https://shadowedgetools.com/checklist?utm_source=social&utm_medium=organic&utm_campaign={campaign_id}"
    attribution = _run_attribution(campaign_id)
    return templates.TemplateResponse(
        request,
        "content_detail.html",
        {
            "active": "content",
            "slug": slug,
            "files": files,
            "meta": meta,
            "manifest": manifest,
            "clips": clips_for_view,
            "long_form": long_form_for_view,
            "yt_status": yt_status,
            "tt_connected": tt_connected,
            "campaign_id": campaign_id,
            "cta_url": cta_url,
            "attribution": attribution,
        },
    )


@app.post("/content/{slug}/save")
def content_save(slug: str, filename: str = Form(...), content: str = Form(...)):
    folder = _content_run_folder(slug)
    if folder is None:
        return RedirectResponse(url="/content", status_code=303)
    if not folder.exists():
        return RedirectResponse(url="/content", status_code=303)
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith(".") or "/" in safe_name or "\\" in safe_name:
        return RedirectResponse(url=f"/content/{slug}", status_code=303)
    target = folder / safe_name
    if not target.exists():
        return RedirectResponse(url=f"/content/{slug}", status_code=303)
    target.write_text(content, encoding="utf-8")
    return RedirectResponse(url=f"/content/{slug}#{safe_name}", status_code=303)


# ---------------------------------------------------------------------------
# Video pipeline routes
# ---------------------------------------------------------------------------

@app.post("/content/upload-video")
async def content_upload_video(
    request: Request,
    file: UploadFile = File(...),
    slug: str = Form(""),
    product: str = Form("both"),
    provider: str = Form("local"),
    whisper_model: str = Form("base"),
    premium: str = Form(""),
    creator_guidance: str = Form(""),
):
    """Stream a video file to disk, then kick off the pipeline as a background job.

    Streaming write avoids holding gigabytes in RAM. Returns the job id so the
    UI can redirect to the status page.
    """
    INCOMING_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "video.mp4").name
    if Path(filename).suffix.lower() not in ALLOWED_VIDEO_EXTS:
        return JSONResponse(
            {"error": f"Unsupported video type. Allowed: {sorted(ALLOWED_VIDEO_EXTS)}"},
            status_code=400,
        )

    blockers = _video_upload_blockers(provider)
    if blockers:
        await file.close()
        return JSONResponse(
            {
                "error": "Video pipeline is not ready: " + " ".join(blockers),
                "blockers": blockers,
            },
            status_code=400,
        )

    target = INCOMING_VIDEOS_DIR / filename
    if target.exists():
        ts = int(time.time())
        target = INCOMING_VIDEOS_DIR / f"{Path(filename).stem}-{ts}{Path(filename).suffix}"

    # Stream the upload to disk in 4MB chunks
    bytes_written = 0
    chunk = await file.read(4 * 1024 * 1024)
    with open(target, "wb") as out:
        while chunk:
            out.write(chunk)
            bytes_written += len(chunk)
            chunk = await file.read(4 * 1024 * 1024)
    await file.close()
    if bytes_written <= 0:
        target.unlink(missing_ok=True)
        return JSONResponse(
            {"error": "Upload was empty. Re-select the video and wait for the upload to finish before running the pipeline."},
            status_code=400,
        )
    creator_guidance = (creator_guidance or "").strip()[:2000]

    job = jobs_mod.create_job(
        kind="video",
        source_filename=target.name,
        meta={
            "video_path": str(target),
            "size_bytes": bytes_written,
            "provider": provider,
            "whisper_model": whisper_model,
            "product": product,
            "slug": slug or None,
            "premium": bool(premium),
            "creator_guidance": creator_guidance or None,
        },
    )
    jobs_mod.run_job(
        job.id,
        video_pipeline.process_video_job,
        args=(str(target),),
        kwargs={
            "slug": slug or None,
            "product": product,
            "transcribe_provider": provider,
            "whisper_model": whisper_model,
            "premium": bool(premium),
            "creator_guidance": creator_guidance,
        },
    )
    return JSONResponse({
        "ok": True,
        "job_id": job.id,
        "redirect": f"/content/job/{job.id}/view",
    })


@app.get("/content/job/{job_id}")
def content_job_status(job_id: str):
    """Polling endpoint — returns the current job JSON."""
    job = jobs_mod.get_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "kind": job.kind,
        "state": job.state,
        "current_step": job.current_step,
        "progress_pct": job.progress_pct,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "source_filename": job.source_filename,
        "result_slug": job.result_slug,
        "log_tail": job.log[-12:],
    }


@app.get("/content/job/{job_id}/view", response_class=HTMLResponse)
def content_job_view(request: Request, job_id: str):
    job = jobs_mod.get_job(job_id)
    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    return templates.TemplateResponse(
        request,
        "content_job.html",
        {
            "active": "content",
            "job": job,
        },
    )


@app.get("/content/clip/{slug}/{filename:path}")
def content_clip_serve(slug: str, filename: str):
    """Serve a clip .mp4 (or any file inside a content/<slug>/ folder)."""
    folder = _content_run_folder(slug)
    if folder is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    target = (folder / filename).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    from fastapi.responses import FileResponse
    media_type = None
    if target.suffix.lower() in {".mp4", ".m4v"}:
        media_type = "video/mp4"
    elif target.suffix.lower() == ".webm":
        media_type = "video/webm"
    return FileResponse(target, media_type=media_type)


# ---------------------------------------------------------------------------
# YouTube auth + upload
# ---------------------------------------------------------------------------

def _youtube_redirect_uri(request: Request) -> str:
    """Build the canonical redirect URI from the incoming request host.

    Whatever value we use here MUST be in the OAuth client's authorized list.
    Default is `http://localhost:8876/content/youtube/auth/callback`.
    """
    base = str(request.base_url).rstrip("/")
    return f"{base}/content/youtube/auth/callback"


@app.get("/content/youtube/settings", response_class=HTMLResponse)
def youtube_settings(request: Request):
    status = youtube_auth.get_status()
    return templates.TemplateResponse(
        request,
        "youtube_settings.html",
        {
            "active": "content",
            "status": status,
            "redirect_uri": _youtube_redirect_uri(request),
            "client_secret_path": str(youtube_auth.CLIENT_SECRET_PATH),
        },
    )


@app.get("/content/youtube/auth/start")
def youtube_auth_start(request: Request):
    if not youtube_auth.has_client_secret():
        return RedirectResponse(url="/youtube", status_code=303)
    try:
        auth_url = youtube_auth.start_auth(_youtube_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return RedirectResponse(url=auth_url, status_code=303)


@app.get("/content/youtube/auth/callback", response_class=HTMLResponse)
def youtube_auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(
            f"<h1>Authorization denied</h1><p>{error}</p>"
            "<p><a href='/youtube'>← back</a></p>",
            status_code=400,
        )
    if not code:
        return HTMLResponse("<h1>Missing code</h1>", status_code=400)
    try:
        youtube_auth.finish_auth(code, _youtube_redirect_uri(request), state)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            f"<h1>OAuth failed</h1><pre>{exc}</pre>"
            "<p><a href='/youtube'>← back</a></p>",
            status_code=500,
        )
    return RedirectResponse(url="/youtube?just_connected=1", status_code=303)


@app.get("/content/youtube/auth/status")
def youtube_auth_status():
    s = youtube_auth.get_status()
    return {
        "connected": s.connected,
        "has_client_secret": s.has_client_secret,
        "channel_id": s.channel_id,
        "channel_title": s.channel_title,
        "last_refreshed": s.last_refreshed,
        "error": s.error,
    }


@app.post("/content/youtube/auth/disconnect")
def youtube_auth_disconnect():
    youtube_auth.disconnect()
    return RedirectResponse(url="/youtube", status_code=303)


# ---------------------------------------------------------------------------
# TikTok — Connect (Login Kit) + upload short clips to drafts (Content Posting)
# ---------------------------------------------------------------------------
def _tiktok_redirect_uri(request: Request) -> str:
    """Redirect URI for TikTok OAuth. Override with TIKTOK_REDIRECT_URI in .env
    (e.g. https://shadowedgetools.com/tiktok/callback, then use the paste-code
    fallback); otherwise default to this app's local callback."""
    override = os.environ.get("TIKTOK_REDIRECT_URI", "").strip()
    if override:
        return override
    return f"{str(request.base_url).rstrip('/')}/tiktok/auth/callback"


def _tiktok_clips() -> list[dict]:
    """Generated 9:16 short clips available to push to TikTok drafts."""
    clips: list[dict] = []
    for mp4 in (ROOT / "content").glob("*/clips/*.mp4"):
        try:
            clips.append({
                "path": str(mp4.relative_to(ROOT)).replace("\\", "/"),
                "name": mp4.name,
                "run": mp4.parent.parent.name,
                "size_mb": round(mp4.stat().st_size / 1048576, 1),
                "mtime": mp4.stat().st_mtime,
            })
        except OSError:
            continue
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    return clips[:40]


@app.get("/tiktok", response_class=HTMLResponse)
def tiktok_settings(request: Request, msg: str = "", err: str = ""):
    import tiktok_auth
    return templates.TemplateResponse(request, "tiktok_settings.html", {
        "active": "tiktok",
        "status": tiktok_auth.get_status(),
        "redirect_uri": _tiktok_redirect_uri(request),
        "clips": _tiktok_clips(),
        "msg": msg,
        "err": err,
    })


@app.get("/tiktok/auth/start")
def tiktok_auth_start(request: Request):
    import tiktok_auth
    if not tiktok_auth.has_credentials():
        return RedirectResponse("/tiktok?err=Set+TIKTOK_CLIENT_KEY+and+TIKTOK_CLIENT_SECRET+in+.env+first", status_code=303)
    url = tiktok_auth.authorize_url(_tiktok_redirect_uri(request), secrets.token_urlsafe(16))
    return RedirectResponse(url=url, status_code=303)


@app.get("/tiktok/auth/callback", response_class=HTMLResponse)
def tiktok_auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    import tiktok_auth
    if error:
        return HTMLResponse(f"<h1>TikTok authorization denied</h1><p>{error}</p><p><a href='/tiktok'>&larr; back</a></p>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing code</h1><p><a href='/tiktok'>&larr; back</a></p>", status_code=400)
    try:
        tiktok_auth.finish_auth(code, _tiktok_redirect_uri(request), state)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<h1>TikTok OAuth failed</h1><pre>{exc}</pre><p><a href='/tiktok'>&larr; back</a></p>", status_code=500)
    return RedirectResponse("/tiktok?msg=Connected+to+TikTok", status_code=303)


@app.post("/tiktok/auth/paste-code")
def tiktok_auth_paste(code: str = Form(...)):
    from urllib.parse import quote_plus
    import tiktok_auth
    try:
        tiktok_auth.finish_auth(code, None, None)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/tiktok?err=" + quote_plus(str(exc)), status_code=303)
    return RedirectResponse("/tiktok?msg=Connected+to+TikTok", status_code=303)


@app.post("/tiktok/auth/disconnect")
def tiktok_auth_disconnect():
    import tiktok_auth
    tiktok_auth.disconnect()
    return RedirectResponse("/tiktok?msg=Disconnected", status_code=303)


@app.post("/tiktok/upload")
def tiktok_upload_clip(clip: str = Form(...)):
    from urllib.parse import quote_plus
    import tiktok_upload
    rel = clip.replace("\\", "/").lstrip("/")
    if not rel.startswith("content/") or ".." in rel or not rel.endswith(".mp4"):
        return RedirectResponse("/tiktok?err=Invalid+clip+path", status_code=303)
    path = ROOT / rel
    if not path.exists():
        return RedirectResponse("/tiktok?err=Clip+not+found", status_code=303)
    try:
        result = tiktok_upload.upload_to_inbox(path)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/tiktok?err=" + quote_plus(str(exc)), status_code=303)
    return RedirectResponse("/tiktok?msg=" + quote_plus(f"Uploaded to your TikTok drafts (publish_id {result.get('publish_id', '?')}). Open the TikTok app to review and post."), status_code=303)


class _ClipLookupError(Exception):
    """Raised when a run clip / its TikTok kit can't be located."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _resolve_run_clip(slug: str, clip_index: int):
    """Locate a run clip's mp4 + its TikTok kit folder/metadata.

    Returns (clip_path, kit_folder, metadata_path, kit_meta). Raises
    _ClipLookupError(message, status) when the run or clip can't be found.
    """
    folder = _content_run_folder(slug)
    if folder is None or not folder.exists():
        raise _ClipLookupError(f"Run not found: {slug}", 404)

    # Locate the clip mp4 via the pipeline manifest (authoritative), else clips/.
    clip_path = None
    manifest_path = folder / "00-pipeline-manifest.json"
    if manifest_path.exists():
        try:
            mani = json.loads(manifest_path.read_text(encoding="utf-8"))
            for c in mani.get("clips", []):
                if int(c.get("index", -1)) == clip_index and c.get("clip"):
                    clip_path = folder / c["clip"]
                    break
        except (json.JSONDecodeError, ValueError):
            pass
    if (not clip_path or not clip_path.exists()) and (folder / "clips").is_dir():
        matches = sorted((folder / "clips").glob(f"{clip_index:02d}-*.mp4"))
        clip_path = matches[0] if matches else None
    if not clip_path or not clip_path.exists():
        raise _ClipLookupError(f"Clip {clip_index} mp4 not found for run {slug}", 404)

    kit_folder = folder / "distribution" / "tiktok" / f"{clip_index:02d}"
    metadata_path = kit_folder / "metadata.json"
    kit_meta = {}
    if metadata_path.exists():
        try:
            kit_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            kit_meta = {}
    return clip_path, kit_folder, metadata_path, kit_meta


def _tiktok_clip_caption(kit_folder, fallback: str) -> str:
    """The clip's TikTok caption — kit caption.txt with any internal production notes
    stripped (publish-time safety net for older kits) — or a safe brand fallback."""
    caption_path = kit_folder / "caption.txt"
    text = ""
    if caption_path.exists():
        text = caption_path.read_text(encoding="utf-8").strip()
    if not text:
        return fallback
    try:
        cleaned = video_pipeline.clean_caption_for_posting(text)
    except Exception:  # noqa: BLE001 - stub/unavailable pipeline: post raw rather than fail
        cleaned = text
    return cleaned or fallback


@app.post("/content/{slug}/clip/{clip_index}/upload-tiktok")
def tiktok_upload_clip_in_run(slug: str, clip_index: int):
    """Push one run clip straight to the connected TikTok account's drafts."""
    import tiktok_auth
    import tiktok_upload
    if not tiktok_auth.is_connected():
        return JSONResponse({"error": "TikTok not connected. Visit /tiktok to connect."}, status_code=400)
    try:
        clip_path, kit_folder, metadata_path, kit_meta = _resolve_run_clip(slug, clip_index)
    except _ClipLookupError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    if kit_meta.get("uploaded_at"):
        return JSONResponse({"error": "This clip is already in your TikTok drafts."}, status_code=409)

    try:
        result = tiktok_upload.upload_to_inbox(clip_path)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)

    kit_folder.mkdir(parents=True, exist_ok=True)
    kit_meta.update({
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "publish_id": result.get("publish_id"),
        "platform": "tiktok",
        "via": "console-run",
    })
    metadata_path.write_text(json.dumps(kit_meta, indent=2), encoding="utf-8")
    return JSONResponse({
        "ok": True,
        "publish_id": result.get("publish_id"),
        "message": "Uploaded to your TikTok drafts. Open the TikTok app to add sound, finalize, and post.",
    })


@app.post("/content/{slug}/clip/{clip_index}/publish-tiktok")
def tiktok_publish_clip_in_run(slug: str, clip_index: int):
    """One-click PUBLIC publish of a run clip directly to the connected TikTok account.

    Human-gated by design (Jordan reviews, then clicks): we do NOT auto-post. Needs
    the video.publish scope (audited app); direct_post refuses if TikTok hasn't
    cleared the account for public posts. brand_organic_toggle discloses these as
    own-brand promotional clips (TikTok commercial-content compliance)."""
    import tiktok_auth
    import tiktok_upload
    if not tiktok_auth.is_connected():
        return JSONResponse({"error": "TikTok not connected. Visit /tiktok to connect."}, status_code=400)
    try:
        clip_path, kit_folder, metadata_path, kit_meta = _resolve_run_clip(slug, clip_index)
    except _ClipLookupError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    if kit_meta.get("published_at"):
        return JSONResponse({"error": "This clip is already published to TikTok."}, status_code=409)

    caption = _tiktok_clip_caption(
        kit_folder, "Shadow Edge Tools — risk-management add-ons for NinjaTrader 8."
    )
    try:
        result = tiktok_upload.direct_post(
            clip_path,
            title=caption,
            privacy_level="PUBLIC_TO_EVERYONE",
            brand_organic_toggle=True,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)

    kit_folder.mkdir(parents=True, exist_ok=True)
    kit_meta.update({
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "publish_id": result.get("publish_id"),
        "platform": "tiktok",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "via": "console-direct-post",
    })
    metadata_path.write_text(json.dumps(kit_meta, indent=2), encoding="utf-8")
    return JSONResponse({
        "ok": True,
        "publish_id": result.get("publish_id"),
        "message": "Publishing to TikTok now (public). It lands on your profile once TikTok finishes processing.",
    })


@app.post("/content/{slug}/clip/{clip_index}/mark-posted")
def mark_clip_posted(slug: str, clip_index: int, channel: str = Form(...)):
    """Toggle a manual-distribution channel (X / LinkedIn / Instagram) for a clip."""
    channel = (channel or "").lower().strip()
    if channel not in ("instagram", "x", "linkedin"):
        return JSONResponse({"error": "That channel isn't toggled here."}, status_code=400)
    folder = _content_run_folder(slug)
    if folder is None:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    if not folder.exists():
        return JSONResponse({"error": "Run not found"}, status_code=404)
    status_path = folder / "00-distribution-status.json"
    data = {}
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    key = str(clip_index)
    clip_status = data.get(key, {})
    if clip_status.get(channel):
        clip_status.pop(channel, None)
        posted = False
    else:
        clip_status[channel] = datetime.now().isoformat(timespec="seconds")
        posted = True
    if clip_status:
        data[key] = clip_status
    else:
        data.pop(key, None)
    status_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "channel": channel, "posted": posted})


@app.post("/content/{slug}/clip/{clip_index}/kit/{platform}/save")
def save_clip_kit(slug: str, clip_index: int, platform: str, title: str = Form(""), caption: str = Form("")):
    """Persist an edited distribution-kit title + caption back to the kit files."""
    platform = (platform or "").lower().strip()
    if platform not in ("youtube", "tiktok", "instagram", "rumble"):
        return JSONResponse({"error": "unknown platform"}, status_code=400)
    folder = _content_run_folder(slug)
    if folder is None:
        return JSONResponse({"error": "kit not found"}, status_code=404)
    kit_folder = folder / "distribution" / platform / f"{clip_index:02d}"
    if not kit_folder.exists():
        return JSONResponse({"error": "kit not found"}, status_code=404)
    (kit_folder / "title.txt").write_text((title or "").strip() + "\n", encoding="utf-8")
    (kit_folder / "caption.txt").write_text(caption or "", encoding="utf-8")
    return JSONResponse({"ok": True})


@app.post("/content/{slug}/clip/{clip_index}/upload-youtube")
def youtube_upload_clip(
    slug: str,
    clip_index: int,
    privacy: str = Form(""),
    title: str = Form(""),
):
    if not youtube_auth.is_connected():
        return JSONResponse(
            {"error": "YouTube not connected. Visit /youtube to connect."},
            status_code=400,
        )
    folder = _content_run_folder(slug)
    if folder is None:
        return JSONResponse({"error": f"Run not found: {slug}"}, status_code=404)
    if not folder.exists():
        return JSONResponse({"error": f"Run not found: {slug}"}, status_code=404)
    kit_folder = folder / "distribution" / "youtube" / f"{clip_index:02d}"
    if not kit_folder.exists():
        return JSONResponse({"error": f"Clip kit missing: {kit_folder}"}, status_code=404)

    metadata_path = kit_folder / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
        if metadata.get("uploaded_at") or metadata.get("platform_url"):
            return JSONResponse(
                {"error": "This clip already has a YouTube upload recorded. Open the existing YouTube link or clear the kit metadata before re-uploading."},
                status_code=409,
            )

    privacy_value = (privacy or "unlisted").lower()
    if privacy_value not in {"unlisted", "private", "public"}:
        privacy_value = "unlisted"

    job = jobs_mod.create_job(
        kind="youtube-upload",
        source_filename=f"{slug}/clip-{clip_index:02d}",
        meta={
            "slug": slug,
            "clip_index": clip_index,
            "privacy_override": privacy_value,
            "title_override": title or None,
            "triggered_from": "console-manual",
        },
    )
    jobs_mod.run_job(
        job.id,
        youtube_upload.upload_clip_job,
        args=(slug, clip_index),
        kwargs={
            "privacy_override": privacy_value,
            "title_override": title or None,
        },
    )
    return JSONResponse({
        "ok": True,
        "job_id": job.id,
        "redirect": f"/content/job/{job.id}/view",
    })


# ---------------------------------------------------------------------------
# Folder watcher (background thread)
# ---------------------------------------------------------------------------

_WATCHER_INTERVAL_SEC = 6.0
_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


def _watcher_loop():
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    while not _watcher_stop.is_set():
        try:
            # Cheap probe: are there any eligible files?
            has_work = any(
                p.is_file() and p.suffix.lower() in ALLOWED_TRANSCRIPT_EXTS
                for p in INCOMING_DIR.iterdir()
            )
            if has_work:
                content_multiplier.scan_incoming()
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] error: {exc}", file=sys.stderr)
        _watcher_stop.wait(_WATCHER_INTERVAL_SEC)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request):
    import leads as leads_mod
    return templates.TemplateResponse(request, "leads.html", {
        "active": "leads",
        "leads": leads_mod.list_leads(),
        "lead_magnets": _lead_assets("lead-magnets"),
        "email_sequences": _lead_assets("email-sequences"),
    })


@app.get("/leads/export.csv")
def leads_export():
    import leads as leads_mod
    out = STATE_DIR / "leads-export.csv"
    leads_mod.export_csv(out)
    return Response(content=out.read_text(encoding="utf-8"), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=leads-export.csv"})


@app.get("/lead/{campaign_id}", response_class=HTMLResponse)
def lead_capture_form(request: Request, campaign_id: str, utm_source: str = "organic", utm_medium: str = "organic", utm_content: str = "lead-form"):
    return templates.TemplateResponse(request, "lead_capture.html", {"active": "leads", "campaign_id": campaign_id, "utm_source": utm_source, "utm_medium": utm_medium, "utm_content": utm_content, "saved": False, "error": ""})


@app.post("/lead/{campaign_id}", response_class=HTMLResponse)
def lead_capture_submit(request: Request, campaign_id: str, email: str = Form(...), consent: str = Form(""), utm_source: str = Form("organic"), utm_medium: str = Form("organic"), utm_content: str = Form("lead-form")):
    import leads as leads_mod
    import email_nurture
    error = ""
    saved = False
    try:
        lead, _created = leads_mod.capture_lead(email=email, campaign_id=campaign_id, consent=(consent == "yes"), utm_source=utm_source, utm_medium=utm_medium, utm_content=utm_content, landing_page=str(request.url), user_agent=request.headers.get("user-agent", ""))
        email_nurture.queue_for_lead(lead, campaign_id)
        saved = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return templates.TemplateResponse(request, "lead_capture.html", {"active": "leads", "campaign_id": campaign_id, "utm_source": utm_source, "utm_medium": utm_medium, "utm_content": utm_content, "saved": saved, "error": error})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_public_consent(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _is_local_host(value: str) -> bool:
    host = (value or "").split(",", 1)[0].strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host not in {"::1"} and host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _has_public_forwarding_headers(request: Request) -> bool:
    forwarded_host = request.headers.get("x-forwarded-host", "")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_host and not _is_local_host(forwarded_host):
        return True
    return bool(forwarded_proto and forwarded_proto.lower() == "https" and not _is_local_host(request.headers.get("host", "")))


@app.post("/api/leads")
async def api_capture_lead(request: Request, x_shadowedge_token: str | None = Header(default=None)):
    import email_nurture
    import leads as leads_mod
    token = os.environ.get("PUBLIC_LEAD_API_TOKEN", "")
    client_host = request.client.host if request.client else ""
    local_hosts = {"127.0.0.1", "::1", "localhost", "testclient"}
    allow_unauth_dev = _env_truthy("PUBLIC_LEAD_ALLOW_UNAUTHENTICATED") and _env_truthy("PUBLIC_LEAD_DEV_OVERRIDE")
    local_no_proxy = client_host in local_hosts and not _has_public_forwarding_headers(request)
    if not token and not local_no_proxy and not allow_unauth_dev:
        raise HTTPException(status_code=401, detail="public lead token required outside local/test mode")
    if token and not secrets.compare_digest(x_shadowedge_token or "", token):
        raise HTTPException(status_code=401, detail="invalid or missing public lead token")
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed request body should be a client error
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    try:
        consent = _parse_public_consent(data.get("consent"))
        lead, created = leads_mod.capture_lead(
            email=data.get("email", ""),
            campaign_id=data.get("campaign_id", ""),
            consent=consent,
            utm_source=data.get("utm_source", ""),
            utm_medium=data.get("utm_medium", ""),
            utm_content=data.get("utm_content", ""),
            landing_page=data.get("landing_page", "api"),
            user_agent=request.headers.get("user-agent", ""),
            name=data.get("name", ""),
            interest=data.get("interest", "") or data.get("product_name", "") or data.get("productName", ""),
            note=data.get("note", "") or data.get("message", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    queued = email_nurture.queue_for_lead(lead, data.get("campaign_id", "")) if consent else []
    try:
        import tracking
        tracking.append_event({
            "event": "lead_capture",
            "campaign_id": data.get("campaign_id", ""),
            "source": data.get("utm_source", "api") or "api",
            "utm_source": data.get("utm_source", ""),
            "utm_medium": data.get("utm_medium", ""),
            "utm_content": data.get("utm_content", ""),
            "value": 1,
        })
    except Exception:
        pass
    return {"ok": True, "created": created, "email": lead.get("email"), "lead_id": lead.get("email"), "email_queue_created": len(queued), "consent": consent}


@app.get("/email-queue", response_class=HTMLResponse)
def email_queue_page(request: Request, filter: str = "queued", msg: str = "", err: str = ""):
    import email_nurture
    view = email_nurture.build_queue_view(filter)
    provider = email_nurture.provider_status()
    recipient_count = len(email_nurture.eligible_blast_recipients())
    return templates.TemplateResponse(request, "email_queue.html", {
        "active": "email_queue",
        "queue": view["rows"],
        "view": view,
        "summary": view["summary"],
        "filter_labels": view["filter_labels"],
        "selected_filter": view["filter"],
        "provider": provider,
        "blast_recipient_count": recipient_count,
        "recent_blasts": email_nurture.recent_blasts(),
        "msg": msg,
        "err": err,
    })


@app.get("/listening", response_class=HTMLResponse)
def listening_page(request: Request, scan: str = ""):
    import listening
    cfg = listening.load_config()
    sources = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    source_health = listening.source_health(cfg)
    keywords = cfg.get("keywords") if isinstance(cfg.get("keywords"), list) else []
    page_error = ""
    try:
        opportunities = listening.list_opportunities()[:200]
        queue = listening.queue_items()
        last_scan = listening.last_scan_summary()
    except Exception as exc:  # noqa: BLE001 - render the page with the problem visible
        opportunities = []
        queue = []
        last_scan = None
        page_error = str(exc)
    queue_counts: dict[str, int] = {}
    for item in queue:
        key = item.get("status", "drafted")
        queue_counts[key] = queue_counts.get(key, 0) + 1
    return templates.TemplateResponse(request, "listening.html", {
        "active": "listening",
        "keywords": keywords,
        "sources": sources,
        "source_health": source_health,
        "opportunities": opportunities,
        "last_scan": last_scan,
        "queue_counts": queue_counts,
        "scan_started": scan == "started",
        "scan_running": scan == "running",
        "error": page_error,
    })


@app.post("/listening/scan")
def listening_scan(source: str = Form(""), draft: str = Form("0")):
    if not _trigger_listening_scan(only=source or None, draft=draft == "1"):
        return RedirectResponse("/listening?scan=running", status_code=303)
    return RedirectResponse("/listening?scan=started", status_code=303)


@app.get("/listening/scan/status")
def listening_scan_status():
    """Live status of the background scan for the progress bar (polled by JS)."""
    st = dict(_scan_state)
    if st.get("started_epoch"):
        end = st.get("finished_epoch") or time.time()
        st["elapsed_seconds"] = round(end - st["started_epoch"], 1)
    return JSONResponse(st)


@app.get("/engagement", response_class=HTMLResponse)
def engagement_page(request: Request, status: str = "", err: str = ""):
    import listening
    items = listening.queue_items(status=status or None)
    return templates.TemplateResponse(request, "engagement.html", {
        "active": "engagement",
        "items": items,
        "status_filter": status,
        "error": err,
    })


@app.post("/engagement/action")
def engagement_action(
    item_id: str = Form(...),
    action: str = Form(...),
    posted_url: str = Form(""),
    note: str = Form(""),
    draft_reply: str = Form(""),
):
    import listening
    try:
        result = listening.set_queue_status(
            item_id,
            action,
            posted_url=posted_url.strip(),
            note=note.strip(),
            draft_reply=draft_reply.strip(),
        )
        if result is None:
            return RedirectResponse("/engagement?err=Item+not+found", status_code=303)
    except Exception as exc:  # noqa: BLE001 - local console should redirect with a readable reason
        from urllib.parse import quote_plus
        return RedirectResponse(f"/engagement?err={quote_plus(str(exc))}", status_code=303)
    return RedirectResponse("/engagement", status_code=303)


_SCOREBOARD_SECTIONS = [
    {"key": "sales", "label": "💰 Sales", "source": "manual · from Stripe until webhook wired", "metrics": [
        {"key": "revenue", "label": "Revenue", "prefix": "$"},
        {"key": "trials", "label": "Trials / signups"},
    ]},
    {"key": "website", "label": "🌐 Website", "source": "Google Analytics 4 — wire pending", "metrics": [
        {"key": "website_visitors", "label": "Visitors (30d)"},
        {"key": "website_pageviews", "label": "Page views (30d)"},
        {"key": "website_avg_time", "label": "Avg time on page"},
        {"key": "website_top_page", "label": "Top page"},
    ]},
    {"key": "tiktok", "label": "🎵 TikTok", "source": "manual · enable TIKTOK_STATS for auto followers/likes", "metrics": [
        {"key": "tiktok_followers", "label": "Followers"},
        {"key": "tiktok_views", "label": "Views (30d)"},
        {"key": "tiktok_likes", "label": "Total likes"},
    ]},
    {"key": "x", "label": "✕ X", "source": "manual — no public API", "metrics": [
        {"key": "x_followers", "label": "Followers"},
        {"key": "x_impressions", "label": "Impressions (30d)"},
        {"key": "x_interactions", "label": "Interactions (30d)"},
    ]},
    {"key": "linkedin", "label": "💼 LinkedIn", "source": "manual — enter yourself", "metrics": [
        {"key": "linkedin_followers", "label": "Followers"},
        {"key": "linkedin_impressions", "label": "Impressions (30d)"},
        {"key": "linkedin_interactions", "label": "Interactions (30d)"},
    ]},
    {"key": "instagram", "label": "📸 Instagram", "source": "manual — enter yourself", "metrics": [
        {"key": "instagram_followers", "label": "Followers"},
        {"key": "instagram_views", "label": "Views (30d)"},
        {"key": "instagram_interactions", "label": "Interactions (30d)"},
    ]},
]


def _metric_series(rows: list, key: str) -> list:
    """Sorted [(date, float)] for a metric — one point per date (latest that day)."""
    by_date: dict = {}
    for r in rows:
        if r.get("metric") == key and r.get("status", "measured") == "measured":
            try:
                fv = float(str(r.get("value", "")).strip())
            except (ValueError, TypeError):
                continue
            d, ra = r.get("date", ""), str(r.get("recorded_at", ""))
            if d and (d not in by_date or ra >= by_date[d][1]):
                by_date[d] = (fv, ra)
    return [(d, by_date[d][0]) for d in sorted(by_date)]


def _latest_value(rows: list, key: str):
    """Most recent raw (string) value for a metric, for display."""
    best = None
    for r in rows:
        if r.get("metric") == key and r.get("status", "measured") == "measured" and str(r.get("value", "")).strip() != "":
            if best is None or str(r.get("recorded_at", "")) > str(best.get("recorded_at", "")):
                best = r
    return best["value"] if best else None


def _sparkline_points(vals: list, w: int = 80, h: int = 18) -> str:
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    return " ".join(f"{round(i / (n - 1) * w, 1)},{round(h - (v - lo) / rng * h, 1)}" for i, v in enumerate(vals))


def _trend(vals: list) -> dict:
    """{spark, delta, dir} from a numeric series (delta = last point vs the one before)."""
    if len(vals) < 2:
        return {"spark": "", "delta": "", "dir": ""}
    spark = _sparkline_points(vals)
    d = vals[-1] - vals[-2]
    if not d:
        return {"spark": spark, "delta": "", "dir": ""}
    mag = str(int(abs(d))) if abs(d) == int(abs(d)) else f"{abs(d):.1f}"
    return {"spark": spark, "delta": ("▲ " if d > 0 else "▼ ") + mag, "dir": "up" if d > 0 else "down"}


def _snapshot_goal_daily(goal) -> None:
    """Record today's customer count once/day so it builds a trend over time."""
    if not goal:
        return
    try:
        from hermes_store import read_jsonl
        from analytics import PERFORMANCE_PATH, record_metric
        customers = goal.get("customers") if isinstance(goal, dict) else getattr(goal, "customers", None)
        if customers is None:
            return
        today = date.today().isoformat()
        if not any(r.get("metric") == "customers" and r.get("date") == today for r in read_jsonl(PERFORMANCE_PATH)):
            record_metric("customers", customers, source="goal", status="measured")
    except Exception:
        pass


def _snapshot_leads_daily() -> int | None:
    """Record today's total lead/email-capture count once/day so it builds a trend.

    Returns the current lead count (or None if leads can't be read)."""
    try:
        import leads as _leads
        from hermes_store import read_jsonl
        from analytics import PERFORMANCE_PATH, record_metric
        count = len(_leads.list_leads())
        today = date.today().isoformat()
        if not any(r.get("metric") == "leads" and r.get("date") == today for r in read_jsonl(PERFORMANCE_PATH)):
            record_metric("leads", count, source="leads", status="measured")
        return count
    except Exception:
        return None


def _scoreboard() -> dict:
    """Growth scoreboard: goal + auto YouTube + manual metrics, each with a trend."""
    try:
        dash = ops_tracker.compute_dashboard(ops_tracker.load_state())
        goal = dash.get("goal") if isinstance(dash, dict) else getattr(dash, "goal", None)
    except Exception:
        goal = None
    _snapshot_goal_daily(goal)
    lead_count = _snapshot_leads_daily()

    from hermes_store import read_jsonl
    from analytics import PERFORMANCE_PATH
    perf_rows = read_jsonl(PERFORMANCE_PATH)

    keys = [m["key"] for s in _SCOREBOARD_SECTIONS for m in s["metrics"]]
    values = {k: _latest_value(perf_rows, k) for k in keys}
    trends = {k: _trend([v for _, v in _metric_series(perf_rows, k)]) for k in keys}
    customers_trend = _trend([v for _, v in _metric_series(perf_rows, "customers")])
    leads = {
        "count": lead_count if lead_count is not None else _latest_value(perf_rows, "leads"),
        "trend": _trend([v for _, v in _metric_series(perf_rows, "leads")]),
    }

    yt = {"connected": False}
    try:
        import youtube_stats

        def _i(x):
            try:
                return int(float(x))
            except (ValueError, TypeError):
                return 0

        def _chan_series(field):
            by_date = {}
            for r in read_jsonl(STATE_DIR / "youtube-channel-stats.jsonl"):
                d = str(r.get("pulled_at", ""))[:10]
                if d and r.get(field) is not None:
                    by_date[d] = _i(r.get(field))
            return [by_date[d] for d in sorted(by_date)]

        ch = youtube_stats.channel_summary() or {}
        vids = youtube_stats.video_summary() or []
        yt = {
            "connected": bool(ch),
            "needs_reconnect": youtube_auth.connection_state() == "needs_reconnect",
            "subscribers": ch.get("subscribers"),
            "views": ch.get("views"),
            "videos": ch.get("videos"),
            "likes": sum(_i(v.get("likes")) for v in vids),
            "comments": sum(_i(v.get("comments")) for v in vids),
            "top": sorted(vids, key=lambda v: _i(v.get("views")), reverse=True)[:5],
            "last_pull": youtube_stats.last_pull_at(),
            "subs_trend": _trend(_chan_series("subscribers")),
            "views_trend": _trend(_chan_series("views")),
        }
    except Exception:
        pass

    return {
        "goal": goal, "youtube": yt, "leads": leads, "sections": _SCOREBOARD_SECTIONS,
        "values": values, "trends": trends, "customers_trend": customers_trend,
    }


@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request, msg: str = "", err: str = ""):
    sb = _scoreboard()
    try:
        import ga4_stats
        sb["ga4_configured"] = ga4_stats.is_configured()
        sb["ga4"] = ga4_stats.last_pull() or {}
        sb["ga4_last_pull"] = sb["ga4"].get("pulled_at")
    except Exception:
        sb["ga4_configured"] = False
        sb["ga4"] = {}
        sb["ga4_last_pull"] = None
    try:
        import tiktok_stats
        sb["tiktok_configured"] = tiktok_stats.is_configured()
        sb["tiktok_last_pull"] = tiktok_stats.last_pull_at()
    except Exception:
        sb["tiktok_configured"] = False
        sb["tiktok_last_pull"] = None
    return templates.TemplateResponse(request, "performance.html", {"active": "performance", "sb": sb, "msg": msg, "err": err})


@app.post("/performance/import-csv")
async def performance_import_csv(file: UploadFile = File(...)):
    import analytics
    from urllib.parse import urlencode
    safe_name = Path(file.filename or "import.csv").name or "import.csv"
    target = STATE_DIR / "manual-metrics" / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{safe_name}.uploading")
    try:
        temp.write_bytes(await file.read())
    finally:
        await file.close()
    try:
        analytics.import_csv(temp)
        temp.replace(target)
    except Exception as exc:  # noqa: BLE001 - bad operator CSVs should not 500
        temp.unlink(missing_ok=True)
        return RedirectResponse("/performance?" + urlencode({"err": f"CSV import failed: {exc}"}), status_code=303)
    return RedirectResponse("/performance", status_code=303)


@app.post("/performance/metric")
def performance_set_metric(metric: str = Form(...), value: str = Form("")):
    """Quick manual update of one scoreboard metric (records to performance.jsonl)."""
    import analytics
    if (value or "").strip():
        analytics.record_metric(metric.strip(), value.strip(), source="manual", status="measured")
    return RedirectResponse("/performance", status_code=303)


@app.post("/performance/ga4-refresh")
def performance_ga4_refresh():
    """Pull the latest GA4 metrics into the scoreboard (records to performance.jsonl)."""
    from urllib.parse import urlencode
    try:
        import ga4_stats
        s = ga4_stats.pull()
        return RedirectResponse(
            "/performance?" + urlencode({"msg": f"GA4: {s['visitors']} visitors, {s['pageviews']} page views (last {s['days']}d)"}),
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/performance?" + urlencode({"err": f"GA4 pull failed: {exc}"}), status_code=303)


@app.post("/performance/tiktok-refresh")
def performance_tiktok_refresh():
    """Pull the latest TikTok profile stats into the scoreboard (records to performance.jsonl)."""
    from urllib.parse import urlencode
    try:
        import tiktok_stats
        s = tiktok_stats.pull()
        msg = f"TikTok: {s['followers']} followers"
        if s.get("likes") is not None:
            msg += f", {s['likes']} total likes"
        return RedirectResponse(
            "/performance?" + urlencode({"msg": msg}),
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/performance?" + urlencode({"err": f"TikTok pull failed: {exc}"}), status_code=303)


@app.get("/youtube", response_class=HTMLResponse)
def youtube_stats_page(request: Request, msg: str = "", err: str = ""):
    channel: dict = {}
    videos: list = []
    last_pull = None
    editor_brief = ""
    try:
        status = youtube_auth.get_status()
    except Exception as exc:  # noqa: BLE001 - auth status must never 500 the page
        status = youtube_auth.YTAuthStatus(connected=False, has_client_secret=False, error=str(exc))
    connected = status.connected
    try:
        import youtube_stats
        channel = youtube_stats.channel_summary()
        videos = youtube_stats.video_summary()
        last_pull = youtube_stats.last_pull_at()
    except Exception as exc:  # noqa: BLE001 - missing libs / read error: show it, don't crash
        err = err or f"Could not load YouTube stats: {exc}"
    brief_path = STATE_DIR / "editorial-brief.md"
    if brief_path.exists():
        editor_brief = brief_path.read_text(encoding="utf-8")
    return templates.TemplateResponse(
        request,
        "youtube_stats.html",
        {
            "active": "youtube",
            "connected": connected,
            "status": status,
            "redirect_uri": _youtube_redirect_uri(request),
            "client_secret_path": str(youtube_auth.CLIENT_SECRET_PATH),
            "channel": channel,
            "videos": videos,
            "last_pull": last_pull,
            "editor_brief": editor_brief,
            "msg": msg,
            "err": err,
        },
    )


@app.post("/youtube/refresh")
def youtube_stats_refresh():
    from urllib.parse import urlencode
    try:
        import youtube_stats
        result = youtube_stats.pull()
        msg = f"Pulled {result['video_count']} videos ({result['tracked_count']} tracked) at {result['pulled_at']}."
        try:
            import refresh_editor
            editor_code = refresh_editor.main()
            if editor_code:
                msg += f" Editor refresh exited with code {editor_code}."
            else:
                msg += " Editor refreshed."
        except Exception as exc:  # noqa: BLE001 - stats should still land even if guidance needs attention
            msg += f" Editor refresh failed: {exc}"
        try:
            today_path = ops_tracker.run()
            msg += f" Today regenerated ({today_path.name})."
        except Exception as exc:  # noqa: BLE001 - keep stats refresh useful even if today's brief has trouble
            msg += f" Today refresh failed: {exc}"
        return RedirectResponse("/youtube?" + urlencode({"msg": msg}), status_code=303)
    except Exception as exc:  # noqa: BLE001 - keep the operator on the page with the reason
        return RedirectResponse("/youtube?" + urlencode({"err": str(exc)}), status_code=303)


@app.post("/youtube/backfill-links")
def youtube_backfill_links(apply: str = Form("")):
    """Backfill the tracked /checklist link into already-published video descriptions.

    Preview (no apply) is safe read-only. Apply calls videos().update, which needs
    the youtube.force-ssl scope — if it's missing, the error tells the operator to
    reconnect."""
    from urllib.parse import urlencode
    do_apply = apply.strip().lower() in ("1", "true", "yes", "on")
    try:
        import youtube_backfill
        s = youtube_backfill.backfill(apply=do_apply)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/youtube?" + urlencode({"err": f"Backfill failed: {exc}"}), status_code=303)
    if not do_apply:
        if not s["changed"]:
            return RedirectResponse("/youtube?" + urlencode({"msg": "Every video already carries the tracked /checklist link — nothing to backfill."}), status_code=303)
        return RedirectResponse("/youtube?" + urlencode({"msg": f"Preview: {s['changed']} of {s['total']} videos would get the tracked /checklist link. Click 'Apply backfill' to write them."}), status_code=303)
    if s["errors"]:
        hint = " — reconnect YouTube to grant edit access." if "insufficient" in s["errors"][0].lower() or "scope" in s["errors"][0].lower() or "403" in s["errors"][0] else ""
        return RedirectResponse("/youtube?" + urlencode({"err": f"Applied {s['applied']}/{s['changed']}; first error: {s['errors'][0]}{hint}"}), status_code=303)
    return RedirectResponse("/youtube?" + urlencode({"msg": f"Backfilled {s['applied']} video descriptions with the tracked /checklist link."}), status_code=303)


@app.post("/email-queue/action")
def email_queue_action(queue_key: str = Form(""), action: str = Form(...), email: str = Form(""), reason: str = Form(""), manual_url: str = Form(""), queue_filter: str = Form("queued")):
    import email_nurture
    from urllib.parse import urlencode
    target = "/email-queue?" + urlencode({"filter": queue_filter or "queued"})
    try:
        if action == "suppress":
            email_nurture.suppress_lead(email, reason or "manual suppression")
            msg = "Lead suppressed."
        else:
            email_nurture.update_queue_item(queue_key, action, reason=reason, manual_url=manual_url)
            msg = "Email queue action saved."
    except Exception as exc:  # noqa: BLE001 - keep operator on the queue with the failure visible
        return RedirectResponse(target + "&" + urlencode({"err": str(exc)}), status_code=303)
    return RedirectResponse(target + "&" + urlencode({"msg": msg}), status_code=303)


@app.post("/email-queue/archive-fixtures")
def email_queue_archive_fixtures(confirm: str = Form(""), reason: str = Form("fixture/example.com queue cleanup")):
    import email_nurture
    from urllib.parse import urlencode
    target = "/email-queue?" + urlencode({"filter": "fixture"})
    if confirm != "ARCHIVE_FIXTURE_EMAILS":
        return RedirectResponse(target + "&" + urlencode({"err": "Type ARCHIVE_FIXTURE_EMAILS to archive fixture email rows."}), status_code=303)
    try:
        result = email_nurture.archive_fixture_items(apply=True, reason=reason or "fixture/example.com queue cleanup")
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(target + "&" + urlencode({"err": str(exc)}), status_code=303)
    return RedirectResponse(target + "&" + urlencode({"msg": f"Archived {result['archived']} fixture email row(s)."}), status_code=303)


@app.post("/email-queue/capture-bulk")
def email_queue_capture_bulk(emails: str = Form(""), campaign_id: str = Form("bulk-capture"), opt_in_confirm: str = Form("")):
    import leads as leads_mod
    from urllib.parse import urlencode
    target = "/email-queue?" + urlencode({"filter": "real"})
    if opt_in_confirm != "OPTED_IN":
        return RedirectResponse(target + "&" + urlencode({"err": "Type OPTED_IN before adding bulk lead addresses."}), status_code=303)
    tokens = [token.strip().strip(",;") for token in re.split(r"[\s,;]+", emails or "") if token.strip().strip(",;")]
    created = 0
    updated = 0
    errors: list[str] = []
    for email in dict.fromkeys(tokens):
        try:
            _lead, was_created = leads_mod.capture_lead(
                email=email,
                campaign_id=campaign_id or "bulk-capture",
                consent=True,
                utm_source="email_queue",
                utm_medium="bulk_capture",
                utm_content="manual_paste",
                landing_page="email_queue_bulk_capture",
            )
        except ValueError as exc:
            errors.append(f"{email}: {exc}")
            continue
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    if errors:
        return RedirectResponse(target + "&" + urlencode({"err": f"Captured {created} new, updated {updated}. Errors: {'; '.join(errors[:3])}"}), status_code=303)
    return RedirectResponse(target + "&" + urlencode({"msg": f"Captured {created} new, updated {updated} opted-in lead(s)."}), status_code=303)


@app.post("/email-queue/blast/queue")
def email_queue_queue_blast(subject: str = Form(""), body: str = Form(""), campaign_id: str = Form("bulk-blast"), blast_id: str = Form(""), confirm: str = Form(""), include_fixtures: str = Form("")):
    import email_nurture
    from urllib.parse import urlencode
    target = "/email-queue?" + urlencode({"filter": "queued"})
    if confirm != email_nurture.BLAST_QUEUE_CONFIRM:
        return RedirectResponse(target + "&" + urlencode({"err": f"Type {email_nurture.BLAST_QUEUE_CONFIRM} to queue a bulk blast."}), status_code=303)
    try:
        result = email_nurture.queue_bulk_blast(
            subject=subject,
            body=body,
            campaign_id=campaign_id,
            blast_id=blast_id,
            include_fixtures=include_fixtures == "1",
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(target + "&" + urlencode({"err": str(exc)}), status_code=303)
    return RedirectResponse(target + "&" + urlencode({"msg": f"Queued {result['created']} blast email(s) for {result['recipient_count']} eligible recipient(s)."}), status_code=303)


@app.post("/email-queue/blast/send")
def email_queue_send_blast(campaign_id: str = Form(""), blast_id: str = Form(""), confirm: str = Form("")):
    import email_nurture
    from urllib.parse import urlencode
    target = "/email-queue?" + urlencode({"filter": "queued"})
    try:
        result = email_nurture.send_bulk_blast(campaign_id=campaign_id, blast_id=blast_id, confirm=confirm, mode="live")
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(target + "&" + urlencode({"err": str(exc)}), status_code=303)
    if result.get("blockers"):
        return RedirectResponse(target + "&" + urlencode({"err": f"Bulk send blocked: {'; '.join(result['blockers'][:3])}"}), status_code=303)
    return RedirectResponse(target + "&" + urlencode({"msg": f"Sent {result['sent']} blast email(s)."}), status_code=303)


@app.get("/unsubscribe/{email}", response_class=HTMLResponse)
def unsubscribe_local(request: Request, email: str):
    import email_nurture
    email_nurture.suppress_lead(email, "local unsubscribe route")
    return HTMLResponse("<h1>Unsubscribed locally</h1><p>This address has been suppressed in the local Shadow Edge ops system.</p>")


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def main():
    host = os.environ.get("CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("CONSOLE_PORT", str(DEFAULT_CONSOLE_PORT)))
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(f"http://{host}:{port}{DEFAULT_HOME_PATH}")
        except Exception:
            pass
    # Orphaned jobs (worker threads don't survive a restart) -> mark failed so
    # they stop spinning forever and can be dismissed from the activity list.
    try:
        stale = jobs_mod.reconcile_stale_jobs()
        if stale:
            print(f"[Shadow Edge] Reconciled {stale} interrupted job(s)")
    except Exception:
        pass
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

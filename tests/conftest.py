import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# Modules that write to state/*.jsonl, and the path attributes to redirect.
# "STATE_DIR" maps to the tmp dir itself; every other attr maps to tmp/<filename>.
# Keeping this exhaustive means running the suite can never pollute the real
# state files (leads, email queue, lead events, publish queue, listening, ...).
_STATE_WRITERS = {
    "leads": {"STATE_DIR": None, "LEADS_PATH": "leads.jsonl", "EMAIL_EVENTS_PATH": "email-events.jsonl"},
    "email_nurture": {"STATE_DIR": None, "QUEUE_PATH": "email-queue.jsonl", "EVENTS_PATH": "email-events.jsonl"},
    "tracking": {"STATE_DIR": None, "EVENTS_PATH": "events.jsonl"},
    "distribution_queue": {"STATE_DIR": None, "QUEUE_PATH": "publish-queue.jsonl", "LOG_PATH": "publish-log.jsonl"},
    "listening": {
        "STATE_DIR": None,
        "OPPORTUNITIES_PATH": "listening-opportunities.jsonl",
        "ENGAGEMENT_QUEUE_PATH": "engagement-queue.jsonl",
        "ENGAGEMENT_LOG_PATH": "engagement-log.jsonl",
    },
}


@pytest.fixture(autouse=True)
def _isolate_state_writes(tmp_path, monkeypatch):
    """Redirect every module that writes to state/*.jsonl to a per-test tmp dir,
    so running the suite never pollutes the real state files. This is the safety
    net the lead-capture path (POST /api/leads -> leads.capture_lead +
    email_nurture.queue_for_lead + tracking.append_event) was missing.

    Runs before each test. A test that needs to assert on these files can still
    monkeypatch them to its own tmp dir — that setattr runs after this one and
    wins; both are undone at teardown.
    """
    state = tmp_path / "_state"
    state.mkdir(parents=True, exist_ok=True)
    for mod_name, attrs in _STATE_WRITERS.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, filename in attrs.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, state if filename is None else state / filename)

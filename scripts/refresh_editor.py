"""Re-derive the editor's editorial brief + guidance from the latest YouTube stats.

Run right after `youtube_stats.py pull` so the guidance that steers the next
video always reflects the newest numbers. Also runs inside the team loop via
the Editor agent; this is the standalone entry the nightly task uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_io import configure_utf8_stdio  # noqa: E402
from agents.editor_agent import EditorAgent  # noqa: E402


def main() -> int:
    configure_utf8_stdio()
    result = EditorAgent().run({"date": "", "dry_run": True})
    print(f"editor: {result.status} | lead={result.outputs.get('lead_theme')} "
          f"| confidence={result.outputs.get('confidence')}")
    for blocker in result.blockers:
        print(f"  blocker: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

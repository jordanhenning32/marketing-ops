from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

from hermes_store import CONFIG_DIR, write_text

DEFAULT_SOURCE = Path("C:/Users/jorda/OneDrive/Documents/Daily worksheets/")
DRAFT_PATH = CONFIG_DIR / "brand-voice-draft.md"
SENSITIVE_PATTERNS = [r"\$\s?\d[\d,]*(?:\.\d+)?", r"\b\d+\.\d{2}\b", r"account\s*#?\s*\d+"]


def _read_docx(path: Path) -> str:
    try:
        import docx  # type: ignore
    except Exception:
        return ""
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def source_files(source_dir: Path) -> list[Path]:
    if not source_dir.exists():
        return []
    return sorted([p for p in source_dir.rglob("*") if p.suffix.lower() in {".txt", ".md", ".docx"}])[:200]


def _read_source(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return _read_docx(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def _redact(text: str) -> str:
    for pattern in SENSITIVE_PATTERNS:
        text = re.sub(pattern, "[redacted]", text)
    return text


def _tokens(text: str) -> list[str]:
    return re.findall(r"\b[A-Z]{2,}|\b[A-Za-z][A-Za-z-]{3,}\b", text)


def extract_brand_voice(source_dir: str | Path = DEFAULT_SOURCE, output_path: str | Path = DRAFT_PATH) -> dict:
    source_dir = Path(source_dir)
    output_path = Path(output_path)
    files = source_files(source_dir)
    if not files:
        return {"ok": False, "blocker": f"source directory missing or no supported files: {source_dir}", "draft_path": str(output_path), "sources": 0}
    chunks = []
    for path in files:
        text = _redact(_read_source(path))
        if text.strip():
            chunks.append(text[:6000])
    corpus = "\n".join(chunks)
    vocab = [w for w, _ in Counter(_tokens(corpus)).most_common(30)]
    short_lines = [ln.strip() for ln in corpus.splitlines() if 8 <= len(ln.strip()) <= 140]
    examples = short_lines[:8]
    body = f"""# Brand Voice Draft

Status: DRAFT ONLY. Do not replace `config/brand-voice.md` until Jordan reviews and approves.

Sources scanned: {len(files)} files from `{source_dir}`.

## Observed vocabulary
{chr(10).join(f'- {w}' for w in vocab[:20])}

## Sentence rhythm
- Short, declarative notes.
- Conditional planning language: if/above/below/inside/failed break.
- Market-state fragments before explanation.
- Process first; no hype.

## Hook patterns
- "Above X... Below Y... Between..."
- "The rule has to exist before the click."
- "If the plan is unclear, reduce action."

## Phrases to prefer
- balance rules
- drawdown discipline
- risk controls before order entry
- platform-enforced guardrail
- wait for confirmation

## Phrases to avoid
- game-changer
- unlock your edge
- crush it
- guaranteed
- secret strategy

## Safe Shadow Edge marketing copy examples
- If your daily loss rule only shows up in the recap, it is too late. Move the control closer to order entry.
- Above the rule, trade the plan. Below the rule, stop the platform from accepting another impulse click.
- Bracket Boss and Drawdown Guardian are discipline tools. Not signals. Not profit promises. Guardrails.

## Compliance cautions for personal trading references
- Do not include private account details, P&L, exact trades, or sensitive notes.
- Do not convert worksheet observations into testimonials or claimed outcomes.
- Keep examples framed as process and risk-management discipline.

## Redacted source examples
{chr(10).join(f'- {line}' for line in examples)}
"""
    write_text(output_path, body)
    return {"ok": True, "draft_path": str(output_path), "sources": len(files), "vocabulary": vocab[:20]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output", default=str(DRAFT_PATH))
    args = parser.parse_args(argv)
    result = extract_brand_voice(args.source, args.output)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

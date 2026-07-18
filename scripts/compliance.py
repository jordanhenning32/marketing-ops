"""Local compliance gate for Shadow Edge campaign assets."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

try:
    import yaml
except Exception:  # pragma: no cover - yaml is optional at runtime
    yaml = None

from hermes_store import CONFIG_DIR, read_text, write_text

DEFAULT_BANNED = [
    "guaranteed profit", "guaranteed profits", "guaranteed payout",
    "pass your prop firm", "make you profitable", "never lose", "risk-free",
    "get rich", "secret strategy", "limited time only", "cold dm",
]
DEFAULT_PATTERNS = [
    r"\$\d+[\d,]*(?:\.\d+)?\s*(?:profit|payout|day|week|month)",
    r"\b\d+%\s*(?:win rate|return|roi|profit)\b",
    r"\b(?:double|triple) your account\b",
]

@dataclass
class ComplianceIssue:
    file: str
    severity: str
    rule: str
    match: str
    reason: str

@dataclass
class ComplianceResult:
    status: str
    risk_level: str
    issues: list[ComplianceIssue]
    checked_files: list[str]

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def reasons(self) -> list[str]:
        return [issue.reason for issue in self.issues]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "risk_level": self.risk_level,
            "issues": [asdict(i) for i in self.issues],
            "checked_files": self.checked_files,
        }


def _load_rules() -> dict:
    path = CONFIG_DIR / "compliance-rules.yaml"
    if yaml and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    data.setdefault("banned_phrases", DEFAULT_BANNED)
    data.setdefault("profit_claim_patterns", DEFAULT_PATTERNS)
    data.setdefault("fake_story_markers", ["my funded account", "when I passed", "my payout"])
    return data


def _iter_asset_files(campaign_dir: Path) -> Iterable[Path]:
    for path in sorted(campaign_dir.glob("*.md")):
        if path.name.startswith("08-compliance"):
            continue
        yield path


def check_text(text: str, source: str = "inline") -> ComplianceResult:
    rules = _load_rules()
    lowered = text.lower()
    issues: list[ComplianceIssue] = []
    for phrase in rules.get("banned_phrases", []):
        if phrase.lower() in lowered:
            issues.append(ComplianceIssue(source, "block", "banned_phrase", phrase, "banned phrase or trust-damaging claim/language."))
    for marker in rules.get("fake_story_markers", []):
        if marker.lower() in lowered:
            issues.append(ComplianceIssue(source, "revise", "personal_story", marker, "Personal trading story requires documented source material."))
    for pattern in rules.get("profit_claim_patterns", []):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            issues.append(ComplianceIssue(source, "block", "profit_claim", match.group(0), "Profit, ROI, payout, or guaranteed outcome claim is unsupported."))
    status = "pass" if not issues else ("block" if any(i.severity == "block" for i in issues) else "revise")
    risk = "low" if status == "pass" else ("high" if status == "block" else "medium")
    return ComplianceResult(status, risk, issues, [source])


def check_asset(path: Path) -> ComplianceResult:
    path = Path(path)
    return check_text(read_text(path), path.name)


def check_campaign_folder(campaign_dir: Path) -> ComplianceResult:
    campaign_dir = Path(campaign_dir)
    issues: list[ComplianceIssue] = []
    checked: list[str] = []
    for path in _iter_asset_files(campaign_dir):
        result = check_text(read_text(path), path.name)
        checked.extend(result.checked_files)
        issues.extend(result.issues)
    status = "pass" if not issues else ("block" if any(i.severity == "block" for i in issues) else "revise")
    risk = "low" if status == "pass" else ("high" if status == "block" else "medium")
    result = ComplianceResult(status, risk, issues, checked)
    write_campaign_result(campaign_dir, result)
    return result


def write_campaign_result(campaign_dir: Path, result: ComplianceResult) -> Path:
    lines = [
        "# Compliance Check", "", f"- Status: {result.status}",
        f"- Risk level: {result.risk_level}", f"- Checked files: {len(result.checked_files)}",
        "- Guardrails source: config/guardrails.md + config/compliance-rules.yaml",
        "- Required disclaimer: Tools and educational content only. Not financial advice. Trading futures involves risk.",
        "", "## Issues",
    ]
    if not result.issues:
        lines.append("- None. Assets are cleared for manual review/queueing, subject to platform credentials and final human publishing authority.")
    else:
        for issue in result.issues:
            lines.append(f"- {issue.severity.upper()} `{issue.file}` - {issue.rule}: {issue.match} - {issue.reason}")
    lines += ["", "```json", json.dumps(result.to_dict(), indent=2), "```", ""]
    out = campaign_dir / "08-compliance-check.md"
    write_text(out, "\n".join(lines))
    return out


def assert_distribution_allowed(campaign_dir: Path) -> ComplianceResult:
    result = check_campaign_folder(campaign_dir)
    if not result.passed:
        raise SystemExit(f"Compliance status is {result.status}; distribution refused for {campaign_dir}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Shadow Edge compliance checks")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    p = Path(args.path)
    result = check_campaign_folder(p) if p.is_dir() else check_text(read_text(p), p.name)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Compliance: {result.status} risk={result.risk_level} issues={len(result.issues)}")
    return 0 if result.status == "pass" else 2

if __name__ == "__main__":
    raise SystemExit(main())

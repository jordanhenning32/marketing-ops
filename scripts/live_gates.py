"""Centralized live-mode safety gates for Shadow Edge Hermes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from hermes_store import CONFIG_DIR


def load_hermes_config() -> dict[str, Any]:
    path = CONFIG_DIR / "hermes.yaml"
    if yaml and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {}


def check_publish_gate(*, mode: str, platform: str, platform_config: dict[str, Any], queue_item: dict[str, Any]) -> dict[str, Any]:
    cfg = load_hermes_config()
    blockers: list[str] = []
    if mode != "live":
        blockers.append("mode is not live; dry-run never publishes")
    if cfg.get("live_publish_enabled") is not True:
        blockers.append("config/hermes.yaml live_publish_enabled is false")
    if platform_config.get("api_enabled") is not True:
        blockers.append(f"{platform} api_enabled is false")
    credential_env = platform_config.get("credential_env") or ""
    if credential_env and not os.getenv(str(credential_env)):
        blockers.append(f"missing {credential_env}")
    if queue_item.get("compliance_status") != "pass":
        blockers.append("queue item compliance_status is not pass")
    if queue_item.get("approval_status") != "approved":
        blockers.append("queue item is not approved")
    return {"allowed": not blockers, "blockers": blockers, "mode": mode, "platform": platform}


def check_email_gate(*, mode: str, email_config: dict[str, Any], lead: dict[str, Any]) -> dict[str, Any]:
    cfg = load_hermes_config()
    blockers: list[str] = []
    if mode != "live":
        blockers.append("mode is not live; dry-run never sends email")
    if cfg.get("live_email_enabled") is not True:
        blockers.append("config/hermes.yaml live_email_enabled is false")
    if email_config.get("send_enabled") is not True:
        blockers.append("config/email.yaml send_enabled is false")
    if not email_config.get("provider") or email_config.get("provider") == "none":
        blockers.append("email provider is not configured")
    credential_env = email_config.get("credential_env") or "EMAIL_PROVIDER_API_KEY"
    if credential_env and not os.getenv(str(credential_env)):
        blockers.append(f"missing {credential_env}")
    if not lead.get("consent"):
        blockers.append("lead consent is false")
    if lead.get("suppressed"):
        blockers.append("lead is suppressed")
    return {"allowed": not blockers, "blockers": blockers, "mode": mode}

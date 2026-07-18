"""Publisher adapter status registry.

Real API publishing remains disabled unless official docs, permissions, credentials,
queue approval, compliance, and live gates all pass.
"""
from __future__ import annotations

import os
from typing import Any

SUPPORTED_API_PLATFORMS = set()  # kept empty until official docs/permissions are verified
DEFAULT_PLATFORMS = ["x", "stocktwits", "tiktok", "linkedin", "youtube", "email"]


def adapter_status(platform: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    credential_env = config.get("credential_env") or ""
    api_enabled = bool(config.get("api_enabled"))
    credential_present = bool(os.getenv(str(credential_env))) if credential_env else False
    supported = platform in SUPPORTED_API_PLATFORMS
    fallback = config.get("publish_mode") or ("kit" if platform in {"youtube", "tiktok", "email"} else "manual")
    blockers: list[str] = []
    if not supported:
        blockers.append("API publishing not supported by this adapter until official docs/permissions are verified")
    if not api_enabled:
        blockers.append("api_enabled is false")
    if credential_env and not credential_present:
        blockers.append(f"missing {credential_env}")
    return {
        "platform": platform,
        "supported": supported,
        "api_enabled": api_enabled,
        "credential_status": "present" if credential_present else ("missing" if credential_env else "not_required"),
        "blockers": blockers,
        "docs_verified": False,
        "safe_fallback_mode": fallback,
    }


def status_all(platform_configs: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    platform_configs = platform_configs or {}
    platforms = sorted(set(DEFAULT_PLATFORMS) | set(platform_configs))
    return {platform: adapter_status(platform, platform_configs.get(platform, {})) for platform in platforms}

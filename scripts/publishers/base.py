from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any

@dataclass
class PublishResult:
    ok: bool
    platform: str
    mode: str
    message: str
    url: str = ""

class Publisher:
    platform = "base"
    credential_env = ""
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
    def validate_config(self) -> tuple[bool, list[str]]:
        if self.config.get("api_enabled") is True and self.credential_env and not os.getenv(self.credential_env):
            return False, [f"missing {self.credential_env}"]
        return True, []
    def dry_run(self, asset: dict[str, Any]) -> PublishResult:
        return PublishResult(True, self.platform, "dry_run", "no external publish attempted")
    def publish(self, asset: dict[str, Any]) -> PublishResult:
        ok, blockers = self.validate_config()
        if not ok:
            return PublishResult(False, self.platform, "api", "; ".join(blockers))
        return PublishResult(False, self.platform, "api", "API publishing is not enabled by this adapter")
    def status(self) -> dict[str, Any]:
        ok, blockers = self.validate_config()
        return {"platform": self.platform, "ok": ok, "blockers": blockers, "api_enabled": bool(self.config.get("api_enabled"))}

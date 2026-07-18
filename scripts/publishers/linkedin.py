from __future__ import annotations
from .base import Publisher, PublishResult

class LinkedinPublisher(Publisher):
    platform = "linkedin"
    credential_env = "LINKEDIN_ACCESS_TOKEN"
    def publish(self, asset: dict) -> PublishResult:
        ok, blockers = self.validate_config()
        if not self.config.get("api_enabled"):
            return PublishResult(True, self.platform, asset.get("publish_mode", "manual"), "API disabled; manual/kit workflow only")
        if not ok:
            return PublishResult(False, self.platform, "api", "; ".join(blockers))
        return PublishResult(False, self.platform, "api", "API publishing requires official-doc verification before enabling")

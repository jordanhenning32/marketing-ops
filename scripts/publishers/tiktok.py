from __future__ import annotations
from .base import Publisher, PublishResult

class TiktokPublisher(Publisher):
    platform = "tiktok"
    credential_env = "TIKTOK_CLIENT_KEY"
    def publish(self, asset: dict) -> PublishResult:
        ok, blockers = self.validate_config()
        if not self.config.get("api_enabled"):
            return PublishResult(True, self.platform, asset.get("publish_mode", "kit"), "API disabled; manual/kit workflow only")
        if not ok:
            return PublishResult(False, self.platform, "api", "; ".join(blockers))
        return PublishResult(False, self.platform, "api", "API publishing requires official-doc verification before enabling")

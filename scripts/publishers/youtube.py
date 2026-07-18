from __future__ import annotations
from .base import Publisher, PublishResult

class YouTubePublisher(Publisher):
    platform = "youtube"
    credential_env = "YOUTUBE_CLIENT_SECRETS_FILE"
    def publish(self, asset: dict) -> PublishResult:
        ok, blockers = self.validate_config()
        if not self.config.get("api_enabled"):
            return PublishResult(True, self.platform, "kit", "YouTube API disabled; upload kit/manual workflow only")
        if not ok:
            return PublishResult(False, self.platform, "api", "; ".join(blockers))
        return PublishResult(False, self.platform, "api", "API upload must be routed through existing youtube_upload.py after OAuth review")

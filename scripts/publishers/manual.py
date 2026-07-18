from __future__ import annotations
from .base import Publisher, PublishResult

class ManualPublisher(Publisher):
    platform = "manual"
    def publish(self, asset: dict) -> PublishResult:
        return PublishResult(True, asset.get("platform", self.platform), "manual", "manual-ready kit queued; human review required")

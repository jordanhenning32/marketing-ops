"""Editor agent for the YouTube performance feedback loop.

The dry-run "marketing team" agents were removed; only the editor agent (which
turns live YouTube stats into the next-video guidance) remains.
"""
from .base import AgentResult, MarketingAgent
from .editor_agent import EditorAgent

__all__ = ["AgentResult", "MarketingAgent", "EditorAgent"]

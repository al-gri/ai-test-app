"""Explicit A2A 1.0 JSON-RPC profile and pinned Agent Card discovery."""
from .cards import AgentCard
from .client import A2AClient
from .discovery import AgentRegistry, Requirements, Selection

__all__ = ["AgentCard", "A2AClient", "AgentRegistry", "Requirements", "Selection"]

"""
pact-bridge
───────────
The spinal cord connecting pact (intent translation) and pact-ax (agent collaboration).

    from pact_bridge import PACTBridge, BridgeConfig
    from pact_bridge.agent_registry import AgentRegistry, AgentCard
"""

from pact_bridge.config import BridgeConfig
from pact_bridge.core import PACTBridge
from pact_bridge.agent_registry import AgentRegistry, AgentCard
from pact_bridge.intent_router import IncomingMessage, RoutingDecision, RoutingOutcome
from pact_bridge.response_adapter import BridgeResponse, BridgeStatus, ResponseAdapter
from pact_bridge.session_store import Session, SessionStore

__version__ = "0.1.0"

__all__ = [
    "PACTBridge",
    "BridgeConfig",
    "AgentRegistry",
    "AgentCard",
    "IncomingMessage",
    "RoutingDecision",
    "RoutingOutcome",
    "BridgeResponse",
    "BridgeStatus",
    "ResponseAdapter",
    "Session",
    "SessionStore",
]

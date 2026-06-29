"""
pact_bridge/response_adapter.py
─────────────────────────────────
ResponseAdapter — translates pact-ax agent outcomes back to the
requesting platform's expected message format.

The bridge is symmetric:
  inbound:   platform format  →  PACT intent  →  pact-ax
  outbound:  pact-ax result   →  PACT envelope  →  platform format

Supported platform adapters
────────────────────────────
  "dialogflow"  →  Dialogflow v2 WebhookResponse shape
  "rasa"        →  Rasa NLU/Core response shape
  "pact"        →  Raw PACT envelope (default / pass-through)
  "custom"      →  Minimal JSON envelope

New platforms: subclass PlatformAdapter and register it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Bridge response envelope
# ──────────────────────────────────────────────────────────────────────────────

class BridgeStatus(str, Enum):
    SUCCESS          = "success"
    NO_AGENT         = "no_agent"
    UNTRUSTED        = "untrusted"
    CONSENSUS_FAIL   = "consensus_fail"
    RUPTURE_BLOCKED  = "rupture_blocked"
    ERROR            = "error"


@dataclass
class BridgeResponse:
    """
    Normalised response produced by the bridge before platform adaptation.
    ResponseAdapter converts this to the target platform's wire format.
    """

    status:           BridgeStatus
    intent:           str                         # translated PACT intent
    original_intent:  str                         # as received from platform
    agent_id:         Optional[str]               # who handled it
    result:           Dict[str, Any]              # agent's raw result
    session_id:       Optional[str]
    consensus_result: Optional[Any]   = None      # ConsensusResult if multi-agent
    warnings:         List[str]       = field(default_factory=list)
    metadata:         Dict[str, Any]  = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == BridgeStatus.SUCCESS


# ──────────────────────────────────────────────────────────────────────────────
# Platform adapter base
# ──────────────────────────────────────────────────────────────────────────────

class PlatformAdapter(ABC):
    """Convert a BridgeResponse to a platform-specific wire format."""

    @abstractmethod
    def adapt(self, response: BridgeResponse) -> Dict[str, Any]:
        ...

    def error_response(self, message: str) -> Dict[str, Any]:
        return {"error": message}


# ──────────────────────────────────────────────────────────────────────────────
# Built-in adapters
# ──────────────────────────────────────────────────────────────────────────────

class PACTAdapter(PlatformAdapter):
    """
    Raw PACT envelope — default output when no platform-specific adapter
    is configured.  Other PACT nodes speak this natively.
    """

    def adapt(self, response: BridgeResponse) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "pact_version":     "0.1.0",
            "status":           response.status.value,
            "intent":           response.intent,
            "original_intent":  response.original_intent,
            "agent_id":         response.agent_id,
            "session_id":       response.session_id,
            "result":           response.result,
        }
        if response.warnings:
            out["warnings"] = response.warnings
        if response.consensus_result:
            out["consensus"] = {
                "outcome":    response.consensus_result.outcome.value,
                "confidence": response.consensus_result.confidence_score,
                "decision":   response.consensus_result.winning_decision,
            }
        return out


class DialogflowAdapter(PlatformAdapter):
    """
    Dialogflow v2 WebhookResponse shape.
    https://cloud.google.com/dialogflow/docs/reference/rpc/google.cloud.dialogflow.v2#webhookresponse
    """

    def adapt(self, response: BridgeResponse) -> Dict[str, Any]:
        if not response.ok:
            return {
                "fulfillmentText": f"Unable to process: {response.status.value}",
                "outputContexts":  [],
            }

        text = (
            response.result.get("message")
            or response.result.get("text")
            or f"Handled intent: {response.intent}"
        )
        return {
            "fulfillmentText": text,
            "fulfillmentMessages": [
                {"text": {"text": [text]}}
            ],
            "outputContexts": [
                {
                    "name": f"projects/-/agent/sessions/{response.session_id}/contexts/pact-bridge",
                    "lifespanCount": 5,
                    "parameters": {
                        "pact_intent":  response.intent,
                        "pact_agent":   response.agent_id,
                        "pact_session": response.session_id,
                    },
                }
            ] if response.session_id else [],
            "source": "pact-bridge",
        }


class RasaAdapter(PlatformAdapter):
    """
    Rasa custom action response shape (actions server protocol).
    """

    def adapt(self, response: BridgeResponse) -> Dict[str, Any]:
        if not response.ok:
            return {
                "responses": [{"text": f"[pact-bridge] {response.status.value}"}],
                "events":    [],
            }

        text = (
            response.result.get("message")
            or response.result.get("text")
            or f"Intent {response.intent} handled by {response.agent_id}."
        )
        return {
            "responses": [{"text": text}],
            "events": [
                {
                    "event": "slot",
                    "name":  "pact_intent",
                    "value": response.intent,
                },
                {
                    "event": "slot",
                    "name":  "pact_agent",
                    "value": response.agent_id,
                },
            ],
        }


class CustomAdapter(PlatformAdapter):
    """
    Minimal JSON envelope for custom / generic callers.
    """

    def adapt(self, response: BridgeResponse) -> Dict[str, Any]:
        return {
            "ok":      response.ok,
            "status":  response.status.value,
            "intent":  response.intent,
            "agent":   response.agent_id,
            "result":  response.result,
            "session": response.session_id,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Adapter registry + ResponseAdapter facade
# ──────────────────────────────────────────────────────────────────────────────

_BUILT_IN_ADAPTERS: Dict[str, type] = {
    "pact":       PACTAdapter,
    "dialogflow": DialogflowAdapter,
    "rasa":       RasaAdapter,
    "custom":     CustomAdapter,
}


class ResponseAdapter:
    """
    Selects and applies the right PlatformAdapter for a given platform name.

    Usage
    ─────
        adapter = ResponseAdapter()
        adapter.register("my-platform", MyCustomAdapter())

        wire_response = adapter.adapt(bridge_response, platform="dialogflow")
    """

    def __init__(self) -> None:
        self._adapters: Dict[str, PlatformAdapter] = {
            name: cls() for name, cls in _BUILT_IN_ADAPTERS.items()
        }

    def register(self, platform: str, adapter: PlatformAdapter) -> None:
        """Register a custom adapter for *platform*."""
        self._adapters[platform.lower()] = adapter
        logger.info("Registered custom adapter for platform %r", platform)

    def adapt(self, response: BridgeResponse, platform: str) -> Dict[str, Any]:
        """
        Convert *response* to the wire format for *platform*.

        Falls back to PACTAdapter if the platform is unrecognised.
        """
        key     = platform.lower()
        adapter = self._adapters.get(key)
        if adapter is None:
            logger.warning(
                "No adapter for platform %r — falling back to PACT envelope.", platform
            )
            adapter = self._adapters["pact"]

        try:
            return adapter.adapt(response)
        except Exception as exc:
            logger.error("Adapter %r failed: %s", key, exc)
            return adapter.error_response(str(exc))

    def supported_platforms(self) -> List[str]:
        return sorted(self._adapters.keys())

    def __repr__(self) -> str:
        return f"ResponseAdapter(platforms={self.supported_platforms()})"

"""
pact_bridge/intent_router.py
──────────────────────────────
IntentRouter — translates platform intents via pact, then routes to
the right pact-ax agent via AgentRegistry + HumilityAwareCoordinator.

This is the first real integration between the two repos:
  platform intent  →  pact IntentRegistry.translate()  →  PACT intent
  PACT intent      →  AgentRegistry.candidates()        →  agent pool
  agent pool       →  HumilityAwareCoordinator          →  best agent

The result is a RoutingDecision that the bridge core uses to dispatch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pact_bridge.agent_registry import AgentCard, AgentRegistry
from pact_bridge.config import BridgeConfig

logger = logging.getLogger(__name__)


class RoutingOutcome(str, Enum):
    ROUTED        = "routed"          # single agent found and selected
    MULTI_AGENT   = "multi_agent"     # intent flagged for consensus
    NO_AGENT      = "no_agent"        # nobody can handle this intent
    UNTRUSTED     = "untrusted"       # sender trust below floor
    UNKNOWN_INTENT = "unknown_intent" # translation returned nothing useful


@dataclass
class IncomingMessage:
    """
    Normalised incoming message from any external platform.

    The bridge accepts this shape regardless of whether the origin is
    Dialogflow, Rasa, a custom agent, or another PACT node.
    """

    sender_id:  str                        # agent or user ID sending the message
    platform:   str                        # "dialogflow" | "rasa" | "custom" | ...
    intent:     str                        # raw intent from the source platform
    entities:   Dict[str, Any]  = field(default_factory=dict)
    text:       Optional[str]   = None     # original natural language text
    session_id: Optional[str]   = None     # tie to an existing session
    metadata:   Dict[str, Any]  = field(default_factory=dict)
    trust_score: float          = 1.0      # sender's trust (from TrustNetwork)


@dataclass
class RoutingDecision:
    """
    Output of IntentRouter — everything the bridge needs to dispatch.
    """

    outcome:            RoutingOutcome
    original_intent:    str
    translated_intent:  str
    primary_agent:      Optional[AgentCard]         # best single agent
    candidate_agents:   List[AgentCard]             # full pool for consensus
    sender_id:          str
    session_id:         Optional[str]
    entities:           Dict[str, Any]
    text:               Optional[str]
    reason:             str                         # human-readable explanation
    metadata:           Dict[str, Any]  = field(default_factory=dict)

    @property
    def routed(self) -> bool:
        return self.outcome == RoutingOutcome.ROUTED

    @property
    def needs_consensus(self) -> bool:
        return self.outcome == RoutingOutcome.MULTI_AGENT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome":           self.outcome.value,
            "original_intent":   self.original_intent,
            "translated_intent": self.translated_intent,
            "primary_agent":     self.primary_agent.agent_id if self.primary_agent else None,
            "candidate_count":   len(self.candidate_agents),
            "sender_id":         self.sender_id,
            "session_id":        self.session_id,
            "reason":            self.reason,
        }


class IntentRouter:
    """
    Translates and routes incoming intents.

    Parameters
    ----------
    registry : AgentRegistry
        Live agent registry.
    config : BridgeConfig
        Bridge-wide configuration.
    intent_registry : IntentRegistry, optional
        pact IntentRegistry instance.  When None, intents are passed
        through untranslated (useful when pact isn't installed).
    """

    def __init__(
        self,
        registry:        AgentRegistry,
        config:          BridgeConfig,
        intent_registry=None,     # pact_protocol.IntentRegistry
    ) -> None:
        self._agents   = registry
        self._config   = config
        self._pact_reg = intent_registry
        self._stats: Dict[str, int] = {o.value: 0 for o in RoutingOutcome}

    # ── main entry ────────────────────────────────────────────────────────────

    def route(self, message: IncomingMessage) -> RoutingDecision:
        """
        Full routing pipeline for one incoming message.

        Steps:
          1. Trust gate — reject low-trust senders
          2. Intent translation — pact IntentRegistry
          3. Candidate discovery — AgentRegistry
          4. Multi-agent check — is this intent in the consensus set?
          5. Single-agent selection — highest trust candidate
        """

        # ── 1. Trust gate ──────────────────────────────────────────────────
        if message.trust_score < self._config.trust_floor:
            return self._decide(
                RoutingOutcome.UNTRUSTED,
                message,
                message.intent,
                [],
                f"Sender trust {message.trust_score:.2f} < floor {self._config.trust_floor:.2f}",
            )

        # ── 2. Translate intent ────────────────────────────────────────────
        translated = self._translate(message.intent)
        logger.debug("Intent: %r → %r", message.intent, translated)

        if not translated:
            return self._decide(
                RoutingOutcome.UNKNOWN_INTENT,
                message,
                translated or message.intent,
                [],
                f"Intent {message.intent!r} could not be translated.",
            )

        # ── 3. Find candidates ────────────────────────────────────────────
        candidates = self._agents.candidates(
            translated,
            min_trust=self._config.trust_floor,
        )
        logger.debug(
            "Candidates for %r: %s",
            translated,
            [c.agent_id for c in candidates],
        )

        if not candidates:
            return self._decide(
                RoutingOutcome.NO_AGENT,
                message,
                translated,
                [],
                f"No registered agent handles intent {translated!r}.",
            )

        # ── 4. Multi-agent check ──────────────────────────────────────────
        if translated in self._config.multi_agent_intents or \
                message.intent in self._config.multi_agent_intents:
            return self._decide(
                RoutingOutcome.MULTI_AGENT,
                message,
                translated,
                candidates,
                f"Intent {translated!r} requires multi-agent consensus.",
            )

        # ── 5. Single best agent ──────────────────────────────────────────
        best = self._select_best(candidates, message)
        return self._decide(
            RoutingOutcome.ROUTED,
            message,
            translated,
            candidates,
            f"Routed to {best.agent_id!r} (trust={best.trust_score:.2f}).",
            primary=best,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _translate(self, intent: str) -> str:
        """
        Use pact IntentRegistry if available, else passthrough.
        Returns the translated intent string (may equal input if no mapping).
        """
        if self._pact_reg is not None:
            try:
                return self._pact_reg.translate(intent)
            except Exception as exc:
                logger.warning("IntentRegistry.translate failed: %s", exc)
        return intent

    def _select_best(
        self,
        candidates: List[AgentCard],
        message: IncomingMessage,
    ) -> AgentCard:
        """
        Pick the best single agent from candidates.

        Prefer same-platform agents (reduces serialisation overhead),
        then fall back to pure trust ranking.
        """
        same_platform = [c for c in candidates if c.platform == message.platform]
        pool = same_platform if same_platform else candidates
        return max(pool, key=lambda c: c.trust_score)

    def _decide(
        self,
        outcome:    RoutingOutcome,
        message:    IncomingMessage,
        translated: str,
        candidates: List[AgentCard],
        reason:     str,
        primary:    Optional[AgentCard] = None,
    ) -> RoutingDecision:
        self._stats[outcome.value] = self._stats.get(outcome.value, 0) + 1
        if outcome not in (RoutingOutcome.ROUTED, RoutingOutcome.MULTI_AGENT):
            logger.warning("Routing %s for %r: %s", outcome.value, message.intent, reason)
        else:
            logger.info("Routing %s for %r: %s", outcome.value, message.intent, reason)

        return RoutingDecision(
            outcome           = outcome,
            original_intent   = message.intent,
            translated_intent = translated,
            primary_agent     = primary,
            candidate_agents  = candidates,
            sender_id         = message.sender_id,
            session_id        = message.session_id,
            entities          = message.entities,
            text              = message.text,
            reason            = reason,
            metadata          = message.metadata,
        )

    # ── observability ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        """Return routing outcome counts."""
        return dict(self._stats)

    def __repr__(self) -> str:
        total = sum(self._stats.values())
        return (
            f"IntentRouter(agents={len(self._agents)}, "
            f"routed={total})"
        )

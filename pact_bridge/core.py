"""
pact_bridge/core.py
─────────────────────
PACTBridge — the spinal cord connecting pact and pact-ax.

Single entry point for all external platforms.  Internally orchestrates:

    pact    IntentRegistry          → intent translation
    pact-ax AgentRegistry           → agent discovery        (bridge)
    pact-ax HumilityAwareCoordinator→ epistemic routing      (pact-ax)
    pact-ax ConsensusProtocol       → multi-agent decisions  (pact-ax)
    pact-ax StateTransferManager    → session handoffs       (pact-ax)
    pact-ax CoordinationBus         → event spine            (pact-ax)
            ResponseAdapter         → platform formatting    (bridge)

Usage — minimal
───────────────
    from pact_bridge import PACTBridge, BridgeConfig
    from pact_bridge.agent_registry import AgentRegistry, AgentCard

    bridge = PACTBridge()

    bridge.registry.register(AgentCard(
        agent_id     = "billing-agent",
        platform     = "rasa",
        capabilities = {"billing.lookup", "billing.dispute"},
        trust_score  = 0.9,
    ))

    response = bridge.handle({
        "sender_id": "user-42",
        "platform":  "rasa",
        "intent":    "check_bill",
    })
    # → {"ok": True, "intent": "billing.lookup", "agent": "billing-agent", ...}

Usage — full with pact + pact-ax wired
───────────────────────────────────────
    from pact_protocol.intent_registry import IntentRegistry
    from pact_ax.coordination import TrustNetwork, CoordinationBus, AgentSession

    trust_net = TrustNetwork()
    bus       = CoordinationBus()

    bridge = PACTBridge(
        config          = BridgeConfig(
            registry_path       = "intent_registry.json",
            trust_floor         = 0.4,
            multi_agent_intents = {"approve_refund", "escalate"},
        ),
        intent_registry = IntentRegistry.load("intent_registry.json"),
        trust_network   = trust_net,
        bus             = bus,
    )
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pact_bridge.agent_registry import AgentCard, AgentRegistry
from pact_bridge.config import BridgeConfig
from pact_bridge.intent_router import IncomingMessage, IntentRouter, RoutingOutcome
from pact_bridge.response_adapter import BridgeResponse, BridgeStatus, ResponseAdapter
from pact_bridge.rlp_session import RLPSessionStore
from pact_bridge.session_store import SessionStore

logger = logging.getLogger(__name__)


class PACTBridge:
    """
    The spinal cord between pact (intent translation) and pact-ax (collaboration).

    Parameters
    ----------
    config : BridgeConfig, optional
        Bridge-wide configuration.  Defaults to sensible values.
    intent_registry : IntentRegistry, optional
        pact IntentRegistry instance for cross-platform intent translation.
        When None, intents are passed through untranslated.
    trust_network : TrustNetwork, optional
        pact-ax TrustNetwork.  Used to look up sender trust scores and
        receive trust update events from the bus.
    bus : CoordinationBus, optional
        pact-ax CoordinationBus.  When provided, all bridge events are
        published here for full observability.
    agent_handlers : dict, optional
        ``{agent_id: callable}`` — in-process agent handlers.
        ``handler(intent, entities, context) → dict``
        For agents running as HTTP services, set ``AgentCard.endpoint`` instead.
    """

    def __init__(
        self,
        config:           Optional[BridgeConfig]  = None,
        intent_registry=  None,                    # pact_protocol.IntentRegistry
        trust_network=    None,                    # pact_ax.coordination.TrustNetwork
        bus=              None,                    # pact_ax.coordination.CoordinationBus
        agent_handlers:   Optional[Dict[str, Any]] = None,
    ) -> None:
        self.config   = config or BridgeConfig()
        self.registry = AgentRegistry()
        self._router  = IntentRouter(
            registry        = self.registry,
            config          = self.config,
            intent_registry = intent_registry,
        )
        self._sessions  = SessionStore(
            ttl_minutes              = self.config.session_ttl_minutes,
            transfer_manager_factory = self._make_transfer_manager,
        )
        self.rlp_store      = RLPSessionStore(rupture_threshold=self.config.rupture_threshold)
        self._adapter       = ResponseAdapter()
        self._trust_net     = trust_network
        self._bus           = bus
        self._handlers:     Dict[str, Any] = agent_handlers or {}
        self._consensus_proto = self._build_consensus()
        self._metrics: Dict[str, int] = {
            "handled": 0, "success": 0, "errors": 0,
            "consensus_rounds": 0, "rupture_blocked": 0,
        }

        if bus is not None:
            self._wire_bus()

        logger.info("PACTBridge initialised. %s", repr(self))

    # ── main entry point ──────────────────────────────────────────────────────

    def handle(
        self,
        message:  Dict[str, Any],
        platform: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Handle one incoming message from any external platform.

        Accepts a raw dict and returns a platform-formatted dict.

        Parameters
        ----------
        message : dict
            Must include: ``sender_id``, ``platform``, ``intent``.
            Optional: ``entities``, ``text``, ``session_id``,
                      ``metadata``, ``trust_score``.
        platform : str, optional
            Override the response platform (useful for testing).

        Returns
        -------
        dict
            Platform-formatted response.
        """
        t0 = time.monotonic()
        self._metrics["handled"] += 1

        try:
            incoming = self._parse(message)
            response = self._process(incoming)
            out_platform = platform or incoming.platform
            result = self._adapter.adapt(response, out_platform)

            if response.ok:
                self._metrics["success"] += 1
            self._emit("bridge.response", incoming.sender_id, {
                "status":   response.status.value,
                "intent":   response.intent,
                "agent_id": response.agent_id,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            })
            return result

        except Exception as exc:
            self._metrics["errors"] += 1
            logger.exception("PACTBridge.handle error: %s", exc)
            return {"ok": False, "error": str(exc), "status": "error"}

    def register_handler(self, agent_id: str, handler) -> None:
        """
        Register an in-process handler for *agent_id*.

        ``handler(intent, entities, session_context) → dict``
        """
        self._handlers[agent_id] = handler
        logger.info("Registered in-process handler for agent %r", agent_id)

    # ── internal pipeline ─────────────────────────────────────────────────────

    def _parse(self, raw: Dict[str, Any]) -> IncomingMessage:
        """Normalise a raw dict into an IncomingMessage."""
        sender_id  = raw.get("sender_id") or raw.get("sender") or "anonymous"
        platform   = raw.get("platform", "custom")
        intent     = raw.get("intent") or raw.get("action", "")
        trust      = raw.get("trust_score", self._get_trust(sender_id))

        return IncomingMessage(
            sender_id   = sender_id,
            platform    = platform,
            intent      = intent,
            entities    = raw.get("entities", {}),
            text        = raw.get("text"),
            session_id  = raw.get("session_id"),
            metadata    = raw.get("metadata", {}),
            trust_score = trust,
        )

    def _process(self, incoming: IncomingMessage) -> BridgeResponse:
        """Full pipeline: route → dispatch → session → response."""

        # ── Session ──────────────────────────────────────────────────────────
        session = self._sessions.get_or_create(
            sender_id  = incoming.sender_id,
            platform   = incoming.platform,
            session_id = incoming.session_id,
        )

        # ── RLP-0 relational session ─────────────────────────────────────────
        rlp_session = self.rlp_store.get_or_create(
            session_id = session.session_id,
            bus        = self._bus,
        )

        # ── Route ────────────────────────────────────────────────────────────
        decision = self._router.route(incoming)

        # Map sender trust into rlp-0 — every message updates relational state
        rlp_session.on_trust_evaluated(incoming.trust_score)

        self._emit("bridge.routed", incoming.sender_id, decision.to_dict())

        if decision.outcome == RoutingOutcome.UNTRUSTED:
            return BridgeResponse(
                status          = BridgeStatus.UNTRUSTED,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": "Sender not trusted."},
                session_id      = session.session_id,
            )

        if decision.outcome == RoutingOutcome.NO_AGENT:
            return BridgeResponse(
                status          = BridgeStatus.NO_AGENT,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": f"No agent handles {decision.translated_intent!r}."},
                session_id      = session.session_id,
            )

        if decision.outcome == RoutingOutcome.UNKNOWN_INTENT:
            return BridgeResponse(
                status          = BridgeStatus.ERROR,
                intent          = incoming.intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": f"Unknown intent: {incoming.intent!r}"},
                session_id      = session.session_id,
            )

        # Intent was translated — signal that shared intent is clear
        if decision.translated_intent != incoming.intent:
            rlp_session.on_intent_translated(confidence=1.0)

        # ── Relational gate check ────────────────────────────────────────────
        # rlp-0 may have closed the gate from accumulated rupture across turns
        if not rlp_session.gate_open():
            self._metrics["rupture_blocked"] += 1
            logger.warning(
                "RLP gate closed for session %s (risk=%.2f) — blocking routing",
                session.session_id, rlp_session.rupture_risk(),
            )
            return BridgeResponse(
                status          = BridgeStatus.RUPTURE_BLOCKED,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {
                    "message":      "Relational rupture detected — interaction blocked until repair.",
                    "rupture_risk": rlp_session.rupture_risk(),
                },
                session_id      = session.session_id,
                warnings        = [f"rlp-0 gate closed; rupture_risk={rlp_session.rupture_risk():.2f}"],
            )

        # ── Multi-agent consensus ────────────────────────────────────────────
        if decision.needs_consensus:
            return self._run_consensus(incoming, session, decision, rlp_session)

        # ── Single agent dispatch ────────────────────────────────────────────
        agent   = decision.primary_agent
        result  = self._dispatch(agent, decision.translated_intent,
                                 incoming.entities, session.context)

        session.record_turn(
            intent     = incoming.intent,
            translated = decision.translated_intent,
            agent_id   = agent.agent_id,
            response   = result,
            entities   = incoming.entities,
        )

        return BridgeResponse(
            status          = BridgeStatus.SUCCESS,
            intent          = decision.translated_intent,
            original_intent = incoming.intent,
            agent_id        = agent.agent_id,
            result          = result,
            session_id      = session.session_id,
        )

    def _run_consensus(self, incoming, session, decision, rlp_session=None) -> BridgeResponse:
        """Collect votes from all candidate agents and run ConsensusProtocol."""
        if self._consensus_proto is None:
            logger.error("Multi-agent intent but no ConsensusProtocol configured.")
            return BridgeResponse(
                status          = BridgeStatus.ERROR,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": "ConsensusProtocol not configured."},
                session_id      = session.session_id,
            )

        try:
            from pact_ax.coordination.consensus import Vote
        except ImportError:
            logger.error("pact-ax not installed — cannot run consensus.")
            return BridgeResponse(
                status          = BridgeStatus.ERROR,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": "pact-ax required for consensus routing."},
                session_id      = session.session_id,
            )

        votes: List = []
        for agent in decision.candidate_agents:
            result = self._dispatch(
                agent,
                decision.translated_intent,
                incoming.entities,
                session.context,
            )
            # Each agent's result includes a "confidence" field, or we default
            confidence = float(result.get("confidence", agent.trust_score))
            decision_text = result.get("decision") or result.get("action") or "proceed"
            votes.append(Vote(
                agent_id   = agent.agent_id,
                decision   = decision_text,
                confidence = min(max(confidence, 0.0), 1.0),
                reasoning  = result.get("reasoning", ""),
            ))

        trust_scores = {a.agent_id: a.trust_score for a in decision.candidate_agents}
        consensus    = self._consensus_proto.run(
            votes        = votes,
            trust_scores = trust_scores,
            round_id     = f"bridge-{session.session_id}-{len(session.turns)}",
        )
        self._metrics["consensus_rounds"] += 1

        self._emit(
            "consensus.reached" if consensus.reached else "consensus.failed",
            "pact-bridge",
            consensus.to_dict(),
        )

        if rlp_session is not None:
            if consensus.reached:
                rlp_session.on_consensus_reached(confidence=consensus.confidence_score)
            else:
                rlp_session.on_consensus_failed(agent_count=len(votes))

        if not consensus.reached:
            return BridgeResponse(
                status          = BridgeStatus.CONSENSUS_FAIL,
                intent          = decision.translated_intent,
                original_intent = incoming.intent,
                agent_id        = None,
                result          = {"message": f"Consensus failed: {consensus.outcome.value}"},
                session_id      = session.session_id,
                consensus_result = consensus,
                warnings        = [f"Outcome: {consensus.outcome.value}"],
            )

        winning_agent_id = consensus.dissent_map.get(consensus.winning_decision, [None])[0]
        session.record_turn(
            intent     = incoming.intent,
            translated = decision.translated_intent,
            agent_id   = winning_agent_id or "consensus",
            response   = {"decision": consensus.winning_decision},
            entities   = incoming.entities,
        )
        return BridgeResponse(
            status           = BridgeStatus.SUCCESS,
            intent           = decision.translated_intent,
            original_intent  = incoming.intent,
            agent_id         = winning_agent_id,
            result           = {
                "decision":   consensus.winning_decision,
                "confidence": consensus.confidence_score,
            },
            session_id       = session.session_id,
            consensus_result = consensus,
        )

    def _dispatch(
        self,
        agent:    AgentCard,
        intent:   str,
        entities: Dict,
        context:  Dict,
    ) -> Dict[str, Any]:
        """
        Call the agent — in-process handler or HTTP endpoint.
        Returns a result dict.
        """
        # In-process handler
        handler = self._handlers.get(agent.agent_id)
        if handler:
            try:
                return handler(intent, entities, context) or {}
            except Exception as exc:
                logger.error("Handler %r raised: %s", agent.agent_id, exc)
                return {"error": str(exc)}

        # HTTP endpoint
        if agent.endpoint:
            return self._call_http(agent, intent, entities, context)

        # No handler and no endpoint — return a stub
        logger.warning("Agent %r has no handler or endpoint.", agent.agent_id)
        return {
            "message":  f"Intent {intent!r} acknowledged by {agent.agent_id}.",
            "agent_id": agent.agent_id,
            "status":   "stub",
        }

    def _call_http(self, agent: AgentCard, intent: str, entities: Dict, context: Dict) -> Dict:
        """POST to an HTTP agent endpoint."""
        try:
            import urllib.request, json as _json
            payload = _json.dumps({
                "intent":   intent,
                "entities": entities,
                "context":  context,
            }).encode()
            req = urllib.request.Request(
                agent.endpoint,
                data    = payload,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return _json.loads(resp.read())
        except Exception as exc:
            logger.error("HTTP dispatch to %s failed: %s", agent.endpoint, exc)
            return {"error": str(exc), "agent_id": agent.agent_id}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_trust(self, sender_id: str) -> float:
        """Look up sender trust from TrustNetwork, default 1.0."""
        if self._trust_net is None:
            return 1.0
        try:
            score = self._trust_net.get_trust("bridge", sender_id)
            return score.overall_trust() if hasattr(score, "overall_trust") else 1.0
        except Exception:
            return 1.0

    def _build_consensus(self):
        try:
            from pact_ax.coordination.consensus import ConsensusProtocol, ConsensusStrategy
            strategy_map = {
                "weighted_vote":        ConsensusStrategy.WEIGHTED_VOTE,
                "quorum":               ConsensusStrategy.QUORUM,
                "unanimous":            ConsensusStrategy.UNANIMOUS,
                "confidence_threshold": ConsensusStrategy.CONFIDENCE_THRESHOLD,
            }
            strategy = strategy_map.get(
                self.config.consensus_strategy,
                ConsensusStrategy.WEIGHTED_VOTE,
            )
            return ConsensusProtocol(
                strategy             = strategy,
                quorum_fraction      = self.config.consensus_quorum,
                confidence_threshold = self.config.confidence_threshold,
            )
        except ImportError:
            logger.info("pact-ax not installed — ConsensusProtocol unavailable.")
            return None

    def _make_transfer_manager(self, agent_id: str):
        try:
            from pact_ax.state.state_transfer_manager import StateTransferManager
            return StateTransferManager(agent_id=agent_id)
        except ImportError:
            return None

    def _wire_bus(self) -> None:
        """Subscribe to bus events that should update bridge state."""
        if self._bus is None:
            return
        try:
            from pact_ax.coordination.coordination_bus import EventType

            def on_trust_updated(event):
                trustee   = event.payload.get("trustee")
                new_score = event.payload.get("new_score")
                if trustee and new_score is not None:
                    self.registry.update_trust(trustee, new_score)

            self._bus.subscribe(on_trust_updated, EventType.TRUST_UPDATED)
            logger.info("PACTBridge wired to CoordinationBus.")
        except ImportError:
            logger.info("pact-ax CoordinationBus not available — skipping bus wiring.")

    def _emit(self, event_name: str, source: str, payload: Dict) -> None:
        if self._bus is None:
            return
        try:
            from pact_ax.coordination.coordination_bus import CoordinationEvent, EventType
            # Map string names to EventType where possible
            try:
                et = EventType(event_name)
            except ValueError:
                et = EventType.CUSTOM
            self._bus.publish(CoordinationEvent(
                event_type = et,
                source     = source,
                payload    = payload,
            ))
        except Exception as exc:
            logger.debug("Bus emit failed: %s", exc)

    # ── observability ─────────────────────────────────────────────────────────

    def metrics(self) -> Dict[str, Any]:
        return {
            "bridge":    dict(self._metrics),
            "router":    self._router.stats(),
            "sessions":  self._sessions.metrics(),
            "registry":  self.registry.metrics(),
            "consensus": (
                self._consensus_proto.metrics()
                if self._consensus_proto else None
            ),
            "rlp":       self.rlp_store.metrics(),
        }

    def health(self) -> Dict[str, Any]:
        rlp_metrics = self.rlp_store.metrics()
        return {
            "status":            "ok",
            "live_agents":       len(self.registry),
            "active_sessions":   self._sessions.metrics()["active_sessions"],
            "pact_registry":     "connected" if self._router._pact_reg else "not connected",
            "trust_network":     "connected" if self._trust_net else "not connected",
            "coordination_bus":  "connected" if self._bus else "not connected",
            "consensus":         "ready" if self._consensus_proto else "unavailable",
            "rlp_sessions":      rlp_metrics["active_rlp_sessions"],
            "rlp_gated":         rlp_metrics.get("gated_sessions", 0),
            "rlp_avg_risk":      rlp_metrics["avg_rupture_risk"],
        }

    def __repr__(self) -> str:
        return (
            f"PACTBridge("
            f"agents={len(self.registry)}, "
            f"pact={'yes' if self._router._pact_reg else 'no'}, "
            f"pact_ax={'yes' if self._consensus_proto else 'no'})"
        )

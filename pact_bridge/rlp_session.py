"""
pact_bridge/rlp_session.py

Per-session rlp-0 adapter for pact-bridge.

Maps bridge pipeline events → rlp-0 relational state updates.
Forwards rlp-0 signals → CoordinationBus.

Drop this file into pact_bridge/ and wire into core.py
(see INTEGRATION.md for the exact lines).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# rlp-0 import — degrades gracefully if not installed
try:
    from rlp_0 import RLP0, Signal
    _RLP0_AVAILABLE = True
except ImportError:
    _RLP0_AVAILABLE = False
    logger.debug("rlp-0 not installed — RLPSession running in passthrough mode")


# ─── Trust mapping ────────────────────────────────────────────────────────────

def _map_pact_ax_trust(pact_ax_score: float) -> float:
    """
    pact-ax TrustScore.overall_trust() → rlp-0 trust primitive.

    pact-ax trust is epistemically grounded (competence, honesty, calibration,
    reliability). rlp-0 trust is relational. They're not the same thing, but
    epistemic trust is a strong input signal to relational trust.

    We apply a slight compression: rlp-0 trust never fully bottoms out from
    a single pact-ax reading. Relational trust decays slower than epistemic.
    """
    return 0.2 + (pact_ax_score * 0.8)


# ─── RLPSession ───────────────────────────────────────────────────────────────

@dataclass
class RLPEvent:
    event_type: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict = field(default_factory=dict)


class RLPSession:
    """
    Wraps a single rlp-0 instance for the lifetime of a pact-bridge session.

    Lifecycle events from the bridge pipeline are translated into rlp-0
    state updates. rlp-0 signals are forwarded to the CoordinationBus.

    Usage:
        rlp_session = RLPSession(session_id="abc123")
        rlp_session.attach_bus(coordination_bus)  # optional

        # As the pipeline runs:
        rlp_session.on_trust_evaluated(0.85)
        rlp_session.on_consensus_reached(confidence=0.9)

        # Check gate before responding:
        if not rlp_session.gate_open():
            # relational rupture — surface to caller
            ...
    """

    def __init__(
        self,
        session_id: str,
        rupture_threshold: float = 0.45,
        on_rupture: Optional[Callable[[dict], None]] = None,
    ):
        self.session_id = session_id
        self.rupture_threshold = rupture_threshold
        self._on_rupture_callback = on_rupture
        self._bus = None
        self._event_log: list[RLPEvent] = []
        self._available = _RLP0_AVAILABLE

        if self._available:
            self._rlp = RLP0(rupture_threshold=rupture_threshold)
            self._rlp.subscribe(self._on_signal)
        else:
            self._rlp = None

    def attach_bus(self, bus) -> "RLPSession":
        """Attach a CoordinationBus to forward rlp-0 signals. Chainable."""
        self._bus = bus
        return self

    # ─── Pipeline event handlers ──────────────────────────────────────────────

    def on_trust_evaluated(self, pact_ax_trust_score: float) -> None:
        """
        Called after pact-bridge evaluates sender trust via pact-ax TrustNetwork.

        pact_ax_trust_score: TrustScore.overall_trust() result (0.0–1.0)
        """
        self._log("trust_evaluated", {"pact_ax_score": pact_ax_trust_score})
        if not self._available:
            return

        rlp_trust = _map_pact_ax_trust(pact_ax_trust_score)
        s = self._rlp.state
        self._rlp.update_state(
            trust=rlp_trust,
            intent=s.intent,
            narrative=s.narrative,
            commitments=s.commitments,
        )

    def on_intent_translated(self, confidence: float) -> None:
        """
        Called after pact successfully translates an intent.

        High confidence translation = clear shared intent signal.
        """
        self._log("intent_translated", {"confidence": confidence})
        if not self._available:
            return

        s = self._rlp.state
        # intent maps directly; narrative improves slightly when communication is clear
        self._rlp.update_state(
            trust=s.trust,
            intent=confidence,
            narrative=min(1.0, s.narrative + 0.05),
            commitments=s.commitments,
        )

    def on_consensus_reached(self, confidence: float) -> None:
        """
        Called when multi-agent consensus succeeds.

        Agents agreed → intent is clear, narrative coherence improves.
        """
        self._log("consensus_reached", {"confidence": confidence})
        if not self._available:
            return

        s = self._rlp.state
        self._rlp.update_state(
            trust=min(1.0, s.trust + 0.05),
            intent=confidence,
            narrative=min(1.0, s.narrative + 0.1),
            commitments=s.commitments,
        )

    def on_consensus_failed(self, agent_count: int = 0) -> None:
        """
        Called when agents fail to reach consensus.

        Divergence → intent unclear, narrative coherence drops.
        """
        self._log("consensus_failed", {"agent_count": agent_count})
        if not self._available:
            return

        s = self._rlp.state
        self._rlp.update_state(
            trust=s.trust,
            intent=max(0.0, s.intent - 0.2),
            narrative=max(0.0, s.narrative - 0.15),
            commitments=s.commitments,
        )

    def on_escalation_to_human(self) -> None:
        """
        Called when the bridge routes to pact-hh for human escalation.

        Escalation signals relational stress — trust and intent take a small hit.
        The gate doesn't close here; it may close after pact-hh processes the outcome.
        """
        self._log("escalation_to_human", {})
        if not self._available:
            return

        s = self._rlp.state
        self._rlp.update_state(
            trust=max(0.0, s.trust - 0.1),
            intent=max(0.0, s.intent - 0.1),
            narrative=s.narrative,
            commitments=s.commitments,
        )

    def on_human_decision(self, decision: str, agent_aligned: bool = True) -> None:
        """
        Called by pact-hh RLPAdapter after a human decision is injected.

        decision: 'approve' | 'hold' | 'escalate'
        agent_aligned: whether the agents' recommendation matched the human decision
        """
        self._log("human_decision", {"decision": decision, "agent_aligned": agent_aligned})
        if not self._available:
            return

        if decision == "approve":
            # Human approved — relational repair
            self._rlp.update_state(
                trust=0.8,
                intent=0.85,
                narrative=0.8,
                commitments=0.85,
            )
            released = self._rlp.acknowledge_repair()
            if released:
                logger.info(f"[RLPSession:{self.session_id}] gate released after human approval")
        elif decision == "hold":
            # Human held — pause, not rupture; mild downgrade
            s = self._rlp.state
            self._rlp.update_state(
                trust=max(0.0, s.trust - 0.1),
                intent=max(0.0, s.intent - 0.15),
                narrative=s.narrative,
                commitments=s.commitments,
            )
        elif decision == "escalate":
            # Escalated further — deeper uncertainty signal
            s = self._rlp.state
            self._rlp.update_state(
                trust=max(0.0, s.trust - 0.2),
                intent=max(0.0, s.intent - 0.2),
                narrative=max(0.0, s.narrative - 0.1),
                commitments=s.commitments,
            )

        # Trust network update: agents who aligned with human get a nudge
        # (the actual TrustNetwork update happens in pact-hh decision_injector;
        #  this is the rlp-0 layer reflection)
        if agent_aligned and decision == "approve":
            s = self._rlp.state
            self._rlp.update_state(
                trust=min(1.0, s.trust + 0.02),
                intent=s.intent,
                narrative=s.narrative,
                commitments=s.commitments,
            )

    def on_commitment_made(self, commitment_description: str) -> None:
        """
        Called when a binding commitment is made in this session
        (e.g. agent promises a follow-up action).
        """
        self._log("commitment_made", {"description": commitment_description})
        if not self._available:
            return

        s = self._rlp.state
        self._rlp.update_state(
            trust=s.trust,
            intent=s.intent,
            narrative=s.narrative,
            commitments=min(1.0, s.commitments + 0.1),
        )

    # ─── Gate / state inspection ──────────────────────────────────────────────

    def gate_open(self) -> bool:
        """True if interaction can proceed normally."""
        if not self._available:
            return True
        return self._rlp.check_gate()

    def rupture_risk(self) -> float:
        """Current rupture risk (0.0–1.0). 0 = healthy, 1 = critical."""
        if not self._available:
            return 0.0
        return self._rlp.rupture_risk

    def status(self) -> dict:
        if not self._available:
            return {"session_id": self.session_id, "rlp0_available": False}
        return {
            "session_id": self.session_id,
            "rlp0_available": True,
            **self._rlp.status(),
            "event_count": len(self._event_log),
        }

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _on_signal(self, signal: "Signal") -> None:
        """Forward rlp-0 RUPTURE_DETECTED to CoordinationBus and optional callback."""
        payload = {
            "session_id": self.session_id,
            "signal": str(signal),
            "rupture_risk": self.rupture_risk(),
            "gate_open": self.gate_open(),
        }

        if self._bus:
            try:
                self._bus.publish("RLP_RUPTURE_DETECTED", payload)
            except Exception as exc:
                logger.warning(f"[RLPSession] bus publish failed: {exc}")

        if self._on_rupture_callback:
            try:
                self._on_rupture_callback(payload)
            except Exception as exc:
                logger.warning(f"[RLPSession] on_rupture callback failed: {exc}")

        logger.warning(
            f"[RLPSession:{self.session_id}] RUPTURE_DETECTED "
            f"risk={self.rupture_risk():.2f}"
        )

    def _log(self, event_type: str, data: dict) -> None:
        self._event_log.append(RLPEvent(event_type=event_type, data=data))


# ─── RLPSessionStore ──────────────────────────────────────────────────────────

class RLPSessionStore:
    """
    Mirrors pact-bridge's SessionStore — one RLPSession per bridge session.

    Attach to PACTBridge alongside the existing SessionStore.
    """

    def __init__(self, rupture_threshold: float = 0.45):
        self._sessions: dict[str, RLPSession] = {}
        self.rupture_threshold = rupture_threshold

    def get_or_create(
        self,
        session_id: str,
        bus=None,
        on_rupture: Optional[Callable[[dict], None]] = None,
    ) -> RLPSession:
        if session_id not in self._sessions:
            session = RLPSession(
                session_id=session_id,
                rupture_threshold=self.rupture_threshold,
                on_rupture=on_rupture,
            )
            if bus:
                session.attach_bus(bus)
            self._sessions[session_id] = session
        return self._sessions[session_id]

    def get(self, session_id: str) -> Optional[RLPSession]:
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def metrics(self) -> dict:
        active = len(self._sessions)
        if active == 0:
            return {"active_rlp_sessions": 0, "avg_rupture_risk": 0.0}
        avg_risk = sum(s.rupture_risk() for s in self._sessions.values()) / active
        gated = sum(1 for s in self._sessions.values() if not s.gate_open())
        return {
            "active_rlp_sessions": active,
            "avg_rupture_risk": round(avg_risk, 3),
            "gated_sessions": gated,
        }

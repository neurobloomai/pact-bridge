"""
pact_bridge/session_store.py
──────────────────────────────
SessionStore — multi-turn conversation context using pact-ax StateTransferManager.

Each conversation gets a Session that accumulates state across turns.
When a conversation hands off to a different agent, StateTransferManager
packages the full context (state + epistemic + narrative) and the receiving
agent integrates it seamlessly.

This is the second major pact/pact-ax integration point:
  pact-ax StateTransferManager  ←→  bridge Session lifecycle
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """A single exchange within a session."""

    intent:      str
    translated:  str
    agent_id:    str
    response:    Dict[str, Any]
    entities:    Dict[str, Any] = field(default_factory=dict)
    timestamp:   float          = field(default_factory=time.time)


@dataclass
class Session:
    """
    Live conversation session between a sender and the bridge.

    Wraps pact-ax StateTransferManager to enable:
      - cross-turn state accumulation
      - agent handoff with full context preservation
      - checkpoint/rollback for risky actions
    """

    session_id:      str
    sender_id:       str
    platform:        str
    created_at:      float          = field(default_factory=time.time)
    last_active:     float          = field(default_factory=time.time)
    current_agent:   Optional[str]  = None
    turns:           List[Turn]     = field(default_factory=list)
    context:         Dict[str, Any] = field(default_factory=dict)
    _transfer_mgr:   Any            = field(default=None, repr=False)  # StateTransferManager

    def record_turn(
        self,
        intent:     str,
        translated: str,
        agent_id:   str,
        response:   Dict[str, Any],
        entities:   Dict[str, Any] = None,
    ) -> None:
        self.turns.append(Turn(
            intent     = intent,
            translated = translated,
            agent_id   = agent_id,
            response   = response,
            entities   = entities or {},
        ))
        self.current_agent = agent_id
        self.last_active   = time.time()
        # Accumulate context for state transfer
        self.context["last_intent"]  = translated
        self.context["last_agent"]   = agent_id
        self.context["turn_count"]   = len(self.turns)

    def prepare_handoff(self, to_agent_id: str, reason: str = "continuation") -> Optional[str]:
        """
        Use StateTransferManager to prepare a context handoff to *to_agent_id*.
        Returns the packet_id, or None if no transfer manager is attached.
        """
        if self._transfer_mgr is None:
            logger.debug("No StateTransferManager attached — skipping handoff packet.")
            return None
        try:
            from pact_ax.state.state_transfer_manager import HandoffReason
            reason_enum = HandoffReason(reason) if reason in [r.value for r in HandoffReason] \
                          else HandoffReason.CONTINUATION
            packet_id = self._transfer_mgr.prepare(
                to_agent_id = to_agent_id,
                state_data  = {
                    "session_id":    self.session_id,
                    "sender_id":     self.sender_id,
                    "platform":      self.platform,
                    "context":       self.context,
                    "turn_count":    len(self.turns),
                    "recent_turns":  [
                        {"intent": t.intent, "agent": t.agent_id}
                        for t in self.turns[-3:]
                    ],
                },
                reason  = reason_enum,
                context = {"session_id": self.session_id},
            )
            return packet_id
        except Exception as exc:
            logger.warning("StateTransferManager.prepare failed: %s", exc)
            return None

    def checkpoint(self, label: str) -> Optional[str]:
        """Snapshot current session state via StateTransferManager."""
        if self._transfer_mgr is None:
            return None
        try:
            return self._transfer_mgr.checkpoint(
                label      = label,
                state_data = dict(self.context),
            )
        except Exception as exc:
            logger.warning("Checkpoint failed: %s", exc)
            return None

    def age_seconds(self) -> float:
        return time.time() - self.last_active

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id":    self.session_id,
            "sender_id":     self.sender_id,
            "platform":      self.platform,
            "turn_count":    len(self.turns),
            "current_agent": self.current_agent,
            "age_seconds":   round(self.age_seconds(), 1),
            "context_keys":  list(self.context.keys()),
        }


class SessionStore:
    """
    Manages the lifecycle of all active bridge sessions.

    Parameters
    ----------
    ttl_minutes : int
        Idle sessions older than this are garbage-collected.  Default 60.
    transfer_manager_factory : callable, optional
        ``(agent_id) → StateTransferManager`` — called when a new session
        is created.  Pass None to skip state transfer integration.
    """

    def __init__(
        self,
        ttl_minutes:              int      = 60,
        transfer_manager_factory=None,     # (agent_id) → StateTransferManager
    ) -> None:
        self._sessions:   Dict[str, Session] = {}
        self._ttl:        float              = ttl_minutes * 60
        self._factory     = transfer_manager_factory

    # ── session lifecycle ─────────────────────────────────────────────────────

    def get_or_create(
        self,
        sender_id:  str,
        platform:   str,
        session_id: Optional[str] = None,
    ) -> Session:
        """
        Return an existing session by *session_id*, or create a new one.
        """
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            if session.age_seconds() < self._ttl:
                session.last_active = time.time()
                return session
            # expired — start fresh
            del self._sessions[session_id]

        new_id  = session_id or f"sess-{uuid.uuid4().hex[:10]}"
        mgr     = None
        if self._factory:
            try:
                mgr = self._factory(sender_id)
            except Exception as exc:
                logger.warning("StateTransferManager factory failed: %s", exc)

        session = Session(
            session_id   = new_id,
            sender_id    = sender_id,
            platform     = platform,
            _transfer_mgr = mgr,
        )
        self._sessions[new_id] = session
        logger.debug("Created session %s for sender %s", new_id, sender_id)
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.debug("Closed session %s", session_id)
            return True
        return False

    def gc(self) -> int:
        """Garbage-collect expired sessions. Returns count removed."""
        expired = [
            sid for sid, s in self._sessions.items()
            if s.age_seconds() >= self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            logger.info("GC removed %d expired sessions", len(expired))
        return len(expired)

    # ── observability ─────────────────────────────────────────────────────────

    def metrics(self) -> Dict[str, Any]:
        active   = [s for s in self._sessions.values() if s.age_seconds() < self._ttl]
        avg_turns = (
            sum(len(s.turns) for s in active) / len(active) if active else 0
        )
        return {
            "active_sessions":    len(active),
            "total_sessions":     len(self._sessions),
            "avg_turns_per_sess": round(avg_turns, 1),
            "ttl_minutes":        self._ttl / 60,
        }

    def __len__(self) -> int:
        return len(self._sessions)

    def __repr__(self) -> str:
        return f"SessionStore(active={len(self)}, ttl_min={self._ttl/60:.0f})"

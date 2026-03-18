"""
pact_bridge/agent_registry.py
───────────────────────────────
AgentRegistry — dynamic agent discovery for PACT-Bridge.

Fixes the fake registry problem: the PACT schema references
neurobloom.ai/pact/agents as a discovery endpoint but nothing
implements it.  This module provides the actual registry.

Agents register themselves (or are registered statically via config).
The bridge queries the registry to find who can handle a given intent
before routing through pact-ax's HumilityAwareCoordinator.

Registration modes
──────────────────
1. Static  — pass a dict at startup (tests, local dev)
2. Heartbeat — agents call register() periodically; TTL-based expiry
3. Config file — JSON/YAML listing of agent capabilities

Usage
─────
    from pact_bridge.agent_registry import AgentRegistry, AgentCard

    registry = AgentRegistry(heartbeat_ttl_seconds=60)

    registry.register(AgentCard(
        agent_id     = "billing-agent",
        platform     = "rasa",
        capabilities = {"billing.lookup", "billing.dispute", "account.update"},
        endpoint     = "http://billing-service:8080",
        trust_score  = 0.9,
    ))

    card = registry.find("billing.lookup")   # → AgentCard or None
    all_ = registry.candidates("billing")    # → [AgentCard, ...]
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class AgentCard:
    """
    Self-description of a registered agent.

    Parameters
    ----------
    agent_id : str
        Unique identifier (matches pact-ax agent_id convention).
    platform : str
        Which platform this agent speaks — "rasa", "dialogflow", "custom", etc.
    capabilities : set of str
        Translated PACT intents this agent can handle.
        Should use the pact registry's *translated* intent names (e.g.
        "billing.lookup" not "check_bill").
    endpoint : str, optional
        HTTP endpoint to call when routing to this agent.  None for
        in-process agents.
    trust_score : float
        Initial trust score (0–1).  Updated by TrustNetwork at runtime.
    metadata : dict
        Arbitrary extra data (version, region, SLA, etc.).
    """

    agent_id:     str
    platform:     str
    capabilities: Set[str]       = field(default_factory=set)
    endpoint:     Optional[str]  = None
    trust_score:  float          = 0.7
    metadata:     Dict           = field(default_factory=dict)
    _registered_at: float        = field(default_factory=time.monotonic, repr=False)
    _last_heartbeat: float       = field(default_factory=time.monotonic, repr=False)

    def handles(self, intent: str) -> bool:
        """Return True if this agent advertises the given intent."""
        return intent in self.capabilities

    def touch(self) -> None:
        """Record a fresh heartbeat."""
        self._last_heartbeat = time.monotonic()

    def age_seconds(self) -> float:
        return time.monotonic() - self._last_heartbeat

    def to_dict(self) -> Dict:
        return {
            "agent_id":     self.agent_id,
            "platform":     self.platform,
            "capabilities": sorted(self.capabilities),
            "endpoint":     self.endpoint,
            "trust_score":  self.trust_score,
            "metadata":     self.metadata,
        }


class AgentRegistry:
    """
    Live registry of all agents known to the bridge.

    Parameters
    ----------
    heartbeat_ttl_seconds : int
        Agents that haven't sent a heartbeat within this window are
        considered stale and excluded from routing.  Default 120s.
        Set to 0 to disable TTL (useful for static registries).
    """

    def __init__(self, heartbeat_ttl_seconds: int = 120) -> None:
        self._agents:  Dict[str, AgentCard] = {}
        self._ttl:     int                  = heartbeat_ttl_seconds

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, card: AgentCard) -> None:
        """Add or refresh an agent's registration."""
        card.touch()
        self._agents[card.agent_id] = card
        logger.info(
            "Registered agent %r (%s) with %d capabilities",
            card.agent_id, card.platform, len(card.capabilities),
        )

    def heartbeat(self, agent_id: str) -> bool:
        """
        Record a liveness signal from *agent_id*.
        Returns False if the agent is not registered.
        """
        if agent_id not in self._agents:
            logger.warning("Heartbeat from unknown agent %r — ignoring.", agent_id)
            return False
        self._agents[agent_id].touch()
        return True

    def deregister(self, agent_id: str) -> bool:
        """Remove an agent. Returns True if it existed."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            logger.info("Deregistered agent %r", agent_id)
            return True
        return False

    def update_trust(self, agent_id: str, new_score: float) -> None:
        """Propagate trust network updates into the registry."""
        if agent_id in self._agents:
            self._agents[agent_id].trust_score = max(0.0, min(1.0, new_score))

    # ── discovery ─────────────────────────────────────────────────────────────

    def find(self, intent: str, min_trust: float = 0.0) -> Optional[AgentCard]:
        """
        Return the highest-trust live agent that handles *intent*.
        Returns None if no candidate exists.
        """
        candidates = self.candidates(intent, min_trust=min_trust)
        return max(candidates, key=lambda c: c.trust_score) if candidates else None

    def candidates(
        self,
        intent: str,
        min_trust: float = 0.0,
    ) -> List[AgentCard]:
        """
        Return all live agents that handle *intent* above *min_trust*,
        sorted by trust_score descending.
        """
        live    = self._live_agents()
        matched = [
            c for c in live
            if c.handles(intent) and c.trust_score >= min_trust
        ]
        return sorted(matched, key=lambda c: c.trust_score, reverse=True)

    def all_agents(self) -> List[AgentCard]:
        """Return all live agents."""
        return self._live_agents()

    def get(self, agent_id: str) -> Optional[AgentCard]:
        card = self._agents.get(agent_id)
        return card if card and self._is_live(card) else None

    def platforms(self) -> Set[str]:
        """Return the set of all registered platforms."""
        return {c.platform for c in self._live_agents()}

    def capability_map(self) -> Dict[str, List[str]]:
        """Return intent → [agent_ids] for all live agents."""
        result: Dict[str, List[str]] = {}
        for card in self._live_agents():
            for cap in card.capabilities:
                result.setdefault(cap, []).append(card.agent_id)
        return result

    # ── bulk loading ──────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, agents: List[Dict], **kwargs) -> "AgentRegistry":
        """
        Load a static registry from a list of agent dicts.

            AgentRegistry.from_dict([
                {"agent_id": "billing", "platform": "rasa",
                 "capabilities": ["billing.lookup"], "trust_score": 0.9},
            ])
        """
        reg = cls(**kwargs)
        for d in agents:
            card = AgentCard(
                agent_id     = d["agent_id"],
                platform     = d.get("platform", "custom"),
                capabilities = set(d.get("capabilities", [])),
                endpoint     = d.get("endpoint"),
                trust_score  = d.get("trust_score", 0.7),
                metadata     = d.get("metadata", {}),
            )
            reg.register(card)
        return reg

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "AgentRegistry":
        """Load from a JSON file containing a list of agent dicts."""
        p = Path(path)
        with p.open("r", encoding="utf-8") as fh:
            agents = json.load(fh)
        return cls.from_dict(agents, **kwargs)

    # ── internals ────────────────────────────────────────────────────────────

    def _is_live(self, card: AgentCard) -> bool:
        if self._ttl == 0:
            return True
        return card.age_seconds() <= self._ttl

    def _live_agents(self) -> List[AgentCard]:
        return [c for c in self._agents.values() if self._is_live(c)]

    def prune_stale(self) -> int:
        """Remove stale agents. Returns count removed."""
        stale = [aid for aid, c in self._agents.items() if not self._is_live(c)]
        for aid in stale:
            del self._agents[aid]
        if stale:
            logger.info("Pruned %d stale agents: %s", len(stale), stale)
        return len(stale)

    def metrics(self) -> Dict:
        live = self._live_agents()
        return {
            "total_registered": len(self._agents),
            "live_count":       len(live),
            "stale_count":      len(self._agents) - len(live),
            "platforms":        sorted(self.platforms()),
            "total_capabilities": len(self.capability_map()),
        }

    def __len__(self) -> int:
        return len(self._live_agents())

    def __repr__(self) -> str:
        return f"AgentRegistry(live={len(self)}, total={len(self._agents)})"

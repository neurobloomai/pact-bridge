"""
pact_bridge/config.py
──────────────────────
BridgeConfig — single configuration object for the entire bridge.

All tuneable knobs live here so callers never reach into bridge internals.

Usage
─────
    from pact_bridge import BridgeConfig, PACTBridge

    cfg = BridgeConfig(
        registry_path     = "path/to/intent_registry.json",
        trust_floor       = 0.4,
        consensus_quorum  = 0.6,
        multi_agent_intents = {"escalate", "approve_budget"},
    )
    bridge = PACTBridge(config=cfg)

    # or pull from environment:
    cfg = BridgeConfig.from_env()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class BridgeConfig:
    """
    Unified configuration for PACTBridge.

    Parameters
    ----------
    registry_path : str, optional
        Path to the pact IntentRegistry JSON/YAML file.
        Falls back to ``PACT_REGISTRY_PATH`` env var, then built-in defaults.

    trust_floor : float
        Minimum trust score a sender must have for their intent to be routed.
        Senders below this threshold receive an UNTRUSTED response.
        Default 0.3.

    session_ttl_minutes : int
        How long an idle session is retained before being garbage-collected.
        Default 60.

    multi_agent_intents : set of str
        Intents that always trigger a multi-agent consensus round rather than
        single-agent routing.  E.g. {"escalate", "approve_budget"}.

    consensus_strategy : str
        Strategy passed to ConsensusProtocol.
        One of: "weighted_vote", "quorum", "unanimous", "confidence_threshold".
        Default "weighted_vote".

    consensus_quorum : float
        Quorum fraction for the QUORUM consensus strategy.  Default 0.5.

    confidence_threshold : float
        Minimum confidence for CONFIDENCE_THRESHOLD consensus strategy.
        Default 0.7.

    max_delegation_depth : int
        Maximum number of agent hops before a query is declared unroutable.
        Default 3.

    enable_gossip : bool
        Whether incoming intents are broadcast through the GossipClarityProtocol
        to share knowledge across the agent network.  Default True.

    enable_bus : bool
        Whether to publish coordination events to the CoordinationBus.
        Default True.

    platform_adapters : dict
        Map of platform name → adapter config dict.
        E.g. {"dialogflow": {"version": "v2"}, "rasa": {"nlu_threshold": 0.6}}

    log_level : str
        Python logging level for bridge internals. Default "INFO".
    """

    registry_path:         Optional[str]   = None
    trust_floor:           float           = 0.3
    session_ttl_minutes:   int             = 60
    multi_agent_intents:   Set[str]        = field(default_factory=set)
    consensus_strategy:    str             = "weighted_vote"
    consensus_quorum:      float           = 0.5
    confidence_threshold:  float           = 0.7
    max_delegation_depth:  int             = 3
    enable_gossip:         bool            = True
    enable_bus:            bool            = True
    platform_adapters:     dict            = field(default_factory=dict)
    log_level:             str             = "INFO"
    rupture_threshold:     float           = 0.45

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        """
        Build a BridgeConfig from environment variables.

        Variables
        ---------
        PACT_REGISTRY_PATH        → registry_path
        PACT_BRIDGE_TRUST_FLOOR   → trust_floor (float)
        PACT_BRIDGE_SESSION_TTL   → session_ttl_minutes (int)
        PACT_BRIDGE_CONSENSUS     → consensus_strategy (str)
        PACT_BRIDGE_LOG_LEVEL     → log_level
        """
        return cls(
            registry_path       = os.environ.get("PACT_REGISTRY_PATH"),
            trust_floor         = float(os.environ.get("PACT_BRIDGE_TRUST_FLOOR", 0.3)),
            session_ttl_minutes = int(os.environ.get("PACT_BRIDGE_SESSION_TTL", 60)),
            consensus_strategy  = os.environ.get("PACT_BRIDGE_CONSENSUS", "weighted_vote"),
            log_level           = os.environ.get("PACT_BRIDGE_LOG_LEVEL", "INFO"),
        )

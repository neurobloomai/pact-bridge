# pact-bridge

> The spinal cord connecting **pact** (intent translation) and **pact-ax** (agent collaboration).

```
External Platform (Dialogflow / Rasa / custom agent)
         │  raw platform message
         ▼
    ┌─────────────────────────────────┐
    │          PACTBridge             │
    │                                 │
    │  IntentRouter                   │
    │    └─ pact IntentRegistry  ───► translated intent
    │    └─ AgentRegistry        ───► who can handle?
    │                                 │
    │  RLPSession (rlp-0)             │
    │    └─ trust / intent signals ──► relational gate check
    │    └─ RUPTURE_BLOCKED if gated  │
    │                                 │
    │  (single agent)                 │
    │    └─ dispatch to best agent    │
    │                                 │
    │  (multi-agent intent)           │
    │    └─ pact-ax ConsensusProtocol │
    │         └─ all candidates vote  │
    │         └─ consensus ──────────► RLPSession.on_consensus_*
    │                                 │
    │  SessionStore                   │
    │    └─ pact-ax StateTransfer ───► context persisted
    │                                 │
    │  ResponseAdapter                │
    │    └─ format for platform  ─────┤
    └─────────────────────────────────┘
         │  platform-formatted response
         ▼
External Platform
```

pact alone can translate intents.
pact-ax alone can coordinate agents.
pact-bridge makes them work **together** as a live, multi-platform, multi-agent system.

---

## Quickstart

```python
from pact_bridge import PACTBridge, BridgeConfig
from pact_bridge.agent_registry import AgentCard

bridge = PACTBridge(
    config=BridgeConfig(
        trust_floor         = 0.4,
        multi_agent_intents = {"approve_refund"},
    )
)

# Register agents
bridge.registry.register(AgentCard(
    agent_id     = "billing-agent",
    platform     = "rasa",
    capabilities = {"billing.lookup", "billing.dispute", "approve_refund"},
    trust_score  = 0.92,
))

# Register in-process handler
bridge.register_handler("billing-agent", lambda intent, entities, ctx: {
    "message": f"Billing handled: {intent}"
})

# Handle a message from any platform
response = bridge.handle({
    "sender_id": "user-42",
    "platform":  "rasa",
    "intent":    "billing.lookup",
    "entities":  {"account_id": "ACC-1234"},
})
# → {"ok": True, "intent": "billing.lookup", "agent": "billing-agent", ...}
```

---

## Installation

```bash
# Bridge only (zero dependencies)
pip install pact-bridge

# With pact intent translation
pip install pact-bridge[pact]

# With pact-ax collaboration
pip install pact-bridge[pact_ax]

# Everything
pip install pact-bridge[full]
```

---

## Architecture

### Components

| Module | Purpose |
|--------|---------|
| `PACTBridge` | Single entry point — orchestrates the full pipeline |
| `AgentRegistry` | Dynamic agent discovery with TTL heartbeats |
| `IntentRouter` | Translates intents (pact) then routes to agents |
| `SessionStore` | Multi-turn sessions via pact-ax StateTransferManager |
| `ResponseAdapter` | Formats bridge results for Dialogflow, Rasa, PACT, custom |
| `BridgeConfig` | All tuneable knobs in one place |

### Routing pipeline

```
handle(message)
  │
  ├─ parse()              normalise raw dict → IncomingMessage
  ├─ trust gate           sender trust_score ≥ config.trust_floor?
  ├─ rlp_session          get/create per-session rlp-0 instance
  ├─ on_trust_evaluated() map sender trust → rlp-0 relational state
  ├─ rlp gate check       gate_open()? → RUPTURE_BLOCKED if closed
  ├─ translate()          pact IntentRegistry.translate(intent)
  ├─ on_intent_translated() intent confidence → rlp-0 intent signal
  ├─ candidates()         AgentRegistry.candidates(translated_intent)
  ├─ single?              dispatch to best-trust agent
  ├─ multi-agent?         ConsensusProtocol.run(votes from all candidates)
  │                         → on_consensus_reached/failed() → rlp-0
  └─ adapt()              ResponseAdapter.adapt(result, platform)
```

### Multi-agent consensus

Declare which intents always require a group decision:

```python
BridgeConfig(multi_agent_intents={"approve_refund", "policy_override"})
```

All registered agents that handle the intent cast a vote.
`ConsensusProtocol` (from pact-ax) picks the winner.
The result includes `consensus.outcome`, `consensus.confidence`, and `consensus.decision`.

### Relational health tracking (rlp-0)

Every session has an `RLPSession` that tracks four relational signals as messages flow through the bridge: trust (from sender trust score), intent (from translation confidence), narrative (from consensus coherence), and commitments. When these signals degrade past a threshold, rlp-0 detects rupture and closes a gate.

```python
# Gate-closed response when relational health is degraded
{
  "status":  "rupture_blocked",
  "ok":      False,
  "result":  {
    "message":      "Relational rupture detected — interaction blocked until repair.",
    "rupture_risk": 0.73,
  }
}
```

The gate reopens when pact-hh's `DecisionInjector` calls `acknowledge_repair()` after a human approves — but only if the primitives actually improved. No unconditional release.

```python
# Full repair loop: bridge detects rupture → pact-hh escalates → human approves → gate releases
bridge = PACTBridge(config=BridgeConfig(rupture_threshold=0.45))
loop   = HumanEscalationLoop.create(rlp_store=bridge.rlp_store, ...)

# loop.rlp_store IS bridge.rlp_store — the same sessions, the shared ledger
```

`bridge.rlp_store` is the live `RLPSessionStore`. Pass it to `HumanEscalationLoop` at startup — that's the entire wiring.

### Session continuity

Sessions persist across turns using pact-ax `StateTransferManager`.
When a conversation switches agents mid-session, the full context
(state + epistemic + narrative) is packaged and handed off transparently.

### Platform adapters

| Platform | Response shape |
|----------|----------------|
| `pact` | Raw PACT 0.1.0 envelope (default) |
| `dialogflow` | Dialogflow v2 WebhookResponse |
| `rasa` | Rasa Actions Server response |
| `custom` | Minimal `{"ok", "status", "intent", "result"}` |

Add your own:

```python
from pact_bridge.response_adapter import PlatformAdapter

class MyAdapter(PlatformAdapter):
    def adapt(self, response):
        return {"my_field": response.result}

bridge._adapter.register("my-platform", MyAdapter())
```

---

## Full integration with pact + pact-ax

```python
from pact_protocol.intent_registry import IntentRegistry
from pact_ax.coordination import TrustNetwork, CoordinationBus

bus       = CoordinationBus()
trust_net = TrustNetwork()

bridge = PACTBridge(
    config          = BridgeConfig.from_env(),
    intent_registry = IntentRegistry.load("intent_registry.json"),
    trust_network   = trust_net,
    bus             = bus,
)
```

When a `TRUST_UPDATED` event fires on the bus, the bridge automatically
updates agent trust scores in the registry.  Every bridge action publishes
events back to the bus for full observability.

---

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `registry_path` | None | Path to pact IntentRegistry JSON/YAML |
| `trust_floor` | 0.3 | Min sender trust to route |
| `session_ttl_minutes` | 60 | Idle session lifetime |
| `multi_agent_intents` | `{}` | Intents requiring consensus |
| `consensus_strategy` | `weighted_vote` | `weighted_vote` \| `quorum` \| `unanimous` \| `confidence_threshold` |
| `rupture_threshold` | 0.45 | rlp-0 rupture sensitivity (0–1); higher = gate closes sooner |
| `enable_gossip` | True | Broadcast intents through gossip layer |
| `enable_bus` | True | Publish events to CoordinationBus |

From environment:

```bash
PACT_REGISTRY_PATH=./intent_registry.json
PACT_BRIDGE_TRUST_FLOOR=0.4
PACT_BRIDGE_CONSENSUS=quorum
```

---

## Observability

```python
bridge.health()
# → {"status": "ok", "live_agents": 3, "active_sessions": 12,
#    "pact_registry": "connected", "consensus": "ready",
#    "rlp_sessions": 12, "rlp_gated": 1, "rlp_avg_risk": 0.18}

bridge.metrics()
# → {"bridge": {"handled": 42, "success": 40, "errors": 2, "rupture_blocked": 1},
#    "router": {...}, "sessions": {...}, "registry": {...}, "consensus": {...},
#    "rlp": {"active_rlp_sessions": 12, "avg_rupture_risk": 0.18, "gated_sessions": 1}}
```

---

## Repository structure

```
pact-bridge/
├── pact_bridge/
│   ├── __init__.py
│   ├── config.py           BridgeConfig
│   ├── core.py             PACTBridge (main orchestrator)
│   ├── agent_registry.py   AgentCard + AgentRegistry
│   ├── intent_router.py    IntentRouter (pact → pact-ax)
│   ├── rlp_session.py      RLPSession + RLPSessionStore (rlp-0 wiring)
│   ├── session_store.py    SessionStore + Session
│   └── response_adapter.py ResponseAdapter + platform adapters
├── docs/
│   └── INTEGRATION_RLP0.md full wiring reference
├── examples/
│   └── quickstart.py
├── tests/
│   └── test_bridge.py
└── pyproject.toml
```

---

## Wiring with pact-hh (full repair loop)

pact-bridge detects relational rupture. pact-hh escalates to a human. The human's decision releases the gate. One line of wiring connects them:

```python
from pact_bridge import PACTBridge, BridgeConfig
from pact_hh import HumanEscalationLoop

bridge = PACTBridge(config=BridgeConfig(rupture_threshold=0.45))

loop = HumanEscalationLoop.create(
    slack_token      = "xoxb-...",
    default_human_id = "on-call",
    rlp_store        = bridge.rlp_store,   # shared relational ledger
)
loop.start()
```

```
message arrives
  │
  ├─ trust too low or prior rupture?
  │    └─ RUPTURE_BLOCKED ──────────────────────────────────────────────┐
  │                                                                      │
  │                                                                      ▼
  │                                                         loop.escalate(session_id=...)
  │                                                              │
  │                                                              ├─ on_escalation_opened()
  │                                                              │   → rlp-0: stress signal
  │                                                              │
  │                                                         human replies "approve"
  │                                                              │
  │                                                              ├─ DecisionInjector.inject()
  │                                                              ├─ RLPAdapter.on_decision()
  │                                                              └─ acknowledge_repair()
  │                                                                   → gate releases if
  │                                                                     risk < threshold
  │
  └─ gate open → route normally

```

For the full wiring reference see [docs/INTEGRATION_RLP0.md](docs/INTEGRATION_RLP0.md).

---

## Related repos

| Repo | Role |
|------|------|
| [pact](https://github.com/neurobloomai/pact) | Intent translation protocol |
| [pact-ax](https://github.com/neurobloomai/pact-ax) | Agent collaboration primitives |
| [pact-bridge](https://github.com/neurobloomai/pact-bridge) | **This repo** — connects them |
| [pact-hh](https://github.com/neurobloomai/pact-hh) | Human-in-the-loop escalation |

---

MIT License · [neurobloom.ai](https://neurobloom.ai)

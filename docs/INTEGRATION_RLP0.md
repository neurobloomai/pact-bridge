# rlp-0 Integration Guide
## Wiring rlp-0 into pact-bridge and pact-hh

---

## What this does

Connects rlp-0 as the shared relational substrate underneath pact-bridge and pact-hh.

- **pact-bridge** gets a per-session rlp-0 instance that tracks relational state as the pipeline runs
- **pact-hh** closes the repair loop — human decisions flow back into rlp-0, releasing gates and updating relational state
- **CoordinationBus** gets a new event: `RLP_RUPTURE_DETECTED` — observable across the whole stack

Two new files. Minimal changes to two existing files.

---

## Files to add

```
pact-bridge/pact_bridge/rlp_session.py     ← drop in
pact-hh/pact_hh/rlp_adapter.py            ← drop in
```

---

## Changes to pact-bridge/pact_bridge/core.py

### 1. Import

```python
# Add near other imports
from pact_bridge.rlp_session import RLPSessionStore
```

### 2. Initialize in PACTBridge.__init__

```python
# Add after self.session_store = SessionStore(...)
self.rlp_store = RLPSessionStore(rupture_threshold=0.45)
```

### 3. Wire into _process()

Find the section where trust is evaluated (roughly: `trust_ok = self._check_trust(sender)`).
Add immediately after the trust check:

```python
# Get or create rlp-0 session
rlp_session = self.rlp_store.get_or_create(
    session_id=session.session_id,
    bus=self.bus,  # or however you reference your CoordinationBus
)

# Map pact-ax trust score → rlp-0
if hasattr(trust_result, 'overall_trust'):
    rlp_session.on_trust_evaluated(trust_result.overall_trust())
```

Find where intent translation happens and add:

```python
if translation and hasattr(translation, 'confidence'):
    rlp_session.on_intent_translated(translation.confidence)
```

Find where consensus is evaluated and add:

```python
if consensus_reached:
    rlp_session.on_consensus_reached(confidence=consensus_confidence)
else:
    rlp_session.on_consensus_failed(agent_count=len(candidates))
```

Find where human escalation is triggered and add:

```python
rlp_session.on_escalation_to_human()
```

### 4. Expose rlp_store on the bridge instance

This lets pact-hh's RLPAdapter find sessions by ID:

```python
# Already covered by self.rlp_store = RLPSessionStore(...) in __init__
# Just make sure pact-hh receives a reference to the bridge instance
# or to bridge.rlp_store directly
```

### 5. Add to health/metrics endpoint

```python
# In whatever method returns health metrics:
metrics["rlp"] = self.rlp_store.metrics()
```

---

## Changes to pact-hh/decision_injector.py

### 1. Import

```python
from pact_hh.rlp_adapter import RLPAdapter
```

### 2. Initialize DecisionInjector with RLPAdapter

```python
# In DecisionInjector.__init__, add optional rlp_adapter param:
def __init__(self, store, bus, trust_network, rlp_adapter=None):
    ...
    self.rlp_adapter = rlp_adapter or RLPAdapter()
```

### 3. Call after injecting decision

Find the section where HUMAN_DECISION is published to the bus and trust scores are updated.
Add immediately after:

```python
# Update rlp-0 relational state
if self.rlp_adapter:
    self.rlp_adapter.on_decision(
        session_id=packet.session_id,
        decision=decision,                        # 'approve' | 'hold' | 'escalate'
        agent_recommendation=packet.recommended_action,
    )
```

### 4. Wire at startup (in loop.py or wherever DecisionInjector is constructed)

```python
from pact_hh.rlp_adapter import RLPAdapter

rlp_adapter = RLPAdapter(rlp_store=bridge.rlp_store)

injector = DecisionInjector(
    store=store,
    bus=bus,
    trust_network=trust_network,
    rlp_adapter=rlp_adapter,
)
```

---

## Data flow after integration

```
Incoming message
      │
      ▼
PACTBridge._process()
      │
      ├─ trust evaluated ──────────────► RLPSession.on_trust_evaluated()
      │                                         │
      ├─ intent translated ────────────► RLPSession.on_intent_translated()
      │                                         │
      ├─ consensus reached/failed ─────► RLPSession.on_consensus_*()
      │                                         │
      ├─ escalation to human ──────────► RLPSession.on_escalation_to_human()
      │                                         │
      │                              rlp-0 tracks relational state
      │                              RUPTURE_DETECTED → CoordinationBus
      │
      ▼
pact-hh HumanEscalationLoop
      │
      ├─ escalation opened ────────────► RLPAdapter.on_escalation_opened()
      │
      ├─ human replies
      │
      ├─ decision parsed + injected
      │
      └─ RLPAdapter.on_decision() ─────► RLPSession.on_human_decision()
                                                │
                                         rlp-0.acknowledge_repair()
                                         gate releases if rupture resolved
                                         CoordinationBus: RLP_RUPTURE_DETECTED cleared
```

---

## Graceful degradation

Both new files degrade gracefully:
- If `rlp-0` is not installed: `RLPSession` runs in passthrough mode — all methods are no-ops, `gate_open()` returns `True`
- If `RLPSessionStore` is not passed to `RLPAdapter`: adapter logs a debug message and returns `False` from all methods
- No existing tests break. No existing behavior changes.

Add `rlp-0` to pact-bridge's optional dependencies:

```toml
# pyproject.toml
[project.optional-dependencies]
rlp = ["rlp-0>=0.1.0"]
full = ["rlp-0>=0.1.0", ...]
```

---

## New CoordinationBus event

`RLP_RUPTURE_DETECTED` payload:
```json
{
  "session_id": "abc123",
  "signal": "RUPTURE_DETECTED",
  "rupture_risk": 0.72,
  "gate_open": false
}
```

Consumers can subscribe to this event to:
- Alert humans monitoring session health
- Trigger automatic audit logging
- Feed into future PACT-SX systemic trust tracking

---

## What's not in scope here

- **pact-ax TrustNetwork → rlp-0 direct sync**: The TrustNetwork already feeds rlp-0 indirectly via pact-bridge's trust evaluation. A tighter sync (TrustNetwork emitting directly to rlp-0) is a future step once this integration is stable.
- **PACT-SX / PACT-GX integration**: These protocols will consume rlp-0 state at the systemic and governance layers. That's the next layer up, not part of this wiring.
- **Persistence**: `RLPSessionStore` is in-memory. Persisting rlp-0 state across restarts follows the same pattern as pact-bridge's existing SessionStore persistence — same storage backend, new key namespace.

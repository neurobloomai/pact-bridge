"""
examples/quickstart.py
───────────────────────
End-to-end pact-bridge demo — no external services needed.

Shows three scenarios:
  1. Basic routing  — Rasa intent → pact translation → in-process agent
  2. Multi-agent consensus  — high-stakes intent voted on by two agents
  3. Multi-turn session  — context preserved across two turns
"""

from pact_bridge import PACTBridge, BridgeConfig
from pact_bridge.agent_registry import AgentCard

# ──────────────────────────────────────────────────────────────────────────────
# 1. Set up the bridge
# ──────────────────────────────────────────────────────────────────────────────

bridge = PACTBridge(
    config=BridgeConfig(
        trust_floor          = 0.3,
        multi_agent_intents  = {"approve_refund"},   # requires consensus
    )
)

# ──────────────────────────────────────────────────────────────────────────────
# 2. Register agents
# ──────────────────────────────────────────────────────────────────────────────

bridge.registry.register(AgentCard(
    agent_id     = "billing-agent",
    platform     = "rasa",
    capabilities = {"billing.lookup", "billing.dispute", "approve_refund"},
    trust_score  = 0.92,
))

bridge.registry.register(AgentCard(
    agent_id     = "compliance-agent",
    platform     = "custom",
    capabilities = {"approve_refund", "policy.check"},
    trust_score  = 0.88,
))

# ──────────────────────────────────────────────────────────────────────────────
# 3. Register in-process handlers
# ──────────────────────────────────────────────────────────────────────────────

def billing_handler(intent, entities, context):
    if intent == "billing.lookup":
        return {
            "message":    f"Found 3 invoices for account {entities.get('account_id', '?')}.",
            "confidence": 0.91,
        }
    if intent == "approve_refund":
        return {
            "decision":   "approve",
            "confidence": 0.85,
            "reasoning":  "Amount within auto-approve threshold.",
        }
    return {"message": f"Handled {intent}."}


def compliance_handler(intent, entities, context):
    if intent == "approve_refund":
        amount = entities.get("amount", 0)
        return {
            "decision":   "approve" if amount < 500 else "escalate",
            "confidence": 0.80,
            "reasoning":  f"Amount {amount} {'within' if amount < 500 else 'exceeds'} policy.",
        }
    return {"message": f"Compliance check passed for {intent}."}


bridge.register_handler("billing-agent",    billing_handler)
bridge.register_handler("compliance-agent", compliance_handler)

# ──────────────────────────────────────────────────────────────────────────────
# Scenario 1: Basic routing — single agent
# ──────────────────────────────────────────────────────────────────────────────
print("\n── Scenario 1: Basic routing ──────────────────────────────")

response = bridge.handle({
    "sender_id":  "user-42",
    "platform":   "rasa",
    "intent":     "billing.lookup",
    "entities":   {"account_id": "ACC-1234"},
})
print(response)
# → {'ok': True, 'intent': 'billing.lookup', 'agent': 'billing-agent', ...}

# ──────────────────────────────────────────────────────────────────────────────
# Scenario 2: Multi-agent consensus for high-stakes intent
# ──────────────────────────────────────────────────────────────────────────────
print("\n── Scenario 2: Consensus routing ─────────────────────────")

response = bridge.handle({
    "sender_id": "agent-supervisor",
    "platform":  "custom",
    "intent":    "approve_refund",
    "entities":  {"amount": 250, "customer_id": "CUST-99"},
})
print(response)
# → consensus reached between billing-agent and compliance-agent
# → {'ok': True, 'result': {'decision': 'approve', 'confidence': ...}}

# ──────────────────────────────────────────────────────────────────────────────
# Scenario 3: Multi-turn session — context preserved
# ──────────────────────────────────────────────────────────────────────────────
print("\n── Scenario 3: Multi-turn session ─────────────────────────")

# Turn 1
r1 = bridge.handle({
    "sender_id":  "user-77",
    "platform":   "rasa",
    "intent":     "billing.lookup",
    "entities":   {"account_id": "ACC-9999"},
    "session_id": "my-session-001",
})
session_id = r1.get("session") or "my-session-001"
print("Turn 1:", r1)

# Turn 2 — same session, context carried forward
r2 = bridge.handle({
    "sender_id":  "user-77",
    "platform":   "rasa",
    "intent":     "billing.dispute",
    "entities":   {"invoice_id": "INV-456"},
    "session_id": session_id,
})
print("Turn 2:", r2)

# ──────────────────────────────────────────────────────────────────────────────
# Bridge health + metrics
# ──────────────────────────────────────────────────────────────────────────────
print("\n── Health ─────────────────────────────────────────────────")
print(bridge.health())

print("\n── Metrics ────────────────────────────────────────────────")
import json
print(json.dumps(bridge.metrics(), indent=2, default=str))

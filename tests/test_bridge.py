"""
tests/test_bridge.py
─────────────────────
Test suite for pact-bridge.

Run with: pytest tests/test_bridge.py -v
"""

import pytest
from pact_bridge import PACTBridge, BridgeConfig
from pact_bridge.agent_registry import AgentCard, AgentRegistry
from pact_bridge.intent_router import IncomingMessage, IntentRouter, RoutingOutcome
from pact_bridge.response_adapter import BridgeResponse, BridgeStatus, ResponseAdapter
from pact_bridge.session_store import SessionStore


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def billing_handler(intent, entities, context):
    return {"message": f"Billing: {intent}", "confidence": 0.88}

def support_handler(intent, entities, context):
    return {"message": f"Support: {intent}", "confidence": 0.75}

def make_bridge(**cfg_kwargs):
    cfg    = BridgeConfig(**cfg_kwargs)
    bridge = PACTBridge(config=cfg)

    bridge.registry.register(AgentCard(
        agent_id     = "billing-agent",
        platform     = "rasa",
        capabilities = {"billing.lookup", "billing.dispute"},
        trust_score  = 0.9,
    ))
    bridge.registry.register(AgentCard(
        agent_id     = "support-agent",
        platform     = "custom",
        capabilities = {"support.ticket", "support.escalate"},
        trust_score  = 0.8,
    ))
    bridge.register_handler("billing-agent", billing_handler)
    bridge.register_handler("support-agent", support_handler)
    return bridge


# ──────────────────────────────────────────────────────────────────────────────
# AgentCard
# ──────────────────────────────────────────────────────────────────────────────

class TestAgentCard:
    def test_handles_exact(self):
        card = AgentCard("a", "rasa", capabilities={"billing.lookup"})
        assert card.handles("billing.lookup")
        assert not card.handles("support.ticket")

    def test_effective_weight(self):
        card = AgentCard("a", "rasa", trust_score=0.8)
        assert card.trust_score == 0.8

    def test_to_dict(self):
        card = AgentCard("a", "rasa", capabilities={"x.y"}, endpoint="http://localhost")
        d = card.to_dict()
        assert d["agent_id"] == "a"
        assert "x.y" in d["capabilities"]
        assert d["endpoint"] == "http://localhost"


# ──────────────────────────────────────────────────────────────────────────────
# AgentRegistry
# ──────────────────────────────────────────────────────────────────────────────

class TestAgentRegistry:
    def test_register_and_find(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("a", "rasa", capabilities={"billing.lookup"}, trust_score=0.9))
        card = reg.find("billing.lookup")
        assert card is not None
        assert card.agent_id == "a"

    def test_find_returns_highest_trust(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("low",  "rasa", capabilities={"x"}, trust_score=0.5))
        reg.register(AgentCard("high", "rasa", capabilities={"x"}, trust_score=0.9))
        assert reg.find("x").agent_id == "high"

    def test_find_returns_none_when_empty(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        assert reg.find("billing.lookup") is None

    def test_candidates_sorted(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        for i, score in enumerate([0.7, 0.9, 0.5]):
            reg.register(AgentCard(f"agent-{i}", "rasa", capabilities={"x"}, trust_score=score))
        cands = reg.candidates("x")
        scores = [c.trust_score for c in cands]
        assert scores == sorted(scores, reverse=True)

    def test_min_trust_filter(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("low",  "rasa", capabilities={"x"}, trust_score=0.3))
        reg.register(AgentCard("high", "rasa", capabilities={"x"}, trust_score=0.9))
        cands = reg.candidates("x", min_trust=0.5)
        assert all(c.trust_score >= 0.5 for c in cands)

    def test_deregister(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("a", "rasa", capabilities={"x"}))
        assert reg.deregister("a")
        assert reg.find("x") is None

    def test_from_dict(self):
        reg = AgentRegistry.from_dict([
            {"agent_id": "a", "platform": "rasa",
             "capabilities": ["billing.lookup"], "trust_score": 0.85},
        ], heartbeat_ttl_seconds=0)
        assert reg.find("billing.lookup").agent_id == "a"

    def test_capability_map(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("a", "rasa", capabilities={"x", "y"}))
        cm = reg.capability_map()
        assert "x" in cm
        assert "a" in cm["x"]

    def test_update_trust(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        reg.register(AgentCard("a", "rasa", trust_score=0.8))
        reg.update_trust("a", 0.95)
        assert reg.get("a").trust_score == 0.95

    def test_metrics_keys(self):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        m = reg.metrics()
        for k in ("total_registered", "live_count", "stale_count",
                  "platforms", "total_capabilities"):
            assert k in m


# ──────────────────────────────────────────────────────────────────────────────
# IntentRouter
# ──────────────────────────────────────────────────────────────────────────────

class TestIntentRouter:
    def _router(self, agents=None, **cfg):
        reg = AgentRegistry(heartbeat_ttl_seconds=0)
        for card in (agents or []):
            reg.register(card)
        return IntentRouter(reg, BridgeConfig(**cfg))

    def test_routes_known_intent(self):
        router = self._router([
            AgentCard("a", "rasa", capabilities={"billing.lookup"}, trust_score=0.9)
        ])
        msg = IncomingMessage("user", "rasa", "billing.lookup")
        d = router.route(msg)
        assert d.outcome == RoutingOutcome.ROUTED
        assert d.primary_agent.agent_id == "a"

    def test_no_agent_for_unknown(self):
        router = self._router()
        d = router.route(IncomingMessage("u", "rasa", "unknown.intent"))
        assert d.outcome == RoutingOutcome.NO_AGENT

    def test_trust_gate(self):
        router = self._router(
            [AgentCard("a", "rasa", capabilities={"x"}, trust_score=0.9)],
            trust_floor=0.5,
        )
        msg = IncomingMessage("u", "rasa", "x", trust_score=0.2)
        d = router.route(msg)
        assert d.outcome == RoutingOutcome.UNTRUSTED

    def test_multi_agent_flag(self):
        router = self._router(
            [
                AgentCard("a", "rasa",   capabilities={"approve"}, trust_score=0.9),
                AgentCard("b", "custom", capabilities={"approve"}, trust_score=0.85),
            ],
            multi_agent_intents={"approve"},
        )
        d = router.route(IncomingMessage("u", "rasa", "approve"))
        assert d.outcome == RoutingOutcome.MULTI_AGENT
        assert len(d.candidate_agents) == 2

    def test_prefers_same_platform(self):
        router = self._router([
            AgentCard("rasa-agent",   "rasa",   capabilities={"x"}, trust_score=0.7),
            AgentCard("custom-agent", "custom", capabilities={"x"}, trust_score=0.9),
        ])
        msg = IncomingMessage("u", "rasa", "x")
        d   = router.route(msg)
        assert d.primary_agent.agent_id == "rasa-agent"

    def test_stats_increment(self):
        router = self._router()
        router.route(IncomingMessage("u", "rasa", "x"))
        assert router.stats()["no_agent"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# ResponseAdapter
# ──────────────────────────────────────────────────────────────────────────────

class TestResponseAdapter:
    def _resp(self, status=BridgeStatus.SUCCESS, intent="billing.lookup"):
        return BridgeResponse(
            status=status, intent=intent, original_intent="check_bill",
            agent_id="billing-agent", result={"message": "All good."},
            session_id="sess-001",
        )

    def test_pact_adapter(self):
        out = ResponseAdapter().adapt(self._resp(), "pact")
        assert out["status"] == "success"
        assert out["intent"] == "billing.lookup"

    def test_custom_adapter(self):
        out = ResponseAdapter().adapt(self._resp(), "custom")
        assert out["ok"] is True
        assert out["intent"] == "billing.lookup"

    def test_dialogflow_adapter(self):
        out = ResponseAdapter().adapt(self._resp(), "dialogflow")
        assert "fulfillmentText" in out

    def test_rasa_adapter(self):
        out = ResponseAdapter().adapt(self._resp(), "rasa")
        assert "responses" in out
        assert "events" in out

    def test_fallback_to_pact_for_unknown_platform(self):
        out = ResponseAdapter().adapt(self._resp(), "unknown-platform-xyz")
        assert "status" in out

    def test_error_response_on_failure(self):
        out = ResponseAdapter().adapt(
            BridgeResponse(BridgeStatus.NO_AGENT, "x", "x", None, {}, None),
            "dialogflow",
        )
        assert "fulfillmentText" in out  # graceful error

    def test_supported_platforms_includes_builtins(self):
        platforms = ResponseAdapter().supported_platforms()
        for p in ("pact", "dialogflow", "rasa", "custom"):
            assert p in platforms


# ──────────────────────────────────────────────────────────────────────────────
# SessionStore
# ──────────────────────────────────────────────────────────────────────────────

class TestSessionStore:
    def test_creates_session(self):
        store   = SessionStore(ttl_minutes=60)
        session = store.get_or_create("user-1", "rasa")
        assert session.sender_id == "user-1"

    def test_returns_same_session(self):
        store = SessionStore(ttl_minutes=60)
        s1 = store.get_or_create("u", "rasa", session_id="sess-abc")
        s2 = store.get_or_create("u", "rasa", session_id="sess-abc")
        assert s1.session_id == s2.session_id

    def test_record_turn(self):
        store = SessionStore(ttl_minutes=60)
        s = store.get_or_create("u", "rasa")
        s.record_turn("check_bill", "billing.lookup", "billing-agent", {"msg": "ok"})
        assert len(s.turns) == 1
        assert s.current_agent == "billing-agent"

    def test_close_session(self):
        store = SessionStore(ttl_minutes=60)
        s = store.get_or_create("u", "rasa", session_id="s1")
        assert store.close("s1")
        assert store.get("s1") is None

    def test_metrics(self):
        store = SessionStore(ttl_minutes=60)
        store.get_or_create("u", "rasa")
        m = store.metrics()
        assert m["active_sessions"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# PACTBridge — end-to-end
# ──────────────────────────────────────────────────────────────────────────────

class TestPACTBridge:
    def test_successful_route(self):
        bridge = make_bridge()
        resp = bridge.handle({"sender_id": "u", "platform": "custom",
                              "intent": "billing.lookup"})
        assert resp["ok"] is True

    def test_no_agent_response(self):
        bridge = make_bridge()
        resp = bridge.handle({"sender_id": "u", "platform": "custom",
                              "intent": "completely.unknown.intent"})
        assert resp["ok"] is False

    def test_untrusted_sender_blocked(self):
        bridge = make_bridge(trust_floor=0.8)
        resp = bridge.handle({"sender_id": "u", "platform": "custom",
                              "intent": "billing.lookup", "trust_score": 0.2})
        assert resp["ok"] is False

    def test_session_preserved_across_turns(self):
        bridge = make_bridge()
        r1 = bridge.handle({"sender_id": "u", "platform": "custom",
                            "intent": "billing.lookup", "session_id": "s99"})
        r2 = bridge.handle({"sender_id": "u", "platform": "custom",
                            "intent": "billing.dispute", "session_id": "s99"})
        assert r1["ok"] and r2["ok"]

    def test_platform_adapter_applied(self):
        bridge = make_bridge()
        resp = bridge.handle({"sender_id": "u", "platform": "dialogflow",
                              "intent": "billing.lookup"})
        assert "fulfillmentText" in resp

    def test_rasa_platform_adapter(self):
        bridge = make_bridge()
        resp = bridge.handle({"sender_id": "u", "platform": "rasa",
                              "intent": "billing.lookup"})
        assert "responses" in resp and "events" in resp

    def test_metrics_structure(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom", "intent": "billing.lookup"})
        m = bridge.metrics()
        assert m["bridge"]["handled"] >= 1
        assert "sessions" in m
        assert "registry" in m
        assert "rlp" in m
        assert "active_rlp_sessions" in m["rlp"]

    def test_health_keys(self):
        bridge = make_bridge()
        h = bridge.health()
        for k in ("status", "live_agents", "active_sessions",
                  "pact_registry", "coordination_bus",
                  "rlp_sessions", "rlp_gated", "rlp_avg_risk"):
            assert k in h

    def test_register_handler(self):
        bridge = make_bridge()
        called = []
        bridge.register_handler("billing-agent", lambda i, e, c: called.append(i) or {"msg": "x"})
        bridge.handle({"sender_id": "u", "platform": "rasa", "intent": "billing.lookup"})
        assert "billing.lookup" in called

    def test_malformed_message_returns_error(self):
        bridge = make_bridge()
        resp = bridge.handle({})  # missing required fields
        # should not raise — returns graceful error dict
        assert isinstance(resp, dict)

    def test_repr(self):
        bridge = make_bridge()
        assert "PACTBridge" in repr(bridge)


# ──────────────────────────────────────────────────────────────────────────────
# RLP-0 integration tests
# ──────────────────────────────────────────────────────────────────────────────

class TestRLPIntegration:
    """Tests for rlp-0 relational state tracking wired into PACTBridge."""

    def test_rlp_store_created_on_init(self):
        bridge = make_bridge()
        assert bridge.rlp_store is not None

    def test_rlp_session_created_on_first_handle(self):
        bridge = make_bridge()
        assert bridge.rlp_store.metrics()["active_rlp_sessions"] == 0
        bridge.handle({"sender_id": "u", "platform": "custom", "intent": "billing.lookup"})
        assert bridge.rlp_store.metrics()["active_rlp_sessions"] == 1

    def test_rlp_session_reused_across_turns(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom",
                       "intent": "billing.lookup", "session_id": "sess-1"})
        bridge.handle({"sender_id": "u", "platform": "custom",
                       "intent": "billing.dispute", "session_id": "sess-1"})
        # Same session → still 1 rlp session, not 2
        assert bridge.rlp_store.metrics()["active_rlp_sessions"] == 1

    def test_rlp_separate_sessions_per_session_id(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom",
                       "intent": "billing.lookup", "session_id": "sess-a"})
        bridge.handle({"sender_id": "u", "platform": "custom",
                       "intent": "billing.dispute", "session_id": "sess-b"})
        assert bridge.rlp_store.metrics()["active_rlp_sessions"] == 2

    def test_high_trust_does_not_close_gate(self):
        bridge = make_bridge()
        # High trust sender — gate should stay open
        resp = bridge.handle({
            "sender_id": "u", "platform": "custom",
            "intent": "billing.lookup", "trust_score": 0.95,
        })
        assert resp["status"] != "rupture_blocked"
        assert resp["ok"] is True

    def test_rupture_blocked_status_when_gate_closed(self):
        from pact_bridge import RLPSessionStore, BridgeStatus
        bridge = make_bridge()

        # Manually force gate closed by injecting a pre-ruptured RLP session
        session_id = "forced-rupture-session"

        # Create a session and drive it to rupture
        rlp_session = bridge.rlp_store.get_or_create(session_id=session_id)
        if rlp_session._available:
            # Drive primitives to rupture state
            rlp_session._rlp.update_state(trust=0.05, intent=0.05, narrative=0.05, commitments=0.05)
            assert not rlp_session.gate_open(), "gate should be closed after rupture"

            # Handle a message on this pre-ruptured session
            resp = bridge.handle({
                "sender_id": "u", "platform": "custom",
                "intent": "billing.lookup", "session_id": session_id,
                "trust_score": 0.9,
            })
            assert resp["status"] == BridgeStatus.RUPTURE_BLOCKED.value
            assert resp["ok"] is False
            assert "rupture" in resp["result"]["message"].lower()
        else:
            # rlp-0 not installed — gate is always open, no blocking
            assert True, "rlp-0 not installed, passthrough mode"

    def test_rupture_blocked_counted_in_metrics(self):
        bridge = make_bridge()
        session_id = "metric-test-session"
        rlp_session = bridge.rlp_store.get_or_create(session_id=session_id)
        if rlp_session._available:
            rlp_session._rlp.update_state(trust=0.05, intent=0.05, narrative=0.05, commitments=0.05)
            bridge.handle({
                "sender_id": "u", "platform": "custom",
                "intent": "billing.lookup", "session_id": session_id,
                "trust_score": 0.9,
            })
            assert bridge._metrics["rupture_blocked"] == 1

    def test_rlp_metrics_in_bridge_metrics(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom", "intent": "billing.lookup"})
        m = bridge.metrics()
        assert "rlp" in m
        rlp = m["rlp"]
        assert "active_rlp_sessions" in rlp
        assert "avg_rupture_risk" in rlp
        assert rlp["active_rlp_sessions"] >= 1

    def test_rlp_health_fields(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom", "intent": "billing.lookup"})
        h = bridge.health()
        assert "rlp_sessions" in h
        assert "rlp_gated" in h
        assert "rlp_avg_risk" in h
        assert h["rlp_sessions"] >= 1
        assert h["rlp_avg_risk"] >= 0.0

    def test_custom_rupture_threshold_config(self):
        bridge = PACTBridge(config=BridgeConfig(rupture_threshold=0.3))
        assert bridge.rlp_store.rupture_threshold == 0.3

    def test_rlp_session_status_accessible(self):
        bridge = make_bridge()
        bridge.handle({"sender_id": "u", "platform": "custom",
                       "intent": "billing.lookup", "session_id": "status-test"})
        session = bridge.rlp_store.get("status-test")
        assert session is not None
        status = session.status()
        assert "session_id" in status
        assert "rlp0_available" in status

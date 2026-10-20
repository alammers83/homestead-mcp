"""The guardrails: tiers, approval gating, the human-only approval path, and auditing."""

import asyncio
import dataclasses
import json
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from helpers import Clock, call, make_app
from homestead.approvals import ApprovalError
from homestead.audit import REDACTED, redact
from homestead.config import ConfigError, load_config, parse_config
from homestead.policy import PolicyError, Tier, effective_tier

EXPECTED_TIERS = {
    "list_activities": Tier.READ,
    "check_conditions": Tier.READ,
    "river_level": Tier.READ,
    "list_pets": Tier.READ,
    "care_status": Tier.READ,
    "log_care": Tier.WRITE,
    "notify_sitter": Tier.APPROVAL,
    "list_pending_approvals": Tier.READ,
    "recent_activity": Tier.READ,
    # outdoor extensions
    "air_quality": Tier.READ,
    "tides": Tier.READ,
    "daylight": Tier.READ,
    "suggest_trails": Tier.READ,
    # pets: vet, weight, supplies, homecoming, sitter packet
    "vet_status": Tier.READ,
    "weight_history": Tier.READ,
    "record_vaccine": Tier.WRITE,
    "log_vet_visit": Tier.WRITE,
    "log_weight": Tier.WRITE,
    "supplies_status": Tier.READ,
    "record_stock": Tier.WRITE,
    "request_reorder": Tier.APPROVAL,
    "homecoming_plan": Tier.READ,
    "check_off_homecoming": Tier.WRITE,
    "sitter_packet": Tier.READ,
    "reminders": Tier.READ,
    "send_sitter_packet": Tier.APPROVAL,
    # garden
    "planting_calendar": Tier.READ,
    "water_today": Tier.READ,
    "log_watering": Tier.WRITE,
    # movies
    "search_movies": Tier.READ,
    "horror_picks": Tier.READ,
    "watchlist": Tier.READ,
    "add_to_watchlist": Tier.WRITE,
    "mark_watched": Tier.WRITE,
    "remove_from_watchlist": Tier.WRITE,
}


class TierRules(unittest.TestCase):
    def test_override_can_tighten(self):
        self.assertEqual(effective_tier("x", Tier.WRITE, Tier.APPROVAL), Tier.APPROVAL)

    def test_override_cannot_loosen(self):
        with self.assertRaises(PolicyError):
            effective_tier("notify_sitter", Tier.APPROVAL, Tier.READ)

    def test_unknown_tier_name_rejected(self):
        with self.assertRaises(PolicyError):
            Tier.parse("yolo")


class ToolInventory(unittest.TestCase):
    """If someone adds a tool, this test forces them to decide its tier on purpose."""

    def test_every_tool_has_the_expected_tier(self):
        app, *_ = make_app()
        actual = {name: t.tier for name, t in app.registry.tools.items()}
        self.assertEqual(actual, EXPECTED_TIERS)

    def test_mcp_annotations_match_tiers(self):
        app, *_ = make_app()
        tools = {t.name: t for t in asyncio.run(app.server.list_tools())}
        self.assertEqual(set(tools), set(EXPECTED_TIERS))
        for name, tier in EXPECTED_TIERS.items():
            self.assertEqual(tools[name].annotations.read_only_hint, tier is Tier.READ, name)

    def test_agent_has_no_way_to_approve(self):
        app, *_ = make_app()
        names = {t.name for t in asyncio.run(app.server.list_tools())}
        self.assertFalse({n for n in names if "approve" in n or "deny" in n})


class ApprovalFlow(unittest.TestCase):
    def test_approval_tool_queues_and_does_not_act(self):
        app, state, *_ = make_app()
        out = call(app, "notify_sitter", {"message": "Can you do Saturday?"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertFalse((state / "outbox.jsonl").exists(), "message sent without approval")
        pending = call(app, "list_pending_approvals")["pending"]
        self.assertEqual([p["id"] for p in pending], [out["request_id"]])

    def test_human_approval_executes_once(self):
        app, state, *_ = make_app()
        rid = call(app, "notify_sitter", {"message": "Can you do Saturday?"})["request_id"]
        req = app.approvals.approve_and_execute(rid, app.registry.execute_approved)
        self.assertEqual(req.status, "executed")
        sent = [json.loads(x) for x in (state / "outbox.jsonl").read_text().splitlines()]
        self.assertEqual(sent[0]["text"], "Can you do Saturday?")
        with self.assertRaises(ApprovalError):
            app.approvals.approve_and_execute(rid, app.registry.execute_approved)

    def test_denied_request_never_runs(self):
        app, state, *_ = make_app()
        rid = call(app, "notify_sitter", {"message": "hi"})["request_id"]
        app.approvals.deny(rid, "not needed")
        with self.assertRaises(ApprovalError):
            app.approvals.approve_and_execute(rid, app.registry.execute_approved)
        self.assertFalse((state / "outbox.jsonl").exists())

    def test_expired_request_cannot_be_approved(self):
        clock = Clock()
        app, *_ = make_app(clock=clock)
        rid = call(app, "notify_sitter", {"message": "hi"})["request_id"]
        clock.advance(hours=25)
        with self.assertRaises(ApprovalError):
            app.approvals.approve_and_execute(rid, app.registry.execute_approved)
        self.assertEqual(call(app, "list_pending_approvals")["pending"], [])

    def test_path_traversal_ids_rejected(self):
        app, *_ = make_app()
        with self.assertRaises(ApprovalError):
            app.approvals.get("../../etc/passwd")

    def test_config_can_tighten_write_tool_to_approval(self):
        cfg = load_config_with_policy({"log_care": "approval"})
        app, state, *_ = make_app(cfg=cfg)
        out = call(app, "log_care", {"pet": "Ripley", "task": "nails"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertFalse((state / "care_log.jsonl").exists())

    def test_config_cannot_loosen(self):
        with self.assertRaises(PolicyError):
            make_app(cfg=load_config_with_policy({"notify_sitter": "read"}))

    def test_policy_for_unknown_tool_rejected(self):
        with self.assertRaises(PolicyError):
            make_app(cfg=load_config_with_policy({"launch_rocket": "approval"}))


class Auditing(unittest.TestCase):
    def test_every_call_is_audited(self):
        app, *_ = make_app()
        call(app, "list_pets")
        call(app, "log_care", {"pet": "Ripley", "task": "nails"})
        call(app, "notify_sitter", {"message": "hi"})
        entries = app.audit.tail(10)
        self.assertEqual([e["tool"] for e in entries], ["list_pets", "log_care", "notify_sitter"])
        self.assertEqual([e["outcome"] for e in entries], ["ok", "ok", "queued"])

    def test_errors_are_audited_and_reach_the_model(self):
        app, *_ = make_app()
        with self.assertRaises(ToolError) as ctx:
            call(app, "log_care", {"pet": "Ghost", "task": "nails"})
        self.assertIn("Unknown pet", str(ctx.exception))
        self.assertEqual(app.audit.tail(1)[0]["outcome"], "error")

    def test_secrets_are_redacted(self):
        out = redact({"api_key": "abc", "nested": {"Authorization": "Bearer x"}, "pet": "Ripley"})
        self.assertEqual(
            out, {"api_key": REDACTED, "nested": {"Authorization": REDACTED}, "pet": "Ripley"}
        )


class ConfigValidation(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = load_config_with_policy({})
        self.assertIn("kayak", cfg.activities)

    def test_unknown_gauge_reference_rejected(self):
        import yaml

        from helpers import EXAMPLE_CONFIG

        raw = yaml.safe_load(EXAMPLE_CONFIG.read_text())
        raw["activities"]["kayak"]["gauge"] = "missing"
        with self.assertRaises(ConfigError):
            parse_config(raw)


def load_config_with_policy(policy: dict[str, str]):
    from helpers import EXAMPLE_CONFIG

    cfg = load_config(EXAMPLE_CONFIG)
    return dataclasses.replace(cfg, policy={k: Tier.parse(v) for k, v in policy.items()})


if __name__ == "__main__":
    unittest.main()

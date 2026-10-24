"""Vet records, weight, supplies, homecoming, the sitter packet, and reminders."""

import json
import unittest
from datetime import UTC, date, datetime, timedelta

from mcp.server.mcpserver.exceptions import ToolError

from helpers import call, make_app
from homestead.config import Supply
from homestead.dates import add_months
from homestead.modules.supplies import estimate
from homestead.modules.vet import vaccine_status, weight_trend

TODAY = date(2026, 10, 17)


class Dates(unittest.TestCase):
    def test_add_months_clamps_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 3, 15), 36), date(2029, 3, 15))


class Vaccines(unittest.TestCase):
    def test_status(self):
        self.assertEqual(vaccine_status(TODAY - timedelta(days=1), TODAY), "overdue")
        self.assertEqual(vaccine_status(TODAY + timedelta(days=20), TODAY), "due soon")
        self.assertEqual(vaccine_status(TODAY + timedelta(days=90), TODAY), "ok")

    def test_record_and_status(self):
        app, *_ = make_app()
        call(
            app,
            "record_vaccine",
            {"pet": "Ripley", "vaccine": "rabies", "given_on": "2023-10-01", "valid_months": 36},
        )
        call(app, "record_vaccine", {"pet": "Ripley", "vaccine": "DHPP", "given_on": "2026-01-10"})
        out = call(app, "vet_status", {"pet": "ripley"})
        by = {v["vaccine"]: v for v in out["vaccines"]}
        self.assertEqual(by["rabies"]["status"], "overdue")
        self.assertEqual(by["DHPP"]["due_on"], "2027-01-10")
        self.assertEqual(out["vet"]["name"], "Example Animal Hospital")

    def test_a_newer_shot_replaces_the_older_one(self):
        app, *_ = make_app()
        call(
            app, "record_vaccine", {"pet": "Ripley", "vaccine": "Rabies", "given_on": "2023-10-01"}
        )
        call(
            app, "record_vaccine", {"pet": "Ripley", "vaccine": "rabies", "given_on": "2026-10-01"}
        )
        vaccines = call(app, "vet_status")["vaccines"]
        self.assertEqual(len(vaccines), 1)
        self.assertEqual(vaccines[0]["status"], "ok")

    def test_future_dates_rejected(self):
        app, *_ = make_app()
        with self.assertRaises(ToolError):
            call(
                app,
                "record_vaccine",
                {"pet": "Ripley", "vaccine": "rabies", "given_on": "2027-01-01"},
            )


class Weight(unittest.TestCase):
    def test_trend_and_flags(self):
        entries = [(date(2026, 7, 25), 60.0), (date(2026, 9, 20), 58.0), (date(2026, 10, 15), 54.5)]
        t = weight_trend(entries, TODAY, (55, 62))
        self.assertEqual(t["latest_lb"], 54.5)
        self.assertEqual(t["change_30d_lb"], -3.5)
        self.assertEqual(t["change_90d_lb"], -5.5)
        self.assertEqual(len(t["flags"]), 2)  # fast drop, and under target

    def test_vet_visit_logs_weight_too(self):
        app, *_ = make_app()
        call(app, "log_vet_visit", {"pet": "Ripley", "reason": "annual exam", "weight_lb": 58.2})
        out = call(app, "weight_history", {"pet": "Ripley"})
        self.assertEqual(out["latest_lb"], 58.2)
        self.assertEqual(call(app, "vet_status")["last_visits"]["Ripley"]["reason"], "annual exam")

    def test_silly_weights_rejected(self):
        app, *_ = make_app()
        with self.assertRaises(ToolError):
            call(app, "log_weight", {"pet": "Ripley", "weight_lb": 0})


class Supplies(unittest.TestCase):
    FOOD = Supply("food", "Food", "cups", 7, low_days=7)

    def test_estimate(self):
        start = datetime(2026, 10, 1, tzinfo=UTC)
        self.assertEqual(estimate(self.FOOD, 140, start, start + timedelta(days=5))["status"], "ok")
        self.assertEqual(
            estimate(self.FOOD, 140, start, start + timedelta(days=15))["status"], "low"
        )
        self.assertEqual(
            estimate(self.FOOD, 140, start, start + timedelta(days=25))["status"], "out"
        )

    def test_count_then_draw_down(self):
        app, _, _, clock = make_app()
        self.assertEqual(call(app, "supplies_status")["items"][0]["status"], "never counted")
        call(app, "record_stock", {"item": "Dog food", "quantity": 105})
        clock.advance(days=10)
        food = next(i for i in call(app, "supplies_status")["items"] if i["item"] == "dog_food")
        self.assertEqual((food["estimated_left"], food["status"]), (35.0, "low"))

    def test_reorder_needs_approval(self):
        app, state, *_ = make_app()
        out = call(app, "request_reorder", {"item": "dog_food", "quantity": 1, "note": "30 lb bag"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertFalse((state / "shopping_list.jsonl").exists())
        req = app.approvals.approve_and_execute(out["request_id"], app.registry.execute_approved)
        self.assertEqual(req.status, "executed")
        row = json.loads((state / "shopping_list.jsonl").read_text())
        self.assertEqual((row["label"], row["where"]), ("Dog food", "30 lb bag, local pet store"))


class Homecoming(unittest.TestCase):
    def test_plan_is_dated_from_arrival(self):
        app, *_ = make_app()
        out = call(app, "homecoming_plan")
        self.assertEqual(out["pet"], "Gamera")
        self.assertEqual(out["current_stage"], "before arrival")
        self.assertEqual(out["plan"][0]["from"], "2026-10-24")
        self.assertTrue(any("Ripley" in step for step in out["plan"][0]["steps"]))
        self.assertTrue(any("Vincent and Mothra" in step for step in out["plan"][1]["steps"]))

    def test_override_arrival_and_current_stage(self):
        app, *_ = make_app()
        out = call(app, "homecoming_plan", {"arrival_date": "2026-10-15"})
        self.assertEqual(out["current_stage"], "Decompression")

    def test_check_off(self):
        app, *_ = make_app()
        total = len(call(app, "homecoming_plan")["checklist"])
        out = call(app, "check_off_homecoming", {"item": "crate"})
        self.assertEqual(out["remaining"], total - 1)
        with self.assertRaises(ToolError):
            call(app, "check_off_homecoming", {"item": "pony"})


class SitterPacket(unittest.TestCase):
    def test_packet_lists_care_due_on_the_trip(self):
        app, *_ = make_app()
        call(app, "log_care", {"pet": "Ripley", "task": "heartworm"})  # due again Nov 16
        out = call(app, "sitter_packet", {"start_date": "2026-11-14", "end_date": "2026-11-20"})
        due = [(c["pet"], c["task"], c["due"]) for c in out["care_schedule"]]
        self.assertIn(("Ripley", "Heartworm chew", "2026-11-16"), due)
        self.assertIn("Leashes by the back door", out["text"])
        self.assertIn("555-0199", out["text"])

    def test_overdue_task_lands_on_day_one(self):
        app, _, _, clock = make_app()
        call(app, "log_care", {"pet": "Ripley", "task": "nails"})
        clock.advance(days=40)
        out = call(app, "sitter_packet", {"start_date": "today", "end_date": "tomorrow"})
        nails = [c for c in out["care_schedule"] if c["task"] == "Nail trim"]
        self.assertEqual(nails[0]["due"], "2026-11-26")

    def test_send_needs_approval_and_uses_the_outbox(self):
        app, state, *_ = make_app()
        out = call(
            app, "send_sitter_packet", {"start_date": "2026-11-01", "end_date": "2026-11-03"}
        )
        self.assertFalse((state / "outbox.jsonl").exists())
        app.approvals.approve_and_execute(out["request_id"], app.registry.execute_approved)
        sent = json.loads((state / "outbox.jsonl").read_text())
        self.assertTrue(sent["text"].startswith("Pet care, Sun Nov 1 to Tue Nov 3"))

    def test_bad_ranges(self):
        app, *_ = make_app()
        with self.assertRaises(ToolError):
            call(app, "sitter_packet", {"start_date": "2026-11-05", "end_date": "2026-11-01"})


class Reminders(unittest.TestCase):
    def test_collects_everything_due(self):
        app, *_ = make_app()
        call(
            app, "record_vaccine", {"pet": "Ripley", "vaccine": "rabies", "given_on": "2025-10-25"}
        )
        call(app, "record_stock", {"item": "dog_food", "quantity": 20})
        out = call(app, "reminders", {"days_ahead": 14})
        self.assertEqual(out["vaccines"][0]["status"], "due soon")
        self.assertEqual(out["supplies"][0]["item"], "dog_food")
        self.assertIn(
            "Crate sized for the new dog, with bedding, in a quiet spot", out["homecoming_prep"]
        )
        self.assertTrue(out["care"])


if __name__ == "__main__":
    unittest.main()

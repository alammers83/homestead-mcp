import unittest

from helpers import call, make_app


def status_of(out, pet, task):
    return next(i for i in out["items"] if i["pet"] == pet and i["task"] == task)


class PackCare(unittest.TestCase):
    def test_everything_starts_never_logged(self):
        app, *_ = make_app()
        out = call(app, "care_status")
        self.assertTrue(out["items"])
        self.assertEqual({i["status"] for i in out["items"]}, {"never logged"})

    def test_logging_then_becoming_overdue(self):
        app, _, _, clock = make_app()
        call(app, "log_care", {"pet": "ripley", "task": "nails", "note": "squirmy"})
        self.assertEqual(
            status_of(call(app, "care_status", {"pet": "Ripley"}), "Ripley", "nails")["status"],
            "ok",
        )
        clock.advance(days=24)
        item = status_of(call(app, "care_status"), "Ripley", "nails")
        self.assertEqual((item["status"], item["days_overdue"]), ("overdue", 3))
        self.assertEqual(call(app, "care_status")["items"][0]["status"], "overdue")

    def test_pet_lookup_by_name_is_case_insensitive(self):
        app, *_ = make_app()
        call(app, "log_care", {"pet": "MOTHRA", "task": "brush"})
        self.assertEqual(status_of(call(app, "care_status"), "Mothra", "brush")["status"], "ok")

    def test_unknown_task_rejected(self):
        from mcp.server.mcpserver.exceptions import ToolError

        app, *_ = make_app()
        with self.assertRaises(ToolError):
            call(app, "log_care", {"pet": "Vincent", "task": "heartworm"})


if __name__ == "__main__":
    unittest.main()

"""Garden watering and planting calendar; the TMDB horror watchlist."""

import os
import unittest
from datetime import date, timedelta
from unittest import mock

from mcp.server.mcpserver.exceptions import ToolError

from helpers import FakeHttp, call, make_app, open_meteo_payload
from homestead.modules.garden import DayWater, watering_decision
from homestead.modules.movies import (
    days_until_halloween,
    decade_range,
    fold_watchlist,
    on_my_services,
)

TODAY = date(2026, 10, 17)
TMDB_ENV = {"HOMESTEAD_TMDB_TOKEN": "tmdb-secret"}


def week(rain=0.0, et0=0.15, tmax=70.0, prob=10, today_rain=0.0, tomorrow=(0.0, 10)):
    days = [DayWater(TODAY - timedelta(days=i), rain, et0, tmax, prob) for i in range(1, 9)]
    days.append(DayWater(TODAY, today_rain, et0, tmax, prob))
    days.append(DayWater(TODAY + timedelta(days=1), tomorrow[0], et0, tmax, tomorrow[1]))
    return days


class Watering(unittest.TestCase):
    def test_dry_week_means_water(self):
        out = watering_decision(week(), TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "YES")
        self.assertAlmostEqual(out["inches"], 1.0)  # ET 1.05 in, capped at 1.0

    def test_recent_soaking_means_no(self):
        out = watering_decision(week(rain=0.4), TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "NO")

    def test_rain_coming_means_wait(self):
        out = watering_decision(week(tomorrow=(0.6, 80)), TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "WAIT")

    def test_unlikely_rain_doesnt_count(self):
        out = watering_decision(week(tomorrow=(0.6, 30)), TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "YES")

    def test_logged_watering_counts(self):
        out = watering_decision(week(), TODAY, 1.0, [(TODAY - timedelta(days=3), 1.0)])
        self.assertEqual(out["verdict"], "NO")

    def test_low_evaporation_lowers_the_need(self):
        # Cool week: ET ~0.35 in, floor is 0.5 in; 0.06 in/day of rain covers it.
        out = watering_decision(week(rain=0.06, et0=0.05), TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "NO")

    def test_hot_and_dry_but_met_target_is_light(self):
        days = week(et0=0.1, tmax=95)
        days = [
            d if d.day != TODAY - timedelta(days=7) else DayWater(d.day, 1.0, 0.1, 95, 0)
            for d in days
        ]
        out = watering_decision(days, TODAY, 1.0, [])
        self.assertEqual(out["verdict"], "LIGHT")

    def test_water_today_tool(self):
        rain = [0.0] * 6 + [15.0, 0.0, 0.0, 0.0]  # 15 mm on the 15th
        app, _, http, _ = make_app(FakeHttp(open_meteo=open_meteo_payload(rain_mm=rain)))
        out = call(app, "water_today")
        self.assertEqual(out["verdict"], "NO")
        self.assertTrue(any("open-meteo" in u for u in http.gets))

    def test_log_watering_validates_beds(self):
        app, *_ = make_app()
        call(app, "log_watering", {"inches": 0.5, "bed": "front"})
        with self.assertRaises(ToolError):
            call(app, "log_watering", {"bed": "moon"})


class Planting(unittest.TestCase):
    def test_garlic_in_september(self):
        app, *_ = make_app()
        out = call(app, "planting_calendar", {"on_date": "2026-09-20"})
        self.assertIn("Garlic", [r["crop"] for r in out["now"]])

    def test_tomatoes_in_february(self):
        app, *_ = make_app()
        out = call(app, "planting_calendar", {"on_date": "2026-02-20", "weeks_ahead": 6})
        now = {(r["crop"], r["action"]) for r in out["now"]}
        self.assertIn(("Tomato", "start indoors"), now)
        self.assertIn(
            ("Basil", "start indoors"), {(r["crop"], r["action"]) for r in out["coming_up"]}
        )

    def test_single_crop_schedule(self):
        app, *_ = make_app()
        out = call(app, "planting_calendar", {"crop": "broccoli"})
        self.assertEqual(len(out["schedule"]), 3)
        with self.assertRaises(ToolError):
            call(app, "planting_calendar", {"crop": "mandrake"})


class MoviesPure(unittest.TestCase):
    def test_decades(self):
        self.assertEqual(decade_range("1980s"), ("1980-01-01", "1989-12-31"))
        self.assertEqual(decade_range("70s"), ("1970-01-01", "1979-12-31"))
        self.assertEqual(decade_range("'00s"), ("2000-01-01", "2009-12-31"))
        with self.assertRaises(ValueError):
            decade_range("1984")

    def test_short_service_names_match_tmdb_names(self):
        mine = ("hulu", "netflix", "hbo", "prime", "peacock")
        for name in [
            "Hulu",
            "Netflix basic with Ads",
            "HBO Max",
            "Amazon Prime Video",
            "Peacock Premium",
        ]:
            self.assertTrue(on_my_services(name, mine), name)
        self.assertFalse(on_my_services("Shudder", mine))
        self.assertFalse(on_my_services("Hulu", ("", " ")))

    def test_halloween_countdown(self):
        self.assertEqual(days_until_halloween(TODAY), 14)
        self.assertEqual(days_until_halloween(date(2026, 11, 1)), 364)

    def test_fold(self):
        events = [
            {"event": "add", "tmdb_id": 1, "movie": {"title": "A"}, "at": "1"},
            {"event": "add", "tmdb_id": 2, "movie": {"title": "B"}, "at": "2"},
            {"event": "watched", "tmdb_id": 1, "rating": 5, "at": "3"},
            {"event": "remove", "tmdb_id": 2, "at": "4"},
        ]
        items = fold_watchlist(events)
        self.assertEqual(list(items), [1])
        self.assertTrue(items[1]["watched"])
        self.assertEqual(items[1]["title"], "A")


class MoviesEndToEnd(unittest.TestCase):
    def test_needs_a_token(self):
        app, *_ = make_app()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOMESTEAD_TMDB_TOKEN", None)
            with self.assertRaises(ToolError) as ctx:
                call(app, "horror_picks")
        self.assertIn("HOMESTEAD_TMDB_TOKEN", str(ctx.exception))

    def test_token_goes_in_a_header_not_the_url(self):
        app, _, http, _ = make_app()
        with mock.patch.dict(os.environ, TMDB_ENV):
            call(app, "search_movies", {"query": "The Thing 1982"})
        self.assertFalse(any("tmdb-secret" in u for u in http.gets))
        self.assertEqual(http.headers[-1]["Authorization"], "Bearer tmdb-secret")

    def test_picks_skip_what_you_have_and_show_streaming(self):
        app, *_ = make_app()
        with mock.patch.dict(os.environ, TMDB_ENV):
            call(app, "add_to_watchlist", {"tmdb_id": 1091})
            call(app, "mark_watched", {"tmdb_id": 948, "rating": 5})
            out = call(app, "horror_picks", {"subgenre": "slasher", "decade": "80s"})
        self.assertEqual([p["title"] for p in out["picks"]], ["A Nightmare on Elm Street"])
        self.assertEqual(out["picks"][0]["where"]["on_your_services"], ["Shudder"])
        self.assertEqual(out["days_until_halloween"], 14)
        self.assertIn("TMDB", out["attribution"])

    def test_watchlist_lifecycle(self):
        app, *_ = make_app()
        with mock.patch.dict(os.environ, TMDB_ENV):
            call(app, "add_to_watchlist", {"tmdb_id": 1091, "note": "party"})
            call(app, "add_to_watchlist", {"tmdb_id": 377})
            call(app, "remove_from_watchlist", {"tmdb_id": 377})
            self.assertEqual(
                [m["title"] for m in call(app, "watchlist")["to_watch"]], ["The Thing"]
            )
            call(app, "mark_watched", {"tmdb_id": 1091, "rating": 4, "note": "the blood test!"})
            out = call(app, "watchlist", {"include_watched": True})
        self.assertEqual(out["to_watch"], [])
        self.assertEqual(out["watched"][0]["rating"], 4)
        with self.assertRaises(ToolError):
            call(app, "remove_from_watchlist", {"tmdb_id": 377})


if __name__ == "__main__":
    unittest.main()

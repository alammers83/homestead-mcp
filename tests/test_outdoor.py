import unittest
from datetime import timedelta

from helpers import NOW, FakeHttp, call, hourly_payload, make_app, usgs_payload
from homestead.config import Activity, Gauge
from homestead.http import UpstreamError
from homestead.modules.outdoor import (
    CAUTION,
    GO,
    NO_GO,
    GaugeReading,
    HourForecast,
    evaluate,
    parse_gauge,
    parse_wind_mph,
)

KAYAK = Activity("kayak", "Kayaking", 55, 92, 12, 30, gauge="g")
WALK = Activity("dog_walk", "Dog walk", 15, 90, 30, 70, with_dog=True)
GAUGE = Gauge("g", "Test River", "USGS-1", "00060", "ft3/s", 15000, 30000, 6)


def hours(temp=70, wind=5, precip=10, n=3):
    return [HourForecast(NOW + timedelta(hours=i), temp, wind, precip, "Sunny") for i in range(n)]


def reading(value=9000, age_hours=0.5):
    return GaugeReading(value, "ft3/s", NOW - timedelta(hours=age_hours), "Test", "Provisional")


class Parsing(unittest.TestCase):
    def test_wind_ranges_use_the_top(self):
        self.assertEqual(parse_wind_mph("5 to 15 mph"), 15)
        self.assertEqual(parse_wind_mph("10 mph"), 10)
        self.assertEqual(parse_wind_mph(""), 0)

    def test_usgs_value_is_a_string_in_the_api(self):
        r = parse_gauge(usgs_payload(value="12345.6"))
        self.assertAlmostEqual(r.value, 12345.6)
        self.assertEqual(r.observed_at.tzinfo is not None, True)

    def test_usgs_empty_or_bad_value(self):
        self.assertIsNone(parse_gauge({"features": []}))
        self.assertIsNone(parse_gauge(usgs_payload(value=None)))


class Decisions(unittest.TestCase):
    def test_clear_day_is_go(self):
        v = evaluate(KAYAK, hours(), dog_heat_limit_f=82, gauge=GAUGE, reading=reading(), now=NOW)
        self.assertEqual(v.status, GO)

    def test_rain_is_no_go(self):
        v = evaluate(KAYAK, hours(precip=60), dog_heat_limit_f=82)
        self.assertEqual(v.status, NO_GO)
        self.assertTrue(any("rain" in r for r in v.reasons))

    def test_rain_near_limit_is_caution(self):
        self.assertEqual(evaluate(KAYAK, hours(precip=25), dog_heat_limit_f=82).status, CAUTION)

    def test_wind_is_no_go(self):
        self.assertEqual(evaluate(KAYAK, hours(wind=20), dog_heat_limit_f=82).status, NO_GO)

    def test_too_hot_for_the_dog(self):
        v = evaluate(WALK, hours(temp=86), dog_heat_limit_f=82)
        self.assertEqual(v.status, NO_GO)
        self.assertTrue(any("dog" in r for r in v.reasons))

    def test_same_heat_is_fine_without_the_dog(self):
        solo = Activity("run", "Run", 20, 90, 25, 60)
        self.assertEqual(evaluate(solo, hours(temp=86), dog_heat_limit_f=82).status, GO)

    def test_high_water_is_no_go(self):
        v = evaluate(
            KAYAK, hours(), dog_heat_limit_f=82, gauge=GAUGE, reading=reading(35000), now=NOW
        )
        self.assertEqual(v.status, NO_GO)

    def test_elevated_water_is_caution(self):
        v = evaluate(
            KAYAK, hours(), dog_heat_limit_f=82, gauge=GAUGE, reading=reading(20000), now=NOW
        )
        self.assertEqual(v.status, CAUTION)

    def test_stale_or_missing_river_data_is_caution_not_go(self):
        stale = evaluate(
            KAYAK, hours(), dog_heat_limit_f=82, gauge=GAUGE, reading=reading(age_hours=12), now=NOW
        )
        missing = evaluate(KAYAK, hours(), dog_heat_limit_f=82, gauge=GAUGE, reading=None, now=NOW)
        self.assertEqual((stale.status, missing.status), (CAUTION, CAUTION))

    def test_no_forecast_hours_is_caution(self):
        self.assertEqual(evaluate(KAYAK, [], dog_heat_limit_f=82).status, CAUTION)

    def test_worst_condition_wins(self):
        v = evaluate(KAYAK, hours(precip=25, wind=20), dog_heat_limit_f=82)
        self.assertEqual(v.status, NO_GO)
        self.assertEqual(len(v.reasons), 2)


class EndToEnd(unittest.TestCase):
    """Through the real MCP server, with the network faked."""

    def test_kayak_check_uses_weather_and_river(self):
        app, _, http, _ = make_app()
        out = call(
            app, "check_conditions", {"activity": "kayak", "start_hour": 9, "duration_hours": 3}
        )
        self.assertEqual(out["verdict"], GO)
        self.assertEqual([h["time"] for h in out["hours"]], ["9am", "10am", "11am"])
        self.assertEqual(out["river"]["value"], 9800)
        self.assertTrue(any("waterdata.usgs.gov" in u for u in http.gets))

    def test_tomorrow_window(self):
        app, *_ = make_app(FakeHttp(hourly=hourly_payload(hours=48)))
        out = call(
            app,
            "check_conditions",
            {"activity": "hike", "day": "tomorrow", "start_hour": 7, "duration_hours": 2},
        )
        self.assertIn("Sun Oct 18", out["window"])
        self.assertEqual(len(out["hours"]), 2)

    def test_usgs_outage_degrades_to_caution(self):
        app, *_ = make_app(FakeHttp(usgs_error=UpstreamError("down")))
        out = call(app, "check_conditions", {"activity": "kayak", "start_hour": 9})
        self.assertEqual(out["verdict"], CAUTION)

    def test_activity_without_gauge_skips_usgs(self):
        app, _, http, _ = make_app()
        call(app, "check_conditions", {"activity": "dog_walk", "start_hour": 9})
        self.assertFalse(any("waterdata" in u for u in http.gets))

    def test_bad_inputs_are_reported_to_the_model(self):
        from mcp.server.mcpserver.exceptions import ToolError

        app, *_ = make_app()
        with self.assertRaises(ToolError):
            call(app, "check_conditions", {"activity": "skydiving"})
        with self.assertRaises(ToolError):
            call(app, "check_conditions", {"activity": "kayak", "day": "next blue moon"})


if __name__ == "__main__":
    unittest.main()

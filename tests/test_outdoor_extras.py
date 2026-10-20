"""Sun, pavement, air quality, tides, and trails."""

import io
import os
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError

from helpers import FakeHttp, airnow_payload, call, hourly_payload, make_app, pollen_payload
from homestead.config import AirQuality, Trail
from homestead.http import UpstreamError, UrllibClient
from homestead.modules import solar
from homestead.modules.airquality import AqiReading, PollenReading, air_findings, parse_airnow
from homestead.modules.outdoor import CAUTION, GO, NO_GO, HourForecast, pavement_findings
from homestead.modules.trails import rank_trails

TZ = ZoneInfo("America/New_York")
DC = (38.9, -77.04)
KEYS = {"HOMESTEAD_AIRNOW_API_KEY": "airnow-secret", "HOMESTEAD_POLLEN_API_KEY": "pollen-secret"}


def local(dt):
    return dt.astimezone(TZ)


class Sun(unittest.TestCase):
    def test_dc_sun_times_match_published_tables(self):
        # Published: Oct 17 2026 sunrise 7:19am, sunset ~6:24pm; Jun 21 5:43am / 8:37pm.
        oct17 = solar.daylight(date(2026, 10, 17), *DC)
        jun21 = solar.daylight(date(2026, 6, 21), *DC)
        for got, (h, m) in [
            (oct17.sunrise, (7, 19)),
            (oct17.sunset, (18, 24)),
            (jun21.sunrise, (5, 43)),
            (jun21.sunset, (20, 37)),
        ]:
            d = local(got).date()
            want = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
            self.assertLess(abs((local(got) - want).total_seconds()), 4 * 60, got)
        self.assertLess(oct17.civil_dawn, oct17.sunrise)
        self.assertGreater(oct17.civil_dusk, oct17.sunset)

    def test_polar_night_has_no_sunrise(self):
        self.assertIsNone(solar.daylight(date(2026, 12, 21), 80, 0).sunrise)

    def test_sun_is_up_at_noon_and_down_at_midnight(self):
        noon = datetime(2026, 10, 17, 13, tzinfo=TZ)
        self.assertGreater(solar.sun_elevation(noon, *DC), 35)
        self.assertLess(solar.sun_elevation(noon + timedelta(hours=11), *DC), 0)


class Pavement(unittest.TestCase):
    def test_summer_afternoon_rule_of_thumb(self):
        # An 87°F sunny July afternoon puts asphalt in the 140s.
        t = solar.pavement_temp_f(87, 72, "Sunny")
        self.assertTrue(135 <= t <= 150, t)

    def test_clouds_and_night_cool_it_down(self):
        self.assertLess(solar.pavement_temp_f(87, 72, "Rain Showers"), 100)
        self.assertEqual(solar.pavement_temp_f(87, -5, "Clear"), 87)

    def test_paw_check_levels(self):
        def hrs(temp):
            start = datetime(2026, 7, 15, 13, tzinfo=TZ)
            return [HourForecast(start, temp, 5, 0, "Sunny")]

        burn, _ = pavement_findings(hrs(88), *DC, 125)
        warm, _ = pavement_findings(hrs(60), *DC, 125)
        cool, temps = pavement_findings(hrs(40), *DC, 125)
        self.assertEqual(burn[0][0], NO_GO)
        self.assertEqual(warm[0][0], CAUTION)
        self.assertEqual(cool, [])
        self.assertEqual(len(temps), 1)


class Daylight(unittest.TestCase):
    def test_kayak_ending_after_dark_is_no_go(self):
        app, *_ = make_app()
        out = call(
            app, "check_conditions", {"activity": "kayak", "start_hour": 17, "duration_hours": 3}
        )
        self.assertEqual(out["verdict"], NO_GO)
        self.assertTrue(any("after sunset" in r for r in out["reasons"]))

    def test_run_after_dark_is_only_caution(self):
        app, *_ = make_app()
        out = call(
            app, "check_conditions", {"activity": "run", "start_hour": 17, "duration_hours": 3}
        )
        self.assertEqual(out["verdict"], CAUTION)
        self.assertTrue(any("bring lights" in r for r in out["reasons"]))

    def test_daylight_tool(self):
        app, *_ = make_app()
        out = call(app, "daylight", {"day": "today"})
        self.assertRegex(out["sunrise"], r"^7:(1|2)\d(am)$")
        self.assertRegex(out["sunset"], r"^6:\d\d(pm)$")


class AirQualityChecks(unittest.TestCase):
    def test_findings(self):
        limits = AirQuality()
        bad = air_findings([AqiReading("O3", 160, "Unhealthy", "forecast")], None, limits)
        meh = air_findings([AqiReading("PM2.5", 110, "USG", "forecast")], None, limits)
        pollen = air_findings(None, [PollenReading("Grass", 4, "High")], limits)
        self.assertEqual([bad[0][0], meh[0][0], pollen[0][0]], [NO_GO, CAUTION, CAUTION])
        self.assertEqual(air_findings(None, None, limits), [])

    def test_category_only_rows_use_the_category_floor(self):
        rows = parse_airnow(airnow_payload(aqi=-1, category=(3, "USG")), "forecast")
        self.assertEqual(rows[0].aqi, 101)

    def test_without_keys_air_is_skipped(self):
        app, _, http, _ = make_app()
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in KEYS:
                os.environ.pop(k, None)
            out = call(app, "check_conditions", {"activity": "hike", "start_hour": 9})
        self.assertEqual(out["air"]["aqi"], "not configured")
        self.assertFalse(any("airnow" in u for u in http.gets))
        self.assertEqual(out["verdict"], GO)

    def test_bad_air_makes_a_hike_no_go(self):
        http = FakeHttp(airnow=airnow_payload(aqi=165, category=(4, "Unhealthy")))
        app, *_ = make_app(http)
        with mock.patch.dict(os.environ, KEYS):
            out = call(app, "check_conditions", {"activity": "hike", "start_hour": 9})
        self.assertEqual(out["verdict"], NO_GO)
        self.assertIn("EPA AirNow", out["sources"])

    def test_high_pollen_is_caution(self):
        app, *_ = make_app(FakeHttp(pollen=pollen_payload(index=5)))
        with mock.patch.dict(os.environ, KEYS):
            out = call(app, "air_quality")
        self.assertTrue(any("pollen" in f for f in out["flags"]))

    def test_airnow_outage_is_caution_not_go(self):
        class Down(FakeHttp):
            def get_json(self, url, params=None, headers=None):
                if "airnow" in url:
                    raise UpstreamError(
                        "https://www.airnowapi.org/aq/forecast/latLong/ returned HTTP 500"
                    )
                return super().get_json(url, params, headers)

        app, *_ = make_app(Down())
        with mock.patch.dict(os.environ, KEYS):
            out = call(app, "check_conditions", {"activity": "hike", "start_hour": 9})
        self.assertEqual(out["verdict"], CAUTION)

    def test_api_keys_never_reach_error_messages(self):
        err = urllib.error.HTTPError(
            "https://www.airnowapi.org/aq/x/?API_KEY=airnow-secret", 403, "no", {}, io.BytesIO()
        )
        with (
            mock.patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(UpstreamError) as ctx,
        ):
            UrllibClient().get_json("https://www.airnowapi.org/aq/x/", {"API_KEY": "airnow-secret"})
        self.assertNotIn("airnow-secret", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)


class Tides(unittest.TestCase):
    def test_tides_tool(self):
        app, *_ = make_app()
        out = call(app, "tides")
        self.assertEqual([t["kind"] for t in out["tides"]], ["high", "low", "high", "low"])
        self.assertEqual(out["tides"][0]["time"], "4:36am")

    def test_kayak_check_includes_tides(self):
        app, *_ = make_app()
        out = call(app, "check_conditions", {"activity": "kayak", "start_hour": 9})
        self.assertEqual(len(out["tides"]["tides"]), 4)

    def test_tides_error_message_reaches_the_model(self):
        app, *_ = make_app(FakeHttp(tides={"error": {"message": "No data was found"}}))
        with self.assertRaises(ToolError):
            call(app, "tides")


class Trails(unittest.TestCase):
    T = [
        Trail("woods", "Woods", 2.0, "high", water=True),
        Trail("river", "River", 3.5, "medium", water=True),
        Trail("meadow", "Meadow", 1.2, "low"),
        Trail("park", "No Dogs Park", 1.0, "high", dogs_allowed=False),
    ]

    def names(self, out):
        return [p["trail"] for p in out["suggestions"]]

    def test_cool_day_allows_everything_dogs_can_use(self):
        out = rank_trails(
            self.T, max_temp_f=60, dog_heat_limit_f=82, with_dog=True, duration_hours=2
        )
        self.assertEqual(set(self.names(out)), {"Woods", "River", "Meadow"})
        self.assertIsNone(out["warning"])

    def test_warm_day_drops_low_shade_and_ranks_shade_first(self):
        out = rank_trails(
            self.T, max_temp_f=78, dog_heat_limit_f=82, with_dog=True, duration_hours=2
        )
        self.assertEqual(self.names(out), ["Woods", "River"])

    def test_too_hot_keeps_only_full_shade_and_warns(self):
        out = rank_trails(
            self.T, max_temp_f=88, dog_heat_limit_f=82, with_dog=True, duration_hours=2
        )
        self.assertEqual(self.names(out), ["Woods"])
        self.assertIn("earlier", out["warning"])

    def test_short_window_skips_long_trails(self):
        out = rank_trails(
            self.T, max_temp_f=60, dog_heat_limit_f=82, with_dog=True, duration_hours=1
        )
        self.assertNotIn("River", self.names(out))

    def test_suggest_trails_tool_uses_the_forecast(self):
        app, *_ = make_app(FakeHttp(hourly=hourly_payload(temp=86)))
        out = call(app, "suggest_trails", {"start_hour": 9, "duration_hours": 2})
        self.assertEqual(out["forecast_high_f"], 86)
        self.assertEqual(self.names(out), ["Example Woods Loop"])


class PawsEndToEnd(unittest.TestCase):
    def test_pavement_walk_reports_paw_estimates(self):
        app, *_ = make_app(FakeHttp(hourly=hourly_payload(temp=80)))
        out = call(
            app, "check_conditions", {"activity": "dog_walk", "start_hour": 12, "duration_hours": 2}
        )
        self.assertIn("pavement_est_f", out["hours"][0])
        self.assertTrue(any("pavement" in r for r in out["reasons"]))

    def test_trail_hike_has_no_paw_check(self):
        app, *_ = make_app()
        out = call(app, "check_conditions", {"activity": "hike", "start_hour": 12})
        self.assertNotIn("pavement_est_f", out["hours"][0])


if __name__ == "__main__":
    unittest.main()

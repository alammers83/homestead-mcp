"""Shared fixtures: a frozen clock, canned NOAA/USGS payloads, and a fake HTTP client."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homestead.config import load_config
from homestead.server import build_app

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config" / "household.example.yaml"

# Saturday 2026-10-17, 08:00 America/New_York (EDT, UTC-4)
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw: float) -> None:
        self.at += timedelta(**kw)


def hourly_payload(temp=65, wind="5 to 10 mph", precip=10, hours=24, start_local_hour=8):
    periods = []
    for i in range(hours):
        hour = (start_local_hour + i) % 24
        day = 17 + (start_local_hour + i) // 24
        periods.append(
            {
                "startTime": f"2026-10-{day:02d}T{hour:02d}:00:00-04:00",
                "temperature": temp(i) if callable(temp) else temp,
                "temperatureUnit": "F",
                "windSpeed": wind,
                "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": precip},
                "shortForecast": "Sunny",
            }
        )
    return {"properties": {"periods": periods}}


def usgs_payload(value="9800", time="2026-10-17T11:45:00+00:00"):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "monitoring_location_id": "USGS-01646500",
                    "monitoring_location_name": "POTOMAC RIVER NEAR WASH, DC LITTLE FALLS PUMP STA",
                    "parameter_code": "00060",
                    "value": value,
                    "unit_of_measure": "ft^3/s",
                    "time": time,
                    "approval_status": "Provisional",
                },
            }
        ],
    }


class FakeHttp:
    def __init__(self, hourly=None, usgs=None, usgs_error: Exception | None = None) -> None:
        self.hourly = hourly or hourly_payload()
        self.usgs = usgs or usgs_payload()
        self.usgs_error = usgs_error
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self.gets.append(url)
        if "/points/" in url:
            return {
                "properties": {
                    "forecastHourly": "https://api.weather.gov/gridpoints/LWX/1,1/forecast/hourly"
                }
            }
        if url.endswith("/forecast/hourly"):
            return self.hourly
        if "waterdata.usgs.gov" in url:
            assert params and params["monitoring_location_id"].startswith("USGS-")
            if self.usgs_error:
                raise self.usgs_error
            return self.usgs
        raise AssertionError(f"unexpected URL {url}")

    def post_json(self, url: str, body: dict) -> int:
        self.posts.append((url, body))
        return 200


def make_app(http: FakeHttp | None = None, clock: Clock | None = None, cfg=None):
    state = Path(tempfile.mkdtemp(prefix="homestead-test-"))
    http = http or FakeHttp()
    clock = clock or Clock()
    app = build_app(cfg or load_config(EXAMPLE_CONFIG), state, http=http, now=clock)
    return app, state, http, clock


def call(app, tool: str, args: dict | None = None) -> dict:
    result = asyncio.run(app.server.call_tool(tool, args or {}))
    return result.structured_content

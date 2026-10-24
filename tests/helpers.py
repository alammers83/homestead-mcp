"""Shared fixtures: a frozen clock, canned API payloads, and a fake HTTP client."""

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


def tides_payload():
    return {
        "predictions": [
            {"t": "2026-10-17 04:36", "v": "2.912", "type": "H"},
            {"t": "2026-10-17 11:02", "v": "0.301", "type": "L"},
            {"t": "2026-10-17 17:10", "v": "3.104", "type": "H"},
            {"t": "2026-10-17 23:30", "v": "0.402", "type": "L"},
        ]
    }


def airnow_payload(aqi=42, category=(1, "Good"), date="2026-10-17"):
    return [
        {
            "DateForecast": f"{date} ",
            "ParameterName": "PM2.5",
            "AQI": aqi,
            "Category": {"Number": category[0], "Name": category[1]},
        }
    ]


def pollen_payload(index=1, day=17):
    return {
        "dailyInfo": [
            {
                "date": {"year": 2026, "month": 10, "day": day},
                "pollenTypeInfo": [
                    {
                        "code": "WEED",
                        "displayName": "Weed",
                        "indexInfo": {"value": index, "category": "Low"},
                    },
                    {"code": "TREE", "displayName": "Tree"},  # out of season: no index
                ],
            }
        ]
    }


def open_meteo_payload(rain_mm=None, et0_mm=3.0, tmax=70.0, prob=10):
    """10 days, 2026-10-09 .. 2026-10-18 (8 past days, today the 17th, tomorrow)."""
    days = [f"2026-10-{d:02d}" for d in range(9, 19)]
    rain = rain_mm if rain_mm is not None else [0.0] * 10
    return {
        "daily": {
            "time": days,
            "precipitation_sum": rain,
            "et0_fao_evapotranspiration": [et0_mm] * 10,
            "temperature_2m_max": [tmax] * 10,
            "precipitation_probability_max": [prob] * 10,
        }
    }


TMDB_MOVIES = {
    1091: {
        "id": 1091,
        "title": "The Thing",
        "release_date": "1982-06-25",
        "vote_average": 8.1,
        "vote_count": 7000,
        "overview": "Antarctic shapeshifter.",
    },
    948: {
        "id": 948,
        "title": "Halloween",
        "release_date": "1978-10-24",
        "vote_average": 7.6,
        "vote_count": 6000,
        "overview": "The night he came home.",
    },
    377: {
        "id": 377,
        "title": "A Nightmare on Elm Street",
        "release_date": "1984-11-09",
        "vote_average": 7.3,
        "vote_count": 5000,
        "overview": "Don't fall asleep.",
    },
}


def tmdb_route(path: str, params: dict[str, Any] | None) -> Any:
    if path == "/search/movie":
        return {"results": [m for m in TMDB_MOVIES.values() if params["query"] in m["title"]]}
    if path == "/search/keyword":
        return {"results": [{"id": 12339, "name": "slasher"}]}
    if path == "/discover/movie":
        assert params["with_genres"] == 27
        return {"results": list(TMDB_MOVIES.values()), "total_pages": 1}
    if path.endswith("/watch/providers"):
        return {"results": {"US": {"flatrate": [{"provider_name": "Shudder"}]}}}
    if path.startswith("/movie/"):
        return TMDB_MOVIES[int(path.split("/")[2])]
    raise AssertionError(f"unexpected TMDB path {path}")


def fake_jwt(claims: dict) -> str:
    import base64
    import json

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


class FakeHttp:
    def __init__(
        self,
        hourly=None,
        usgs=None,
        usgs_error: Exception | None = None,
        airnow=None,
        pollen=None,
        open_meteo=None,
        tides=None,
    ) -> None:
        self.hourly = hourly or hourly_payload()
        self.usgs = usgs or usgs_payload()
        self.usgs_error = usgs_error
        self.airnow = airnow if airnow is not None else airnow_payload()
        self.pollen = pollen or pollen_payload()
        self.open_meteo = open_meteo or open_meteo_payload()
        self.tides = tides or tides_payload()
        self.gets: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.posts: list[tuple[str, dict]] = []

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        self.gets.append(url)
        self.headers.append(headers or {})
        if "api.themoviedb.org" in url:
            assert headers and headers["Authorization"].startswith("Bearer ")
            return tmdb_route(url.split("/3", 1)[1], params)
        if "tidesandcurrents" in url:
            return self.tides
        if "airnowapi.org" in url:
            return self.airnow
        if "pollen.googleapis.com" in url:
            return self.pollen
        if "open-meteo.com" in url:
            return self.open_meteo
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

    google_claims: dict | None = None  # set by OAuth tests: the ID token Google "returns"

    def post_form(self, url: str, form: dict) -> Any:
        self.posts.append((url, form))
        assert url == "https://oauth2.googleapis.com/token"
        assert form["client_secret"] and form["grant_type"] == "authorization_code"
        return {"access_token": "g", "id_token": fake_jwt(self.google_claims or {})}


def make_app(http: FakeHttp | None = None, clock: Clock | None = None, cfg=None, oauth=None):
    state = Path(tempfile.mkdtemp(prefix="homestead-test-"))
    http = http or FakeHttp()
    clock = clock or Clock()
    app = build_app(cfg or load_config(EXAMPLE_CONFIG), state, http=http, now=clock, oauth=oauth)
    return app, state, http, clock


def call(app, tool: str, args: dict | None = None) -> dict:
    result = asyncio.run(app.server.call_tool(tool, args or {}))
    return result.structured_content

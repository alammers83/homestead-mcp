"""Air quality (EPA AirNow) and pollen (Google Pollen API) for the configured location.

Both need a free API key, read from the environment (Secret Manager in prod):
  * HOMESTEAD_AIRNOW_API_KEY  - https://docs.airnowapi.org/account/request/
  * HOMESTEAD_POLLEN_API_KEY  - a Google Cloud API key with the Pollen API enabled
Without a key the check is skipped and the tools say so; it never blocks a verdict.
AirNow has no pollen data, which is why pollen comes from a second source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from homestead.config import AirQuality
from homestead.http import HttpClient

AIRNOW_BASE = "https://www.airnowapi.org/aq"
POLLEN_URL = "https://pollen.googleapis.com/v1/forecast:lookup"
AIRNOW_KEY_ENV = "HOMESTEAD_AIRNOW_API_KEY"
POLLEN_KEY_ENV = "HOMESTEAD_POLLEN_API_KEY"

# Lower AQI bound for each EPA category number; AirNow sometimes reports only the category.
_CATEGORY_FLOOR = {1: 0, 2: 51, 3: 101, 4: 151, 5: 201, 6: 301}


@dataclass(frozen=True)
class AqiReading:
    pollutant: str
    aqi: int
    category: str
    kind: str  # "forecast" | "observed"


@dataclass(frozen=True)
class PollenReading:
    plant: str
    index: int  # Universal Pollen Index 0-5
    category: str


def parse_airnow(
    payload: list[dict[str, Any]], kind: str, day: date | None = None
) -> list[AqiReading]:
    readings = []
    for row in payload or []:
        if day is not None and str(row.get("DateForecast", "")).strip() != day.isoformat():
            continue
        cat = row.get("Category") or {}
        aqi = int(row.get("AQI", -1))
        if aqi < 0:
            aqi = _CATEGORY_FLOOR.get(int(cat.get("Number", 0)), -1)
        if aqi < 0:
            continue
        readings.append(
            AqiReading(str(row.get("ParameterName", "?")), aqi, str(cat.get("Name", "")), kind)
        )
    return readings


def parse_pollen(payload: dict[str, Any], day: date) -> list[PollenReading]:
    for info in payload.get("dailyInfo") or []:
        d = info.get("date") or {}
        if (d.get("year"), d.get("month"), d.get("day")) != (day.year, day.month, day.day):
            continue
        out = []
        for t in info.get("pollenTypeInfo") or []:
            idx = t.get("indexInfo")
            if idx:  # absent out of season
                out.append(
                    PollenReading(
                        str(t.get("displayName", t.get("code", "?"))),
                        int(idx.get("value", 0)),
                        str(idx.get("category", "")),
                    )
                )
        return out
    return []


def air_findings(
    aqi: list[AqiReading] | None, pollen: list[PollenReading] | None, limits: AirQuality
) -> list[tuple[str, str]]:
    """(status, reason) pairs. None means 'not checked'; [] means 'checked, nothing reported'."""
    out: list[tuple[str, str]] = []
    if aqi:
        worst = max(aqi, key=lambda r: r.aqi)
        label = f"air quality {worst.aqi} AQI ({worst.category}, {worst.pollutant}, {worst.kind})"
        if worst.aqi >= limits.no_go_aqi:
            out.append(("NO_GO", label))
        elif worst.aqi >= limits.caution_aqi:
            out.append(("CAUTION", f"{label}; hard on dogs and sensitive lungs"))
    if pollen:
        worst_p = max(pollen, key=lambda r: r.index)
        if worst_p.index >= limits.pollen_caution_index:
            out.append(
                (
                    "CAUTION",
                    f"{worst_p.plant} pollen {worst_p.category.lower()} ({worst_p.index}/5)",
                )
            )
    return out


class AirData:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @staticmethod
    def has_aqi() -> bool:
        return bool(os.environ.get(AIRNOW_KEY_ENV))

    @staticmethod
    def has_pollen() -> bool:
        return bool(os.environ.get(POLLEN_KEY_ENV))

    def aqi(self, lat: float, lon: float, day: date, today: date) -> list[AqiReading]:
        key = os.environ[AIRNOW_KEY_ENV]
        common = {
            "format": "application/json",
            "latitude": lat,
            "longitude": lon,
            "distance": 25,
            "API_KEY": key,
        }
        readings = parse_airnow(
            self.http.get_json(
                f"{AIRNOW_BASE}/forecast/latLong/", {**common, "date": day.isoformat()}
            ),
            "forecast",
            day,
        )
        if day == today:
            readings += parse_airnow(
                self.http.get_json(f"{AIRNOW_BASE}/observation/latLong/current/", common),
                "observed",
            )
        return readings

    def pollen(self, lat: float, lon: float, day: date, today: date) -> list[PollenReading]:
        days_ahead = (day - today).days
        if not 0 <= days_ahead <= 4:
            return []
        payload = self.http.get_json(
            POLLEN_URL,
            {
                "key": os.environ[POLLEN_KEY_ENV],
                "location.latitude": lat,
                "location.longitude": lon,
                "days": days_ahead + 1,
                "plantsDescription": "false",
            },
        )
        return parse_pollen(payload, day)

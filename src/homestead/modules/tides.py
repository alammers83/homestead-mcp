"""High/low tides and current predictions from NOAA CO-OPS (free, no API key).

https://api.tidesandcurrents.noaa.gov/api/prod/  Find station ids at
https://tidesandcurrents.noaa.gov (water levels) and .../noaacurrents (currents).
The tidal Potomac rises and falls twice a day well upriver of Leesylvania, so a
paddle against the ebb can be a lot more work than the wind forecast suggests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any

from homestead.http import HttpClient, UpstreamError

COOPS_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


@dataclass(frozen=True)
class TideEvent:
    at: datetime
    kind: str  # high | low | flood | ebb | slack
    value: float  # feet for tides, knots for currents


def _parse_time(text: str, tz: tzinfo) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


def _check(payload: dict[str, Any]) -> None:
    if "error" in payload:
        raise UpstreamError(f"NOAA tides: {payload['error'].get('message', 'unknown error')}")


def parse_tides(payload: dict[str, Any], tz: tzinfo) -> list[TideEvent]:
    _check(payload)
    return [
        TideEvent(_parse_time(p["t"], tz), "high" if p.get("type") == "H" else "low", float(p["v"]))
        for p in payload.get("predictions") or []
    ]


def parse_currents(payload: dict[str, Any], tz: tzinfo) -> list[TideEvent]:
    _check(payload)
    rows = (payload.get("current_predictions") or {}).get("cp") or []
    return [
        TideEvent(
            _parse_time(r["Time"], tz),
            str(r.get("Type", "")).lower(),
            abs(float(r.get("Velocity_Major", 0))),
        )
        for r in rows
    ]


class TideData:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _get(self, station: str, product: str, interval: str, day: date) -> dict[str, Any]:
        stamp = day.strftime("%Y%m%d")
        return self.http.get_json(
            COOPS_URL,
            {
                "product": product,
                "application": "homestead-mcp",
                "station": station,
                "begin_date": stamp,
                "end_date": stamp,
                "datum": "MLLW",
                "time_zone": "lst_ldt",
                "units": "english",
                "interval": interval,
                "format": "json",
            },
        )

    def tides(self, station: str, day: date, tz: tzinfo) -> list[TideEvent]:
        return parse_tides(self._get(station, "predictions", "hilo", day), tz)

    def currents(self, station: str, day: date, tz: tzinfo) -> list[TideEvent]:
        return parse_currents(self._get(station, "currents_predictions", "MAX_SLACK", day), tz)


def describe(events: list[TideEvent], tz: tzinfo) -> list[dict[str, Any]]:
    unit = {"high": "ft", "low": "ft"}
    return [
        {
            "time": e.at.astimezone(tz).strftime("%-I:%M%p").lower(),
            "kind": e.kind,
            "value": round(e.value, 2),
            "unit": unit.get(e.kind, "knots"),
        }
        for e in events
    ]

"""Outdoor planner: go / caution / no-go for an activity, from NOAA weather and USGS river data.

Data sources (both free, no API key):
  * NOAA National Weather Service API  - https://api.weather.gov
  * USGS Water Data API (OGC API)       - https://api.waterdata.usgs.gov
    (the modern replacement for the legacy waterservices.usgs.gov, which USGS
    is decommissioning in early 2027)

The decision logic (`evaluate`) is a pure function, so it's unit-tested without
any network access.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homestead.config import Activity, Gauge, HouseholdConfig
from homestead.http import HttpClient, UpstreamError

NWS_BASE = "https://api.weather.gov"
USGS_LATEST = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous/items"

GO, CAUTION, NO_GO = "GO", "CAUTION", "NO_GO"
_RANK = {GO: 0, CAUTION: 1, NO_GO: 2}


@dataclass(frozen=True)
class HourForecast:
    start: datetime
    temp_f: float
    wind_mph: float
    precip_pct: float
    summary: str


@dataclass(frozen=True)
class GaugeReading:
    value: float
    unit: str
    observed_at: datetime
    site_name: str
    approval_status: str


@dataclass
class Verdict:
    status: str
    reasons: list[str]

    def escalate(self, status: str, reason: str) -> None:
        if _RANK[status] > _RANK[self.status]:
            self.status = status
        self.reasons.append(f"{status}: {reason}")


# --------------------------------------------------------------------------- parsing


def parse_wind_mph(text: str) -> float:
    """'10 mph' -> 10, '5 to 15 mph' -> 15 (use the top of the range)."""
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return max(numbers) if numbers else 0.0


def _to_f(value: float, unit: str) -> float:
    return value * 9 / 5 + 32 if unit.upper().startswith("C") else value


def parse_hourly(payload: dict[str, Any]) -> list[HourForecast]:
    hours = []
    for p in payload.get("properties", {}).get("periods", []):
        precip = (p.get("probabilityOfPrecipitation") or {}).get("value")
        hours.append(
            HourForecast(
                start=datetime.fromisoformat(p["startTime"]),
                temp_f=_to_f(float(p["temperature"]), p.get("temperatureUnit", "F")),
                wind_mph=parse_wind_mph(p.get("windSpeed", "")),
                precip_pct=float(precip or 0),
                summary=p.get("shortForecast", ""),
            )
        )
    return hours


def parse_gauge(payload: dict[str, Any]) -> GaugeReading | None:
    features = payload.get("features") or []
    if not features:
        return None
    # If several statistics come back, prefer the most recent observation.
    props = max((f["properties"] for f in features), key=lambda p: p.get("time", ""))
    try:
        value = float(props["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return GaugeReading(
        value=value,
        unit=props.get("unit_of_measure", ""),
        observed_at=datetime.fromisoformat(props["time"].replace("Z", "+00:00")),
        site_name=props.get("monitoring_location_name", ""),
        approval_status=props.get("approval_status", ""),
    )


# --------------------------------------------------------------------------- clients


class OutdoorData:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._hourly_url: dict[tuple[float, float], str] = {}

    def hourly_forecast(self, lat: float, lon: float) -> list[HourForecast]:
        key = (round(lat, 4), round(lon, 4))
        if key not in self._hourly_url:
            points = self.http.get_json(f"{NWS_BASE}/points/{key[0]},{key[1]}")
            url = points.get("properties", {}).get("forecastHourly")
            if not url:
                raise UpstreamError("NOAA did not return an hourly forecast URL for this location")
            self._hourly_url[key] = url
        return parse_hourly(self.http.get_json(self._hourly_url[key]))

    def latest_gauge(self, gauge: Gauge) -> GaugeReading | None:
        payload = self.http.get_json(
            USGS_LATEST,
            {
                "f": "json",
                "monitoring_location_id": gauge.site_id,
                "parameter_code": gauge.parameter_code,
            },
        )
        return parse_gauge(payload)


# --------------------------------------------------------------------------- decision logic


def evaluate(
    activity: Activity,
    hours: list[HourForecast],
    *,
    dog_heat_limit_f: float,
    gauge: Gauge | None = None,
    reading: GaugeReading | None = None,
    now: datetime | None = None,
) -> Verdict:
    verdict = Verdict(GO, [])
    if not hours:
        verdict.escalate(CAUTION, "no forecast hours cover that window")
        return verdict

    hottest = max(hours, key=lambda h: h.temp_f)
    coldest = min(hours, key=lambda h: h.temp_f)
    windiest = max(hours, key=lambda h: h.wind_mph)
    wettest = max(hours, key=lambda h: h.precip_pct)

    def at(h: HourForecast) -> str:
        return h.start.strftime("%-I%p").lower()

    if wettest.precip_pct > activity.max_precip_pct:
        verdict.escalate(
            NO_GO,
            f"{wettest.precip_pct:.0f}% chance of rain at {at(wettest)} "
            f"(limit {activity.max_precip_pct:.0f}%)",
        )
    elif wettest.precip_pct > activity.max_precip_pct - 10:
        verdict.escalate(CAUTION, f"rain chance near your limit ({wettest.precip_pct:.0f}%)")

    if windiest.wind_mph > activity.max_wind_mph:
        verdict.escalate(
            NO_GO,
            f"wind up to {windiest.wind_mph:.0f} mph at {at(windiest)} "
            f"(limit {activity.max_wind_mph:.0f})",
        )

    if hottest.temp_f > activity.max_temp_f:
        verdict.escalate(
            NO_GO, f"{hottest.temp_f:.0f}°F at {at(hottest)} (limit {activity.max_temp_f:.0f}°F)"
        )
    if coldest.temp_f < activity.min_temp_f:
        verdict.escalate(
            NO_GO, f"{coldest.temp_f:.0f}°F at {at(coldest)} (minimum {activity.min_temp_f:.0f}°F)"
        )

    if activity.with_dog and hottest.temp_f > dog_heat_limit_f:
        verdict.escalate(
            NO_GO,
            f"too hot for the dog: {hottest.temp_f:.0f}°F (dog limit {dog_heat_limit_f:.0f}°F)",
        )

    if gauge:
        if reading is None:
            verdict.escalate(CAUTION, f"couldn't get a current reading for {gauge.label}")
        else:
            if reading.value > gauge.no_go_above:
                verdict.escalate(
                    NO_GO,
                    f"{gauge.label} at {reading.value:,.0f} {reading.unit} "
                    f"(no-go above {gauge.no_go_above:,.0f})",
                )
            elif reading.value > gauge.caution_above:
                verdict.escalate(
                    CAUTION, f"{gauge.label} elevated at {reading.value:,.0f} {reading.unit}"
                )
            if now and now - reading.observed_at > timedelta(hours=gauge.max_age_hours):
                verdict.escalate(
                    CAUTION, f"river reading is stale (from {reading.observed_at:%b %d %H:%M} UTC)"
                )

    if verdict.status == GO:
        verdict.reasons.append(f"GO: conditions are within your {activity.label.lower()} limits")
    return verdict


def resolve_day(day: str, today: date) -> date:
    word = day.strip().lower()
    if word in {"today", ""}:
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError as exc:
        raise ValueError(f"Use 'today', 'tomorrow', or YYYY-MM-DD (got {day!r})") from exc


# --------------------------------------------------------------------------- tool functions


class OutdoorTools:
    def __init__(self, cfg: HouseholdConfig, data: OutdoorData, now) -> None:
        self.cfg = cfg
        self.data = data
        self.now = now  # callable returning an aware datetime

    def list_activities(self) -> dict[str, Any]:
        """List configured outdoor activities and their go/no-go limits."""
        return {
            "activities": [asdict(a) for a in self.cfg.activities.values()],
            "gauges": [asdict(g) for g in self.cfg.gauges.values()],
        }

    def check_conditions(
        self, activity: str, day: str = "today", start_hour: int = 8, duration_hours: int = 2
    ) -> dict[str, Any]:
        """Decide GO / CAUTION / NO_GO for an activity in a time window.

        Args:
            activity: activity key from list_activities (e.g. "kayak", "dog_walk").
            day: "today", "tomorrow", or a date as YYYY-MM-DD (forecast covers ~7 days).
            start_hour: local start hour, 0-23.
            duration_hours: how long you'll be out, 1-12.
        """
        if activity not in self.cfg.activities:
            raise ValueError(
                f"Unknown activity {activity!r}. Options: {sorted(self.cfg.activities)}"
            )
        if not 0 <= start_hour <= 23 or not 1 <= duration_hours <= 12:
            raise ValueError("start_hour must be 0-23 and duration_hours 1-12")

        act = self.cfg.activities[activity]
        tz = self.cfg.tz
        now = self.now()
        target = resolve_day(day, now.astimezone(tz).date())
        window_start = datetime(target.year, target.month, target.day, start_hour, tzinfo=tz)
        window_end = window_start + timedelta(hours=duration_hours)

        hours = [
            h
            for h in self.data.hourly_forecast(self.cfg.home.lat, self.cfg.home.lon)
            if window_start <= h.start.astimezone(tz) < window_end
        ]

        gauge = self.cfg.gauges.get(act.gauge) if act.gauge else None
        reading = None
        if gauge:
            try:
                reading = self.data.latest_gauge(gauge)
            except UpstreamError:
                reading = None  # evaluate() turns a missing reading into CAUTION

        verdict = evaluate(
            act,
            hours,
            dog_heat_limit_f=self.cfg.dog_heat_limit_f,
            gauge=gauge,
            reading=reading,
            now=now,
        )
        return {
            "activity": act.label,
            "window": f"{window_start:%a %b %d, %-I%p}-{window_end:%-I%p} {tz.key}",
            "verdict": verdict.status,
            "reasons": verdict.reasons,
            "hours": [
                {
                    "time": h.start.astimezone(tz).strftime("%-I%p").lower(),
                    "temp_f": round(h.temp_f),
                    "wind_mph": h.wind_mph,
                    "precip_pct": h.precip_pct,
                    "summary": h.summary,
                }
                for h in hours
            ],
            "river": None
            if reading is None
            else {
                "site": reading.site_name,
                "value": reading.value,
                "unit": reading.unit,
                "observed_at": reading.observed_at.isoformat(),
                "status": reading.approval_status,
            },
            "sources": ["NOAA National Weather Service"]
            + (["USGS Water Data API"] if gauge else []),
            "disclaimer": "Planning aid only. Check conditions on site; you make the call.",
        }

    def river_level(self, gauge: str) -> dict[str, Any]:
        """Latest reading for a configured river gauge (e.g. discharge in ft3/s)."""
        if gauge not in self.cfg.gauges:
            raise ValueError(f"Unknown gauge {gauge!r}. Options: {sorted(self.cfg.gauges)}")
        g = self.cfg.gauges[gauge]
        reading = self.data.latest_gauge(g)
        if reading is None:
            return {"gauge": g.label, "reading": None, "note": "USGS returned no current value"}
        return {
            "gauge": g.label,
            "site": reading.site_name,
            "value": reading.value,
            "unit": reading.unit,
            "observed_at": reading.observed_at.isoformat(),
            "approval_status": reading.approval_status,
            "caution_above": g.caution_above,
            "no_go_above": g.no_go_above,
        }

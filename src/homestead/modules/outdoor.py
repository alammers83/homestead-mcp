"""Outdoor planner: go / caution / no-go for an activity, from weather, river, air and sun.

Data sources:
  * NOAA National Weather Service API  - https://api.weather.gov (no key)
  * USGS Water Data API (OGC API)       - https://api.waterdata.usgs.gov (no key)
    (the modern replacement for the legacy waterservices.usgs.gov, which USGS
    is decommissioning in early 2027)
  * NOAA CO-OPS tides and currents      - see modules/tides.py (no key)
  * EPA AirNow and Google Pollen        - see modules/airquality.py (keys optional)
  * Sunrise/sunset and pavement heat    - computed locally, see modules/solar.py

The decision logic (`evaluate` and the `*_findings` helpers) is pure, so it's
unit-tested without any network access.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from homestead.config import Activity, Gauge, HouseholdConfig
from homestead.dates import resolve_day
from homestead.http import HttpClient, UpstreamError
from homestead.modules import solar
from homestead.modules import tides as coops
from homestead.modules.airquality import AirData, air_findings
from homestead.modules.trails import rank_trails

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
    extra: Iterable[tuple[str, str]] = (),
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

    for status, reason in extra:
        verdict.escalate(status, reason)

    if verdict.status == GO:
        verdict.reasons.append(f"GO: conditions are within your {activity.label.lower()} limits")
    return verdict


def daylight_findings(
    activity: Activity,
    start: datetime,
    end: datetime,
    sun: solar.Daylight,
    tz: tzinfo,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    dark = NO_GO if activity.daylight_only else CAUTION
    lights = "" if activity.daylight_only else "; bring lights"
    if sun.sunset:
        after = (end - sun.sunset).total_seconds() / 60
        when = sun.sunset.astimezone(tz).strftime("%-I:%M%p").lower()
        if after > 0:
            out.append((dark, f"ends {after:.0f} min after sunset ({when}){lights}"))
        elif activity.daylight_only and after > -30:
            out.append((CAUTION, f"ends within 30 min of sunset ({when}); little margin"))
    if sun.sunrise and start < sun.sunrise:
        before = (sun.sunrise - start).total_seconds() / 60
        when = sun.sunrise.astimezone(tz).strftime("%-I:%M%p").lower()
        out.append((dark, f"starts {before:.0f} min before sunrise ({when}){lights}"))
    return out


def pavement_findings(
    hours: list[HourForecast], lat: float, lon: float, paw_limit_f: float
) -> tuple[list[tuple[str, str]], list[float]]:
    """Paw-burn check for pavement walks; returns findings and the estimate per hour."""
    temps = [
        solar.pavement_temp_f(
            h.temp_f, solar.sun_elevation(h.start + timedelta(minutes=30), lat, lon), h.summary
        )
        for h in hours
    ]
    if not temps:
        return [], []
    i = max(range(len(temps)), key=temps.__getitem__)
    hottest, at = temps[i], hours[i].start.strftime("%-I%p").lower()
    if hottest >= paw_limit_f:
        return [
            (
                NO_GO,
                f"pavement est. {hottest:.0f}°F at {at}, hot enough to burn paws "
                f"(limit {paw_limit_f:.0f}°F); use grass or go earlier",
            )
        ], temps
    if hottest >= paw_limit_f - 15:
        return [(CAUTION, f"pavement est. {hottest:.0f}°F at {at}; check with your hand")], temps
    return [], temps


# --------------------------------------------------------------------------- tool functions


class OutdoorTools:
    def __init__(
        self,
        cfg: HouseholdConfig,
        data: OutdoorData,
        now,
        air: AirData | None = None,
        tide_data: coops.TideData | None = None,
    ) -> None:
        self.cfg = cfg
        self.data = data
        self.now = now  # callable returning an aware datetime
        self.air = air
        self.tide_data = tide_data

    # ------------------------------------------------------------------ helpers

    def _today(self) -> date:
        return self.now().astimezone(self.cfg.tz).date()

    def _window(self, day: str, start_hour: int, duration_hours: int) -> tuple[datetime, datetime]:
        if not 0 <= start_hour <= 23 or not 1 <= duration_hours <= 12:
            raise ValueError("start_hour must be 0-23 and duration_hours 1-12")
        target = resolve_day(day, self._today())
        tz = self.cfg.tz
        start = datetime(target.year, target.month, target.day, start_hour, tzinfo=tz)
        return start, start + timedelta(hours=duration_hours)

    def _hours(self, start: datetime, end: datetime) -> list[HourForecast]:
        tz = self.cfg.tz
        return [
            h
            for h in self.data.hourly_forecast(self.cfg.home.lat, self.cfg.home.lon)
            if start <= h.start.astimezone(tz) < end
        ]

    def _air(self, day: date) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        """Findings plus a report; missing keys skip quietly, outages degrade to CAUTION."""
        home, today = self.cfg.home, self._today()
        report: dict[str, Any] = {"aqi": "not configured", "pollen": "not configured"}
        aqi = pollen = None
        findings: list[tuple[str, str]] = []
        if self.air and self.air.has_aqi():
            try:
                aqi = self.air.aqi(home.lat, home.lon, day, today)
                report["aqi"] = [asdict(r) for r in aqi] or "no AirNow data for that day"
            except UpstreamError as exc:
                findings.append((CAUTION, "couldn't get air quality from AirNow"))
                report["aqi"] = f"unavailable: {exc}"
        if self.air and self.air.has_pollen():
            try:
                pollen = self.air.pollen(home.lat, home.lon, day, today)
                report["pollen"] = [asdict(r) for r in pollen] or "no pollen forecast for that day"
            except UpstreamError as exc:
                report["pollen"] = f"unavailable: {exc}"
        return findings + air_findings(aqi, pollen, self.cfg.air_quality), report

    def _tides(self, day: date) -> dict[str, Any]:
        t, tz = self.cfg.tides, self.cfg.tz
        if t is None or self.tide_data is None:
            raise ValueError("No tides section in the household config")
        out: dict[str, Any] = {
            "station": t.label,
            "tides": coops.describe(self.tide_data.tides(t.station, day, tz), tz),
        }
        if t.current_station:
            try:
                out["currents"] = coops.describe(
                    self.tide_data.currents(t.current_station, day, tz), tz
                )
            except UpstreamError as exc:
                out["currents"] = f"unavailable: {exc}"
        return out

    # ------------------------------------------------------------------ tools

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
        act = self.cfg.activities[activity]
        tz, home = self.cfg.tz, self.cfg.home
        now = self.now()
        window_start, window_end = self._window(day, start_hour, duration_hours)
        target = window_start.date()
        hours = self._hours(window_start, window_end)

        sun = solar.daylight(target, home.lat, home.lon)
        extra = daylight_findings(act, window_start, window_end, sun, tz)
        pavement: list[float] = []
        if act.with_dog and act.surface == "pavement":
            paw, pavement = pavement_findings(hours, home.lat, home.lon, self.cfg.paw_limit_f)
            extra += paw
        air_extra, air_report = self._air(target)
        extra += air_extra
        tide_report: Any = None
        if act.show_tides:
            try:
                tide_report = self._tides(target)
            except UpstreamError as exc:
                tide_report = f"unavailable: {exc}"

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
            extra=extra,
        )
        sources = ["NOAA National Weather Service"] + (["USGS Water Data API"] if gauge else [])
        if isinstance(air_report["aqi"], list):
            sources.append("EPA AirNow")
        if isinstance(air_report["pollen"], list):
            sources.append("Google Pollen API")
        if act.show_tides:
            sources.append("NOAA Tides & Currents")
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
                    **({"pavement_est_f": round(pavement[i])} if pavement else {}),
                }
                for i, h in enumerate(hours)
            ],
            "daylight": sun.local(tz),
            "air": air_report,
            **({"tides": tide_report} if act.show_tides else {}),
            "river": None
            if reading is None
            else {
                "site": reading.site_name,
                "value": reading.value,
                "unit": reading.unit,
                "observed_at": reading.observed_at.isoformat(),
                "status": reading.approval_status,
            },
            "sources": sources,
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

    def air_quality(self, day: str = "today") -> dict[str, Any]:
        """Air quality (EPA AirNow) and pollen for the configured location.

        Args:
            day: "today", "tomorrow", or YYYY-MM-DD (forecasts reach a few days out).
        """
        target = resolve_day(day, self._today())
        findings, report = self._air(target)
        return {
            "day": target.isoformat(),
            "location": self.cfg.home.name,
            **report,
            "flags": [f"{s}: {r}" for s, r in findings],
            "limits": asdict(self.cfg.air_quality),
        }

    def tides(self, day: str = "today") -> dict[str, Any]:
        """High and low tides (and current max/slack, if configured) for a day.

        Args:
            day: "today", "tomorrow", or YYYY-MM-DD.
        """
        target = resolve_day(day, self._today())
        return {"day": target.isoformat(), **self._tides(target), "source": "NOAA CO-OPS"}

    def daylight(self, day: str = "today") -> dict[str, Any]:
        """Civil dawn, sunrise, sunset and civil dusk at the configured location.

        Args:
            day: "today", "tomorrow", or YYYY-MM-DD.
        """
        target = resolve_day(day, self._today())
        home = self.cfg.home
        return {
            "day": target.isoformat(),
            "location": home.name,
            **solar.daylight(target, home.lat, home.lon).local(self.cfg.tz),
            "note": "Computed locally; accurate to a few minutes.",
        }

    def suggest_trails(
        self,
        day: str = "today",
        start_hour: int = 8,
        duration_hours: int = 2,
        with_dog: bool = True,
    ) -> dict[str, Any]:
        """Suggest configured trails for a window: dog-friendly, shaded and near water when hot.

        Args:
            day: "today", "tomorrow", or YYYY-MM-DD.
            start_hour: local start hour, 0-23.
            duration_hours: time on the trail, 1-12 (assumes ~2 mph with a dog).
            with_dog: only trails that allow dogs, and apply the dog heat limit.
        """
        if not self.cfg.trails:
            raise ValueError("No trails in the household config")
        start, end = self._window(day, start_hour, duration_hours)
        hours = self._hours(start, end)
        if not hours:
            raise ValueError("The forecast doesn't cover that window (about 7 days ahead)")
        max_temp = max(h.temp_f for h in hours)
        ranked = rank_trails(
            list(self.cfg.trails.values()),
            max_temp_f=max_temp,
            dog_heat_limit_f=self.cfg.dog_heat_limit_f,
            with_dog=with_dog,
            duration_hours=duration_hours,
        )
        return {
            "window": f"{start:%a %b %d, %-I%p}-{end:%-I%p}",
            "forecast_high_f": round(max_temp),
            **ranked,
        }

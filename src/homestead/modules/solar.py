"""Sunrise, sunset, and sun height, computed locally (NOAA solar equations, no network).

Also a rough pavement-temperature estimate for dog paws. Asphalt in full sun runs
far hotter than the air (an 86°F afternoon can put it near 140°F); the boost
scales with how high the sun is and how clear the sky is. It's a rule of thumb,
not a measurement, and the tools say so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

SUNRISE_ZENITH = 90.833  # sun's upper edge on the horizon, with refraction
CIVIL_ZENITH = 96.0  # civil twilight: still light enough to see without a headlamp

# Max asphalt boost over air temperature with a high sun and a clear sky.
PAVEMENT_SUN_BOOST_F = 60.0


def _sun(dt: datetime) -> tuple[float, float]:
    """Solar declination (degrees) and equation of time (minutes) at a UTC instant."""
    jd = dt.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m = math.radians(mean_anom)
    center = (
        math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * m) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * m) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = mean_long + center - 0.00569 - 0.00478 * math.sin(omega)
    obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq += 0.00256 * math.cos(omega)
    decl = math.degrees(math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(obliq / 2)) ** 2
    lng = math.radians(mean_long)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * lng)
        - 2 * ecc * math.sin(m)
        + 4 * ecc * y * math.sin(m) * math.cos(2 * lng)
        - 0.5 * y * y * math.sin(4 * lng)
        - 1.25 * ecc * ecc * math.sin(2 * m)
    )
    return decl, eq_time


def sun_elevation(at: datetime, lat: float, lon: float) -> float:
    """Degrees above the horizon (negative at night)."""
    utc = at.astimezone(UTC)
    decl, eq_time = _sun(utc)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60
    true_solar = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = math.radians(true_solar / 4 - 180)
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    cos_zenith = math.sin(lat_r) * math.sin(decl_r) + math.cos(lat_r) * math.cos(decl_r) * math.cos(
        hour_angle
    )
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def _crossing(day: date, lat: float, lon: float, zenith: float, rising: bool) -> datetime | None:
    noon_guess = datetime(day.year, day.month, day.day, 12, tzinfo=UTC) - timedelta(hours=lon / 15)
    decl, eq_time = _sun(noon_guess)
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    cos_ha = math.cos(math.radians(zenith)) / (math.cos(lat_r) * math.cos(decl_r)) - math.tan(
        lat_r
    ) * math.tan(decl_r)
    if not -1 <= cos_ha <= 1:
        return None  # polar day or night
    ha = math.degrees(math.acos(cos_ha))
    noon_min = 720 - 4 * lon - eq_time
    minutes = noon_min - 4 * ha if rising else noon_min + 4 * ha
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=minutes)


@dataclass(frozen=True)
class Daylight:
    civil_dawn: datetime | None
    sunrise: datetime | None
    sunset: datetime | None
    civil_dusk: datetime | None

    def local(self, tz: tzinfo) -> dict[str, str | None]:
        def fmt(dt: datetime | None) -> str | None:
            return dt.astimezone(tz).strftime("%-I:%M%p").lower() if dt else None

        return {
            "civil_dawn": fmt(self.civil_dawn),
            "sunrise": fmt(self.sunrise),
            "sunset": fmt(self.sunset),
            "civil_dusk": fmt(self.civil_dusk),
        }


def daylight(day: date, lat: float, lon: float) -> Daylight:
    """Sun times for a calendar date at a location (returned in UTC)."""
    return Daylight(
        civil_dawn=_crossing(day, lat, lon, CIVIL_ZENITH, rising=True),
        sunrise=_crossing(day, lat, lon, SUNRISE_ZENITH, rising=True),
        sunset=_crossing(day, lat, lon, SUNRISE_ZENITH, rising=False),
        civil_dusk=_crossing(day, lat, lon, CIVIL_ZENITH, rising=False),
    )


def sky_clearness(summary: str) -> float:
    """0..1 from an NWS short forecast like 'Mostly Sunny' or 'Chance Showers'."""
    text = summary.lower()
    if any(w in text for w in ("rain", "shower", "storm", "drizzle", "snow", "fog")):
        return 0.15
    for words, value in (
        (("mostly cloudy", "considerable cloud"), 0.35),
        (("partly",), 0.6),
        (("mostly sunny", "mostly clear"), 0.85),
        (("sunny", "clear"), 1.0),
        (("cloudy", "overcast"), 0.15),
    ):
        if any(w in text for w in words):
            return value
    return 0.6  # unknown wording: assume some sun


def pavement_temp_f(air_f: float, sun_elevation_deg: float, summary: str) -> float:
    """Estimated asphalt surface temperature."""
    sun = max(0.0, math.sin(math.radians(sun_elevation_deg)))
    return air_f + PAVEMENT_SUN_BOOST_F * sun * sky_clearness(summary)

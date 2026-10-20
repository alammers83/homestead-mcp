"""Garden: a planting calendar from your frost dates, and "water today?" from recent rain.

Rain and evapotranspiration come from Open-Meteo (free, no key, non-commercial use):
https://open-meteo.com. The watering decision is a pure function: last week's rain
plus any watering you logged, against what the garden lost to evaporation, with a
pass if real rain is coming in the next day or so.

Crop windows are typical extension-service timings in weeks relative to the last
spring frost or first fall frost. They're starting points; seed packets win.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from homestead.config import ConfigError, HouseholdConfig
from homestead.dates import past_day, resolve_day
from homestead.http import HttpClient
from homestead.store import JsonlStore

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
MM_PER_INCH = 25.4

# action, anchor ("last" spring frost | "first" fall frost), start week, end week
CROPS: dict[str, tuple[str, list[tuple[str, str, int, int]]]] = {
    "tomato": ("Tomato", [("start indoors", "last", -8, -6), ("transplant", "last", 1, 3)]),
    "pepper": ("Pepper", [("start indoors", "last", -10, -8), ("transplant", "last", 2, 4)]),
    "eggplant": ("Eggplant", [("start indoors", "last", -9, -7), ("transplant", "last", 2, 4)]),
    "basil": ("Basil", [("start indoors", "last", -6, -4), ("transplant", "last", 1, 4)]),
    "lettuce": (
        "Lettuce",
        [("direct sow", "last", -6, -1), ("direct sow (fall)", "first", -10, -6)],
    ),
    "spinach": (
        "Spinach",
        [("direct sow", "last", -6, -3), ("direct sow (fall)", "first", -8, -4)],
    ),
    "peas": ("Peas", [("direct sow", "last", -8, -4)]),
    "kale": ("Kale", [("transplant", "last", -4, -2), ("direct sow (fall)", "first", -12, -8)]),
    "broccoli": (
        "Broccoli",
        [
            ("start indoors", "last", -10, -8),
            ("transplant", "last", -4, -2),
            ("transplant (fall)", "first", -12, -10),
        ],
    ),
    "carrot": ("Carrot", [("direct sow", "last", -4, 2), ("direct sow (fall)", "first", -12, -10)]),
    "radish": ("Radish", [("direct sow", "last", -6, 2), ("direct sow (fall)", "first", -8, -2)]),
    "cilantro": (
        "Cilantro",
        [("direct sow", "last", -3, 2), ("direct sow (fall)", "first", -8, -4)],
    ),
    "onion": ("Onion (sets)", [("plant", "last", -4, -2)]),
    "potato": ("Potato", [("plant seed potatoes", "last", -4, -2)]),
    "beans": ("Bush beans", [("direct sow", "last", 1, 8)]),
    "cucumber": ("Cucumber", [("direct sow", "last", 1, 6)]),
    "squash": ("Squash / zucchini", [("direct sow", "last", 1, 5)]),
    "sunflower": ("Sunflower", [("direct sow", "last", 0, 6)]),
    "garlic": ("Garlic", [("plant cloves", "first", -6, -2)]),
}


@dataclass(frozen=True)
class DayWater:
    day: date
    rain_in: float
    et0_in: float | None
    max_temp_f: float | None
    rain_prob_pct: float | None


def parse_open_meteo(payload: dict[str, Any]) -> list[DayWater]:
    d = payload.get("daily") or {}
    n = len(d.get("time") or [])

    def col(name: str) -> list[Any]:
        return (d.get(name) or [None] * n) + [None] * n

    rain, et0 = col("precipitation_sum"), col("et0_fao_evapotranspiration")
    tmax, prob = col("temperature_2m_max"), col("precipitation_probability_max")
    return [
        DayWater(
            day=date.fromisoformat(d["time"][i]),
            rain_in=(rain[i] or 0) / MM_PER_INCH,
            et0_in=None if et0[i] is None else et0[i] / MM_PER_INCH,
            max_temp_f=tmax[i],
            rain_prob_pct=prob[i],
        )
        for i in range(n)
    ]


def watering_decision(
    days: list[DayWater],
    today: date,
    weekly_inches: float,
    watered: list[tuple[date, float]],
) -> dict[str, Any]:
    by_day = {d.day: d for d in days}
    week = [today - timedelta(days=i) for i in range(1, 8)]
    rain_7d = sum(by_day[d].rain_in for d in week if d in by_day)
    hand_7d = sum(inch for d, inch in watered if today - timedelta(days=7) <= d <= today)
    recent = sum(by_day[d].rain_in for d in week[:2] if d in by_day)
    recent += sum(inch for d, inch in watered if d >= today - timedelta(days=2))

    et0s = [by_day[d].et0_in for d in week if d in by_day and by_day[d].et0_in is not None]
    if len(et0s) >= 5:
        et0_week = sum(et0s) * 7 / len(et0s)
        need = min(max(et0_week, weekly_inches * 0.5), weekly_inches * 1.5)
        need_why = f"evaporation last week ≈ {et0_week:.2f} in, kept within 0.5-1.5x your target"
    else:
        need, need_why = weekly_inches, "your weekly target (no evaporation data)"

    ahead = [by_day[d] for d in (today, today + timedelta(days=1)) if d in by_day]
    likely_rain = sum(d.rain_in for d in ahead if (d.rain_prob_pct or 0) >= 60)
    hot = by_day.get(today) and (by_day[today].max_temp_f or 0) >= 90
    dry_3d = sum(by_day[d].rain_in for d in week[:3] if d in by_day) < 0.05

    supply = rain_7d + hand_7d
    deficit = need - supply
    reasons = [
        f"rain in the last 7 days: {rain_7d:.2f} in",
        f"watering you logged: {hand_7d:.2f} in",
        f"garden needs about {need:.2f} in/week: {need_why}",
    ]
    if recent >= 0.5:
        verdict, amount = "NO", 0.0
        reasons.append(f"{recent:.2f} in in the last 2 days; the soil should still be moist")
    elif likely_rain >= 0.25:
        verdict, amount = "WAIT", 0.0
        reasons.append(f"about {likely_rain:.2f} in of rain is likely today or tomorrow")
    elif deficit >= 0.25:
        verdict, amount = "YES", round(min(deficit, 1.0), 2)
        reasons.append(f"short by {deficit:.2f} in this week")
    elif hot and dry_3d:
        verdict, amount = "LIGHT", 0.25
        reasons.append("90°F+ and dry for 3 days: water containers and new transplants")
    else:
        verdict, amount = "NO", 0.0
        reasons.append("the garden is getting enough")
    return {
        "verdict": verdict,
        "inches": amount,
        "gallons_per_10sqft": round(amount * 6.23, 1),
        "reasons": reasons,
    }


def calendar_rows(crop_keys: list[str], last: date, first: date) -> list[dict[str, Any]]:
    rows = []
    for key in crop_keys:
        label, windows = CROPS[key]
        for action, anchor, w0, w1 in windows:
            base = last if anchor == "last" else first
            rows.append(
                {
                    "crop": label,
                    "action": action,
                    "from": base + timedelta(weeks=w0),
                    "to": base + timedelta(weeks=w1),
                }
            )
    return sorted(rows, key=lambda r: r["from"])


class GardenTools:
    def __init__(self, cfg: HouseholdConfig, state_dir: Path, http: HttpClient, now) -> None:
        self.cfg = cfg
        self.http = http
        self.now = now
        self.watering = JsonlStore(state_dir / "garden_watering.jsonl")
        if cfg.garden:
            unknown = set(cfg.garden.crops) - set(CROPS)
            if unknown:
                raise ConfigError(
                    f"garden.crops: unknown {sorted(unknown)}. Known: {sorted(CROPS)}"
                )

    def _garden(self):
        if self.cfg.garden is None:
            raise ValueError("No garden section in the household config")
        return self.cfg.garden

    def _today(self) -> date:
        return self.now().astimezone(self.cfg.tz).date()

    def planting_calendar(
        self, on_date: str = "today", weeks_ahead: int = 4, crop: str = ""
    ) -> dict[str, Any]:
        """What to start, sow, or transplant now and in the next few weeks, from your frost dates.

        Args:
            on_date: "today" or YYYY-MM-DD.
            weeks_ahead: also list windows opening within this many weeks, 1-26.
            crop: a single crop key for its full-year schedule (e.g. "tomato"); empty for all.
        """
        g = self._garden()
        if not 1 <= weeks_ahead <= 26:
            raise ValueError("weeks_ahead must be 1-26")
        when = resolve_day(on_date, self._today())
        if crop:
            if crop not in CROPS:
                raise ValueError(f"Unknown crop {crop!r}. Options: {sorted(CROPS)}")
            keys = [crop]
        else:
            keys = list(g.crops) or list(CROPS)
        last = g.last_frost.replace(year=when.year)
        first = g.first_frost.replace(year=when.year)
        rows = calendar_rows(keys, last, first)
        horizon = when + timedelta(weeks=weeks_ahead)

        def fmt(r: dict[str, Any]) -> dict[str, Any]:
            return {**r, "from": r["from"].isoformat(), "to": r["to"].isoformat()}

        out: dict[str, Any] = {
            "date": when.isoformat(),
            "frost_dates": {"last_spring": last.isoformat(), "first_fall": first.isoformat()},
        }
        if crop:
            out["schedule"] = [fmt(r) for r in rows]
        else:
            out["now"] = [fmt(r) for r in rows if r["from"] <= when <= r["to"]]
            out["coming_up"] = [fmt(r) for r in rows if when < r["from"] <= horizon]
        out["note"] = "Typical windows for your frost dates; check the seed packet."
        return out

    def water_today(self) -> dict[str, Any]:
        """Should I water the garden today? Uses last week's rain, evaporation, and the forecast."""
        g = self._garden()
        home = self.cfg.home
        payload = self.http.get_json(
            OPEN_METEO,
            {
                "latitude": home.lat,
                "longitude": home.lon,
                "daily": "precipitation_sum,et0_fao_evapotranspiration,"
                "temperature_2m_max,precipitation_probability_max",
                "temperature_unit": "fahrenheit",
                "timezone": self.cfg.timezone,
                "past_days": 8,
                "forecast_days": 2,
            },
        )
        today = self._today()
        watered = [
            (date.fromisoformat(e["on"]), float(e["inches"])) for e in self.watering.entries()
        ]
        decision = watering_decision(parse_open_meteo(payload), today, g.weekly_inches, watered)
        return {
            "date": today.isoformat(),
            "location": home.name,
            **decision,
            "source": "Open-Meteo",
        }

    def log_watering(
        self, inches: float = 0.5, bed: str = "", watered_on: str = "today"
    ) -> dict[str, Any]:
        """Record that you watered, so water_today counts it.

        Args:
            inches: roughly how much (0.5 in ≈ 3 gallons per 10 sq ft).
            bed: optional bed name from the config.
            watered_on: "today", "yesterday", or YYYY-MM-DD.
        """
        g = self._garden()
        if not 0 < inches <= 3:
            raise ValueError("inches must be between 0 and 3")
        if bed and g.beds and bed not in g.beds:
            raise ValueError(f"Unknown bed {bed!r}. Options: {list(g.beds)}")
        on = past_day(watered_on, self._today())
        entry = {
            "on": on.isoformat(),
            "inches": float(inches),
            "bed": bed,
            "at": self.now().isoformat(),
        }
        return {"recorded": self.watering.append(entry)}

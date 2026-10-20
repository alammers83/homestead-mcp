"""Household configuration: version-controlled YAML, validated at startup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from homestead.policy import Tier


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Gauge:
    key: str
    label: str
    site_id: str
    parameter_code: str
    unit: str
    caution_above: float
    no_go_above: float
    max_age_hours: float = 6


@dataclass(frozen=True)
class Activity:
    key: str
    label: str
    min_temp_f: float
    max_temp_f: float
    max_wind_mph: float
    max_precip_pct: float
    with_dog: bool = False
    gauge: str | None = None
    daylight_only: bool = False  # ending after sunset is NO_GO, not just CAUTION
    surface: str = "trail"  # trail | pavement; pavement gets a paw-temperature check
    show_tides: bool = False


@dataclass(frozen=True)
class CareTask:
    key: str
    label: str
    every_days: int


@dataclass(frozen=True)
class Pet:
    key: str
    name: str
    species: str
    care: tuple[CareTask, ...]
    target_weight_lb: tuple[float, float] | None = None
    feeding: str = ""  # free text for the sitter packet, e.g. "2 cups AM, 2 cups PM"
    notes: str = ""


@dataclass(frozen=True)
class Sitter:
    name: str
    channel: str = "outbox"  # outbox | webhook
    house_notes: str = ""  # alarm, keys, where the leashes are; goes into the sitter packet


@dataclass(frozen=True)
class AirQuality:
    caution_aqi: int = 101  # "Unhealthy for Sensitive Groups"
    no_go_aqi: int = 151  # "Unhealthy"
    pollen_caution_index: int = 4  # Universal Pollen Index, 0-5


@dataclass(frozen=True)
class Tides:
    station: str  # NOAA CO-OPS water level station id
    label: str
    current_station: str | None = None  # NOAA current prediction station id


@dataclass(frozen=True)
class Trail:
    key: str
    name: str
    miles: float
    shade: str  # high | medium | low
    dogs_allowed: bool = True
    water: bool = False  # creek or river access to cool off
    surface: str = "dirt"
    notes: str = ""


@dataclass(frozen=True)
class Vet:
    name: str
    phone: str = ""
    emergency: str = ""  # 24h emergency clinic, name and phone


@dataclass(frozen=True)
class Supply:
    key: str
    label: str
    unit: str
    daily_use: float
    low_days: float = 7
    pets: tuple[str, ...] = ()
    reorder_note: str = ""  # where you usually buy it


@dataclass(frozen=True)
class Homecoming:
    pet: str
    arrival: date | None
    resident_dogs: tuple[str, ...] = ()
    resident_cats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Garden:
    last_frost: date  # only month/day are used
    first_frost: date
    weekly_inches: float = 1.0
    crops: tuple[str, ...] = ()  # keys from the built-in crop table; empty = all
    beds: tuple[str, ...] = ()


@dataclass(frozen=True)
class Movies:
    region: str = "US"
    min_votes: int = 300
    services: tuple[str, ...] = ()  # streaming services you have, as TMDB names them


@dataclass(frozen=True)
class HouseholdConfig:
    timezone: str
    home: Location
    dog_heat_limit_f: float
    gauges: dict[str, Gauge]
    activities: dict[str, Activity]
    pets: dict[str, Pet]
    sitter: Sitter
    policy: dict[str, Tier] = field(default_factory=dict)
    paw_limit_f: float = 125
    air_quality: AirQuality = field(default_factory=AirQuality)
    tides: Tides | None = None
    trails: dict[str, Trail] = field(default_factory=dict)
    vet: Vet | None = None
    supplies: dict[str, Supply] = field(default_factory=dict)
    homecoming: Homecoming | None = None
    garden: Garden | None = None
    movies: Movies = field(default_factory=Movies)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] is None:
        raise ConfigError(f"{where}: missing required field {key!r}")
    return data[key]


def _month_day(value: Any, where: str) -> date:
    """'04-15' -> a date in a fixed leap year; only month and day matter."""
    try:
        month, day = (int(x) for x in str(value).split("-"))
        if (month, day) == (2, 29):
            raise ValueError("Feb 29 doesn't exist every year")
        return date(2000, month, day)
    except ValueError as exc:
        raise ConfigError(f"{where}: use MM-DD (got {value!r})") from exc


def _choice(value: Any, options: set[str], where: str) -> str:
    value = str(value)
    if value not in options:
        raise ConfigError(f"{where}: must be one of {sorted(options)} (got {value!r})")
    return value


def parse_config(raw: dict[str, Any]) -> HouseholdConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    tz = str(_require(raw, "timezone", "config"))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown timezone {tz!r}") from exc

    home_raw = _require(raw, "home", "config")
    home = Location(
        name=str(_require(home_raw, "name", "home")),
        lat=float(_require(home_raw, "lat", "home")),
        lon=float(_require(home_raw, "lon", "home")),
    )

    gauges: dict[str, Gauge] = {}
    for key, g in (raw.get("gauges") or {}).items():
        where = f"gauges.{key}"
        gauge = Gauge(
            key=key,
            label=str(_require(g, "label", where)),
            site_id=str(_require(g, "site_id", where)),
            parameter_code=str(_require(g, "parameter_code", where)),
            unit=str(g.get("unit", "")),
            caution_above=float(_require(g, "caution_above", where)),
            no_go_above=float(_require(g, "no_go_above", where)),
            max_age_hours=float(g.get("max_age_hours", 6)),
        )
        if gauge.caution_above > gauge.no_go_above:
            raise ConfigError(f"{where}: caution_above must be <= no_go_above")
        gauges[key] = gauge

    activities: dict[str, Activity] = {}
    for key, a in (raw.get("activities") or {}).items():
        where = f"activities.{key}"
        act = Activity(
            key=key,
            label=str(_require(a, "label", where)),
            min_temp_f=float(_require(a, "min_temp_f", where)),
            max_temp_f=float(_require(a, "max_temp_f", where)),
            max_wind_mph=float(_require(a, "max_wind_mph", where)),
            max_precip_pct=float(_require(a, "max_precip_pct", where)),
            with_dog=bool(a.get("with_dog", False)),
            gauge=a.get("gauge"),
            daylight_only=bool(a.get("daylight_only", False)),
            surface=_choice(a.get("surface", "trail"), {"trail", "pavement"}, f"{where}.surface"),
            show_tides=bool(a.get("show_tides", False)),
        )
        if act.gauge and act.gauge not in gauges:
            raise ConfigError(f"{where}: references unknown gauge {act.gauge!r}")
        activities[key] = act

    pets: dict[str, Pet] = {}
    for key, p in (raw.get("pets") or {}).items():
        where = f"pets.{key}"
        care = tuple(
            CareTask(
                key=str(_require(c, "task", f"{where}.care")),
                label=str(c.get("label") or c["task"]),
                every_days=int(_require(c, "every_days", f"{where}.care")),
            )
            for c in (p.get("care") or [])
        )
        target = p.get("target_weight_lb")
        if target is not None and (len(target) != 2 or float(target[0]) > float(target[1])):
            raise ConfigError(f"{where}.target_weight_lb: use [low, high]")
        pets[key] = Pet(
            key=key,
            name=str(_require(p, "name", where)),
            species=str(_require(p, "species", where)),
            care=care,
            target_weight_lb=(float(target[0]), float(target[1])) if target else None,
            feeding=str(p.get("feeding", "")),
            notes=str(p.get("notes", "")),
        )

    sitter_raw = raw.get("sitter") or {"name": "Pet sitter"}
    sitter = Sitter(
        name=str(sitter_raw.get("name", "Pet sitter")),
        channel=str(sitter_raw.get("channel", "outbox")),
        house_notes=str(sitter_raw.get("house_notes", "")),
    )
    if sitter.channel not in {"outbox", "webhook"}:
        raise ConfigError("sitter.channel must be 'outbox' or 'webhook'")

    policy = {str(tool): Tier.parse(str(tier)) for tool, tier in (raw.get("policy") or {}).items()}

    aq_raw = raw.get("air_quality") or {}
    air_quality = AirQuality(
        caution_aqi=int(aq_raw.get("caution_aqi", 101)),
        no_go_aqi=int(aq_raw.get("no_go_aqi", 151)),
        pollen_caution_index=int(aq_raw.get("pollen_caution_index", 4)),
    )
    if air_quality.caution_aqi > air_quality.no_go_aqi:
        raise ConfigError("air_quality: caution_aqi must be <= no_go_aqi")

    tides = None
    if raw.get("tides"):
        t = raw["tides"]
        tides = Tides(
            station=str(_require(t, "station", "tides")),
            label=str(t.get("label", "Tides")),
            current_station=str(t["current_station"]) if t.get("current_station") else None,
        )
    if any(a.show_tides for a in activities.values()) and tides is None:
        raise ConfigError("An activity sets show_tides, but there is no tides section")

    trails: dict[str, Trail] = {}
    for key, t in (raw.get("trails") or {}).items():
        where = f"trails.{key}"
        trails[key] = Trail(
            key=key,
            name=str(_require(t, "name", where)),
            miles=float(_require(t, "miles", where)),
            shade=_choice(_require(t, "shade", where), {"high", "medium", "low"}, f"{where}.shade"),
            dogs_allowed=bool(t.get("dogs_allowed", True)),
            water=bool(t.get("water", False)),
            surface=str(t.get("surface", "dirt")),
            notes=str(t.get("notes", "")),
        )

    vet = None
    if raw.get("vet"):
        v = raw["vet"]
        vet = Vet(
            name=str(_require(v, "name", "vet")),
            phone=str(v.get("phone", "")),
            emergency=str(v.get("emergency", "")),
        )

    supplies: dict[str, Supply] = {}
    for key, s_raw in (raw.get("supplies") or {}).items():
        where = f"supplies.{key}"
        supply = Supply(
            key=key,
            label=str(_require(s_raw, "label", where)),
            unit=str(_require(s_raw, "unit", where)),
            daily_use=float(_require(s_raw, "daily_use", where)),
            low_days=float(s_raw.get("low_days", 7)),
            pets=tuple(str(x) for x in (s_raw.get("pets") or [])),
            reorder_note=str(s_raw.get("reorder_note", "")),
        )
        if supply.daily_use <= 0:
            raise ConfigError(f"{where}.daily_use must be positive")
        unknown = set(supply.pets) - set(pets)
        if unknown:
            raise ConfigError(f"{where}: unknown pets {sorted(unknown)}")
        supplies[key] = supply

    homecoming = None
    if raw.get("homecoming"):
        h = raw["homecoming"]
        arrival = h.get("arrival")
        homecoming = Homecoming(
            pet=str(_require(h, "pet", "homecoming")),
            arrival=date.fromisoformat(str(arrival)) if arrival else None,
            resident_dogs=tuple(str(x) for x in (h.get("resident_dogs") or [])),
            resident_cats=tuple(str(x) for x in (h.get("resident_cats") or [])),
        )
        unknown = {homecoming.pet, *homecoming.resident_dogs, *homecoming.resident_cats} - set(pets)
        if unknown:
            raise ConfigError(f"homecoming: unknown pets {sorted(unknown)}")

    garden = None
    if raw.get("garden"):
        g = raw["garden"]
        garden = Garden(
            last_frost=_month_day(_require(g, "last_frost", "garden"), "garden.last_frost"),
            first_frost=_month_day(_require(g, "first_frost", "garden"), "garden.first_frost"),
            weekly_inches=float(g.get("weekly_inches", 1.0)),
            crops=tuple(str(x) for x in (g.get("crops") or [])),
            beds=tuple(str(x) for x in (g.get("beds") or [])),
        )

    m = raw.get("movies") or {}
    movies = Movies(
        region=str(m.get("region", "US")),
        min_votes=int(m.get("min_votes", 300)),
        services=tuple(str(x) for x in (m.get("services") or [])),
    )

    return HouseholdConfig(
        timezone=tz,
        home=home,
        dog_heat_limit_f=float(raw.get("dog_heat_limit_f", 85)),
        gauges=gauges,
        activities=activities,
        pets=pets,
        sitter=sitter,
        policy=policy,
        paw_limit_f=float(raw.get("paw_limit_f", 125)),
        air_quality=air_quality,
        tides=tides,
        trails=trails,
        vet=vet,
        supplies=supplies,
        homecoming=homecoming,
        garden=garden,
        movies=movies,
    )


def load_config(path: str | Path) -> HouseholdConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file {path} not found. Copy config/household.example.yaml to "
            "config/household.yaml (git-ignored) and edit it."
        )
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")))

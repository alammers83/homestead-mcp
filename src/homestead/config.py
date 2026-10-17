"""Household configuration: version-controlled YAML, validated at startup."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Sitter:
    name: str
    channel: str = "outbox"  # outbox | webhook


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

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] is None:
        raise ConfigError(f"{where}: missing required field {key!r}")
    return data[key]


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
        pets[key] = Pet(
            key=key,
            name=str(_require(p, "name", where)),
            species=str(_require(p, "species", where)),
            care=care,
        )

    sitter_raw = raw.get("sitter") or {"name": "Pet sitter"}
    sitter = Sitter(
        name=str(sitter_raw.get("name", "Pet sitter")),
        channel=str(sitter_raw.get("channel", "outbox")),
    )
    if sitter.channel not in {"outbox", "webhook"}:
        raise ConfigError("sitter.channel must be 'outbox' or 'webhook'")

    policy = {str(tool): Tier.parse(str(tier)) for tool, tier in (raw.get("policy") or {}).items()}

    return HouseholdConfig(
        timezone=tz,
        home=home,
        dog_heat_limit_f=float(raw.get("dog_heat_limit_f", 85)),
        gauges=gauges,
        activities=activities,
        pets=pets,
        sitter=sitter,
        policy=policy,
    )


def load_config(path: str | Path) -> HouseholdConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file {path} not found. Copy config/household.example.yaml to "
            "config/household.yaml (git-ignored) and edit it."
        )
    return parse_config(yaml.safe_load(path.read_text(encoding="utf-8")))

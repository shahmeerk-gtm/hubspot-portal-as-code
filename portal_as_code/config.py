"""Load portal.yaml and check it before anything touches HubSpot.

Validation runs locally and fails fast with every problem listed at once,
so a typo never turns into a half-applied portal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SUPPORTED_OBJECTS = ("contacts", "deals")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# HubSpot type -> field types it allows.
VALID_FIELD_TYPES = {
    "string": {"text", "textarea", "phonenumber"},
    "number": {"number"},
    "date": {"date"},
    "datetime": {"date"},
    "bool": {"booleancheckbox"},
    "enumeration": {"select", "radio", "checkbox"},
}


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid config:\n  - " + "\n  - ".join(problems))


@dataclass
class Option:
    label: str
    value: str
    display_order: int


@dataclass
class PropertyGroup:
    object_type: str
    name: str
    label: str
    display_order: int = -1


@dataclass
class Property:
    object_type: str
    name: str
    label: str
    group: str
    type: str
    field_type: str
    description: str = ""
    unique: bool = False
    options: list[Option] = field(default_factory=list)


@dataclass
class Stage:
    label: str
    probability: float
    display_order: int


@dataclass
class Pipeline:
    object_type: str
    label: str
    stages: list[Stage]
    display_order: int = 0
    id: str | None = None


@dataclass
class PortalConfig:
    groups: list[PropertyGroup]
    properties: list[Property]
    pipelines: list[Pipeline]


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _parse_options(raw, where: str, problems: list[str]) -> list[Option]:
    options: list[Option] = []
    for i, item in enumerate(raw or []):
        if isinstance(item, str):
            options.append(Option(label=item, value=slugify(item), display_order=i))
        elif isinstance(item, dict) and "label" in item:
            options.append(
                Option(
                    label=str(item["label"]),
                    value=str(item.get("value", slugify(str(item["label"])))),
                    display_order=i,
                )
            )
        else:
            problems.append(f"{where}: option #{i + 1} must be a string or have a label")
    values = [o.value for o in options]
    dupes = sorted({v for v in values if values.count(v) > 1})
    if dupes:
        problems.append(f"{where}: duplicate option values {dupes}")
    return options


def parse_config(data: dict) -> PortalConfig:
    problems: list[str] = []
    data = data or {}

    groups: list[PropertyGroup] = []
    for obj, items in (data.get("property_groups") or {}).items():
        if obj not in SUPPORTED_OBJECTS:
            problems.append(f"property_groups.{obj}: unsupported object (use {SUPPORTED_OBJECTS})")
            continue
        for item in items or []:
            name = item.get("name", "")
            if not NAME_RE.match(name):
                problems.append(f"property_groups.{obj}: bad group name {name!r}")
            groups.append(
                PropertyGroup(obj, name, item.get("label", name), int(item.get("display_order", -1)))
            )

    group_names = {(g.object_type, g.name) for g in groups}

    properties: list[Property] = []
    for obj, items in (data.get("properties") or {}).items():
        if obj not in SUPPORTED_OBJECTS:
            problems.append(f"properties.{obj}: unsupported object (use {SUPPORTED_OBJECTS})")
            continue
        seen: set[str] = set()
        for item in items or []:
            name = item.get("name", "")
            where = f"properties.{obj}.{name or '?'}"
            if not NAME_RE.match(name):
                problems.append(f"{where}: name must be lowercase snake_case")
            if name in seen:
                problems.append(f"{where}: defined twice")
            seen.add(name)
            ptype = item.get("type")
            ftype = item.get("field_type")
            if ptype not in VALID_FIELD_TYPES:
                problems.append(f"{where}: unknown type {ptype!r}")
            elif ftype not in VALID_FIELD_TYPES[ptype]:
                problems.append(
                    f"{where}: field_type {ftype!r} doesn't work with type {ptype!r} "
                    f"(use one of {sorted(VALID_FIELD_TYPES[ptype])})"
                )
            group = item.get("group", "")
            if (obj, group) not in group_names:
                problems.append(f"{where}: group {group!r} isn't defined under property_groups.{obj}")
            options = _parse_options(item.get("options"), where, problems)
            if ptype == "enumeration" and not options:
                problems.append(f"{where}: enumeration needs at least one option")
            if ptype != "enumeration" and options:
                problems.append(f"{where}: only enumeration properties take options")
            properties.append(
                Property(
                    object_type=obj,
                    name=name,
                    label=item.get("label", name),
                    group=group,
                    type=ptype or "",
                    field_type=ftype or "",
                    description=item.get("description", ""),
                    unique=bool(item.get("unique", False)),
                    options=options,
                )
            )

    pipelines: list[Pipeline] = []
    for obj, items in (data.get("pipelines") or {}).items():
        if obj != "deals":
            problems.append(f"pipelines.{obj}: only deal pipelines are supported")
            continue
        for item in items or []:
            label = item.get("label", "")
            where = f"pipelines.{obj}.{label or '?'}"
            stages: list[Stage] = []
            for i, s in enumerate(item.get("stages") or []):
                p = s.get("probability")
                if not isinstance(p, (int, float)) or not 0 <= p <= 1:
                    problems.append(f"{where}: stage {s.get('label')!r} needs a probability from 0.0 to 1.0")
                    p = 0.0
                stages.append(Stage(label=s.get("label", ""), probability=float(p), display_order=i))
            labels = [s.label for s in stages]
            if not stages:
                problems.append(f"{where}: needs at least one stage")
            if len(set(labels)) != len(labels):
                problems.append(f"{where}: stage labels must be unique")
            if stages and not any(s.probability == 1.0 for s in stages):
                problems.append(f"{where}: needs a closed-won stage (probability 1.0)")
            if stages and not any(s.probability == 0.0 for s in stages):
                problems.append(f"{where}: needs a closed-lost stage (probability 0.0)")
            pipelines.append(
                Pipeline(
                    object_type=obj,
                    label=label,
                    stages=stages,
                    display_order=int(item.get("display_order", 0)),
                    id=item.get("id"),
                )
            )

    if problems:
        raise ConfigError(problems)
    return PortalConfig(groups=groups, properties=properties, pipelines=pipelines)


def load_config(path: str | Path) -> PortalConfig:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))

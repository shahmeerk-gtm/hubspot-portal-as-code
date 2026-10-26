"""Compare portal.yaml with the live portal and work out what has to change.

Nothing in here writes to HubSpot. The plan is a list of Change objects
that `applier.apply_plan` executes, so `plan` shows exactly what
`apply` will do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Pipeline, PortalConfig, Property, PropertyGroup

CREATE, UPDATE, NOOP, WARN, ERROR = "create", "update", "noop", "warn", "error"
SYMBOLS = {CREATE: "+", UPDATE: "~", NOOP: "=", WARN: "?", ERROR: "!"}


@dataclass
class Change:
    action: str
    kind: str  # group | property | pipeline | stage
    target: str  # human-readable, e.g. "contacts.icp_tier"
    details: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)
    ref: dict = field(default_factory=dict)  # ids/names the applier needs

    def line(self) -> str:
        head = f"  {SYMBOLS[self.action]} {self.kind:<8} {self.target}"
        return "\n".join([head] + [f"        {d}" for d in self.details])


@dataclass
class Plan:
    changes: list[Change]

    def count(self, action: str) -> int:
        return sum(1 for c in self.changes if c.action == action)

    @property
    def has_changes(self) -> bool:
        return any(c.action in (CREATE, UPDATE) for c in self.changes)

    @property
    def has_errors(self) -> bool:
        return any(c.action == ERROR for c in self.changes)

    def render(self, show_noop: bool = False) -> str:
        lines = [c.line() for c in self.changes if show_noop or c.action != NOOP]
        summary = (
            f"Plan: {self.count(CREATE)} to create, {self.count(UPDATE)} to update, "
            f"{self.count(NOOP)} unchanged"
        )
        if self.count(WARN):
            summary += f", {self.count(WARN)} warning(s)"
        if self.count(ERROR):
            summary += f", {self.count(ERROR)} error(s) that block apply"
        if not lines:
            lines = ["  Portal already matches portal.yaml."]
        return "\n".join(lines + ["", summary])


# -- payload builders ---------------------------------------------------------


def group_payload(g: PropertyGroup) -> dict:
    return {"name": g.name, "label": g.label, "displayOrder": g.display_order}


def property_payload(p: Property, include_identity: bool = True) -> dict:
    body = {
        "label": p.label,
        "groupName": p.group,
        "fieldType": p.field_type,
        "description": p.description,
    }
    if p.options:
        body["options"] = [
            {"label": o.label, "value": o.value, "displayOrder": o.display_order, "hidden": False}
            for o in p.options
        ]
    if include_identity:
        body.update({"name": p.name, "type": p.type})
        if p.unique:
            body["hasUniqueValue"] = True
    return body


def stage_payload(label: str, probability: float, display_order: int) -> dict:
    return {
        "label": label,
        "displayOrder": display_order,
        "metadata": {"probability": str(float(probability))},
    }


def pipeline_payload(p: Pipeline) -> dict:
    return {
        "label": p.label,
        "displayOrder": p.display_order,
        "stages": [stage_payload(s.label, s.probability, s.display_order) for s in p.stages],
    }


# -- diffing ------------------------------------------------------------------


def _diff_group(g: PropertyGroup, live: dict) -> list[str]:
    diffs = []
    if live.get("label") != g.label:
        diffs.append(f"label: {live.get('label')!r} -> {g.label!r}")
    if live.get("displayOrder") != g.display_order:
        diffs.append(f"display order: {live.get('displayOrder')} -> {g.display_order}")
    return diffs


def _diff_property(p: Property, live: dict) -> tuple[list[str], list[str]]:
    """Returns (changes, blocking errors)."""
    diffs, errors = [], []
    if live.get("type") != p.type:
        errors.append(
            f"type is {live.get('type')!r} in HubSpot but {p.type!r} in config; "
            "changing a type needs a manual data migration"
        )
    if bool(live.get("hasUniqueValue")) != p.unique:
        errors.append("unique can't be changed after a property is created")
    if live.get("label") != p.label:
        diffs.append(f"label: {live.get('label')!r} -> {p.label!r}")
    if live.get("groupName") != p.group:
        diffs.append(f"group: {live.get('groupName')!r} -> {p.group!r}")
    if live.get("fieldType") != p.field_type:
        diffs.append(f"field type: {live.get('fieldType')!r} -> {p.field_type!r}")
    if (live.get("description") or "") != p.description:
        diffs.append("description updated")
    live_opts = {o["value"]: o for o in live.get("options") or [] if not o.get("hidden")}
    want_opts = {o.value: o for o in p.options}
    added = [want_opts[v].label for v in want_opts if v not in live_opts]
    removed = [live_opts[v]["label"] for v in live_opts if v not in want_opts]
    relabeled = [
        f"{live_opts[v]['label']!r} -> {want_opts[v].label!r}"
        for v in want_opts
        if v in live_opts and live_opts[v]["label"] != want_opts[v].label
    ]
    reordered = [
        v
        for v in want_opts
        if v in live_opts and live_opts[v].get("displayOrder") != want_opts[v].display_order
    ]
    if added:
        diffs.append(f"options added: {added}")
    if removed:
        diffs.append(f"options removed: {removed} (records keep their old value)")
    if relabeled:
        diffs.append(f"options relabeled: {relabeled}")
    if reordered and not (added or removed):
        diffs.append("options reordered")
    return diffs, errors


def _find_pipeline(p: Pipeline, live: list[dict]) -> dict | None:
    for lp in live:
        if p.id and lp["id"] == p.id:
            return lp
        if not p.id and lp["label"] == p.label:
            return lp
    return None


def build_plan(config: PortalConfig, client) -> Plan:
    changes: list[Change] = []
    objects = sorted(
        {g.object_type for g in config.groups}
        | {p.object_type for p in config.properties}
        | {p.object_type for p in config.pipelines}
    )
    live_groups = {o: {g["name"]: g for g in client.list_groups(o)} for o in objects}
    live_props = {o: {p["name"]: p for p in client.list_properties(o)} for o in objects}

    # Groups come first: properties can't be created in a group that doesn't exist.
    for g in config.groups:
        target = f"{g.object_type}.{g.name}"
        live = live_groups[g.object_type].get(g.name)
        if live is None:
            changes.append(Change(CREATE, "group", target, payload=group_payload(g), ref={"obj": g.object_type}))
            continue
        diffs = _diff_group(g, live)
        if diffs:
            body = {k: v for k, v in group_payload(g).items() if k != "name"}
            changes.append(
                Change(UPDATE, "group", target, diffs, body, {"obj": g.object_type, "name": g.name})
            )
        else:
            changes.append(Change(NOOP, "group", target))

    for p in config.properties:
        target = f"{p.object_type}.{p.name}"
        live = live_props[p.object_type].get(p.name)
        if live is None:
            changes.append(
                Change(CREATE, "property", target, payload=property_payload(p), ref={"obj": p.object_type})
            )
            continue
        if live.get("hubspotDefined"):
            changes.append(
                Change(ERROR, "property", target, [f"{p.name!r} is a HubSpot default property; use another name"])
            )
            continue
        diffs, errors = _diff_property(p, live)
        if errors:
            changes.append(Change(ERROR, "property", target, errors))
        elif diffs:
            changes.append(
                Change(
                    UPDATE,
                    "property",
                    target,
                    diffs,
                    property_payload(p, include_identity=False),
                    {"obj": p.object_type, "name": p.name},
                )
            )
        else:
            changes.append(Change(NOOP, "property", target))

    for obj in sorted({p.object_type for p in config.pipelines}):
        live_pipes = client.list_pipelines(obj)
        for p in (x for x in config.pipelines if x.object_type == obj):
            target = f"{obj}.{p.label}"
            live = _find_pipeline(p, live_pipes)
            if live is None:
                if p.id:
                    changes.append(Change(ERROR, "pipeline", target, [f"no pipeline with id {p.id!r} in HubSpot"]))
                else:
                    changes.append(
                        Change(
                            CREATE,
                            "pipeline",
                            target,
                            [f"{len(p.stages)} stages"],
                            pipeline_payload(p),
                            {"obj": obj},
                        )
                    )
                continue

            pipe_diffs = []
            if live["label"] != p.label:
                pipe_diffs.append(f"label: {live['label']!r} -> {p.label!r}")
            if live.get("displayOrder") != p.display_order:
                pipe_diffs.append(f"display order: {live.get('displayOrder')} -> {p.display_order}")
            if pipe_diffs:
                changes.append(
                    Change(
                        UPDATE,
                        "pipeline",
                        target,
                        pipe_diffs,
                        {"label": p.label, "displayOrder": p.display_order},
                        {"obj": obj, "pipeline_id": live["id"]},
                    )
                )
            else:
                changes.append(Change(NOOP, "pipeline", target))

            live_stages = {s["label"]: s for s in live.get("stages", [])}
            for s in p.stages:
                st_target = f"{target} / {s.label}"
                ls = live_stages.get(s.label)
                payload = stage_payload(s.label, s.probability, s.display_order)
                ref = {"obj": obj, "pipeline_id": live["id"]}
                if ls is None:
                    changes.append(Change(CREATE, "stage", st_target, payload=payload, ref=ref))
                    continue
                diffs = []
                live_prob = float((ls.get("metadata") or {}).get("probability", -1))
                if abs(live_prob - s.probability) > 1e-9:
                    diffs.append(f"probability: {live_prob} -> {s.probability}")
                if ls.get("displayOrder") != s.display_order:
                    diffs.append(f"position: {ls.get('displayOrder')} -> {s.display_order}")
                if diffs:
                    changes.append(
                        Change(UPDATE, "stage", st_target, diffs, payload, {**ref, "stage_id": ls["id"]})
                    )
                else:
                    changes.append(Change(NOOP, "stage", st_target))

            wanted = {s.label for s in p.stages}
            extra = [label for label in live_stages if label not in wanted]
            if extra:
                changes.append(
                    Change(
                        WARN,
                        "stage",
                        target,
                        [f"in HubSpot but not in config (left alone): {extra}"],
                    )
                )

    return Plan(changes)

"""Execute a Plan against HubSpot, in dependency order."""

from __future__ import annotations

from typing import Callable

from .planner import CREATE, UPDATE, Plan

# Groups before properties (a property needs its group), pipelines before stages.
ORDER = {"group": 0, "property": 1, "pipeline": 2, "stage": 3}


def apply_plan(plan: Plan, client, log: Callable[[str], None] = print) -> int:
    if plan.has_errors:
        raise RuntimeError("Plan has blocking errors; fix portal.yaml or the portal, then re-run plan.")
    todo = sorted(
        (c for c in plan.changes if c.action in (CREATE, UPDATE)),
        key=lambda c: ORDER[c.kind],
    )
    for c in todo:
        r = c.ref
        if c.kind == "group":
            if c.action == CREATE:
                client.create_group(r["obj"], c.payload)
            else:
                client.update_group(r["obj"], r["name"], c.payload)
        elif c.kind == "property":
            if c.action == CREATE:
                client.create_property(r["obj"], c.payload)
            else:
                client.update_property(r["obj"], r["name"], c.payload)
        elif c.kind == "pipeline":
            if c.action == CREATE:
                client.create_pipeline(r["obj"], c.payload)
            else:
                client.update_pipeline(r["obj"], r["pipeline_id"], c.payload)
        elif c.kind == "stage":
            if c.action == CREATE:
                client.create_stage(r["obj"], r["pipeline_id"], c.payload)
            else:
                client.update_stage(r["obj"], r["pipeline_id"], r["stage_id"], c.payload)
        log(f"  done: {c.action} {c.kind} {c.target}")
    return len(todo)

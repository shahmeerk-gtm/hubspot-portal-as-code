"""Thin HubSpot API client: auth, retries, rate limits, readable errors.

Every endpoint path lives in this file, so moving to HubSpot's newer
date-versioned API paths later is a one-file change.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

BASE_URL = "https://api.hubapi.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HubSpotError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: Any):
        self.status = status
        self.body = body
        message = body.get("message") if isinstance(body, dict) else str(body)
        message = _shorten(str(message))
        hint = ""
        if status == 401:
            hint = " (check HUBSPOT_ACCESS_TOKEN)"
        elif status == 403:
            hint = " (the token is probably missing a scope; HubSpot's message above names it)"
        super().__init__(f"{method} {path} -> {status}: {message}{hint}")


class HubSpotClient:
    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN is not set. Copy .env.example to .env and fill it in.")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        self.max_retries = max_retries
        self.sleep = sleep

    # -- core ---------------------------------------------------------------

    def request(self, method: str, path: str, json: Any = None) -> Any:
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(method, BASE_URL + path, json=json, timeout=30)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(2**attempt, 30)
                self.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                raise HubSpotError(resp.status_code, method, path, body)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise AssertionError("unreachable")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body)

    # -- schema -------------------------------------------------------------

    def list_groups(self, obj: str) -> list[dict]:
        return self.get(f"/crm/v3/properties/{obj}/groups")["results"]

    def create_group(self, obj: str, body: dict) -> dict:
        return self.post(f"/crm/v3/properties/{obj}/groups", body)

    def update_group(self, obj: str, name: str, body: dict) -> dict:
        return self.patch(f"/crm/v3/properties/{obj}/groups/{name}", body)

    def list_properties(self, obj: str) -> list[dict]:
        return self.get(f"/crm/v3/properties/{obj}")["results"]

    def create_property(self, obj: str, body: dict) -> dict:
        return self.post(f"/crm/v3/properties/{obj}", body)

    def update_property(self, obj: str, name: str, body: dict) -> dict:
        return self.patch(f"/crm/v3/properties/{obj}/{name}", body)

    def list_pipelines(self, obj: str) -> list[dict]:
        return self.get(f"/crm/v3/pipelines/{obj}")["results"]

    def create_pipeline(self, obj: str, body: dict) -> dict:
        return self.post(f"/crm/v3/pipelines/{obj}", body)

    def update_pipeline(self, obj: str, pipeline_id: str, body: dict) -> dict:
        return self.patch(f"/crm/v3/pipelines/{obj}/{pipeline_id}", body)

    def create_stage(self, obj: str, pipeline_id: str, body: dict) -> dict:
        return self.post(f"/crm/v3/pipelines/{obj}/{pipeline_id}/stages", body)

    def update_stage(self, obj: str, pipeline_id: str, stage_id: str, body: dict) -> dict:
        return self.patch(f"/crm/v3/pipelines/{obj}/{pipeline_id}/stages/{stage_id}", body)

    # -- records ------------------------------------------------------------

    def batch_upsert(self, obj: str, id_property: str, records: list[dict]) -> None:
        """records: [{"id": <unique value>, "properties": {...}}]. Sent 100 at a time."""
        for chunk in _chunks(records, 100):
            inputs = [{"idProperty": id_property, **r} for r in chunk]
            self.post(f"/crm/v3/objects/{obj}/batch/upsert", {"inputs": inputs})

    def batch_read_ids(self, obj: str, id_property: str, keys: list[str]) -> dict[str, str]:
        """Map unique property values -> HubSpot record IDs."""
        found: dict[str, str] = {}
        for chunk in _chunks(keys, 100):
            resp = self.post(
                f"/crm/v3/objects/{obj}/batch/read",
                {
                    "idProperty": id_property,
                    "properties": [id_property],
                    "inputs": [{"id": k} for k in chunk],
                },
            )
            for r in resp.get("results", []):
                key = r.get("properties", {}).get(id_property)
                if key:
                    found[key] = r["id"]
        return found

    def associate_default(self, from_obj: str, to_obj: str, pairs: list[tuple[str, str]]) -> None:
        for chunk in _chunks(pairs, 100):
            self.post(
                f"/crm/v4/associations/{from_obj}/{to_obj}/batch/associate/default",
                {"inputs": [{"from": {"id": a}, "to": {"id": b}} for a, b in chunk]},
            )

    def search_ids(self, obj: str, property_name: str) -> list[str]:
        """IDs of every record where property_name has any value."""
        ids: list[str] = []
        after = None
        while True:
            body: dict = {
                "filterGroups": [{"filters": [{"propertyName": property_name, "operator": "HAS_PROPERTY"}]}],
                "properties": [property_name],
                "limit": 100,
            }
            if after:
                body["after"] = after
            resp = self.post(f"/crm/v3/objects/{obj}/search", body)
            ids.extend(r["id"] for r in resp.get("results", []))
            after = (resp.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return ids

    def batch_archive(self, obj: str, ids: list[str]) -> None:
        for chunk in _chunks(ids, 100):
            self.post(f"/crm/v3/objects/{obj}/batch/archive", {"inputs": [{"id": i} for i in chunk]})


def _shorten(message: str, limit: int = 400) -> str:
    """Batch validation errors can be tens of KB; keep the first part readable."""
    if len(message) <= limit:
        return message
    return f"{message[:limit]}... [{len(message) - limit} more characters]"


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]

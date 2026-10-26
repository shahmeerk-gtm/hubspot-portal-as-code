"""In-memory stand-in for the parts of HubSpot this project uses.

It enforces the same rules the real API does where they matter here
(a property's group must exist, unique values stay unique, a pipeline
stage needs a probability), so tests catch ordering and payload bugs.
"""

from __future__ import annotations

import itertools

from portal_as_code.client import HubSpotError


class FakeHubSpot:
    def __init__(self):
        self._ids = itertools.count(1000)
        self.calls: list[tuple[str, ...]] = []
        self.groups = {"contacts": {"contactinformation": {"name": "contactinformation", "label": "Contact information", "displayOrder": 0}},
                       "deals": {"dealinformation": {"name": "dealinformation", "label": "Deal information", "displayOrder": 0}}}
        self.properties = {
            "contacts": {n: {"name": n, "type": "string", "fieldType": "text", "groupName": "contactinformation", "hubspotDefined": True, "label": n}
                         for n in ("email", "firstname", "lastname", "company", "jobtitle", "lifecyclestage")},
            "deals": {n: {"name": n, "type": "string", "fieldType": "text", "groupName": "dealinformation", "hubspotDefined": True, "label": n}
                      for n in ("dealname", "amount", "pipeline", "dealstage", "closedate")},
        }
        self.pipelines = {"deals": [{"id": "default", "label": "Sales Pipeline", "displayOrder": 0,
                                     "stages": [{"id": "appointmentscheduled", "label": "Appointment Scheduled", "displayOrder": 0, "metadata": {"probability": "0.2"}}]}]}
        self.records = {"contacts": {}, "deals": {}}
        self.associations: set[tuple[str, str]] = set()

    def _id(self) -> str:
        return str(next(self._ids))

    # schema
    def list_groups(self, obj):
        return [dict(g) for g in self.groups[obj].values()]

    def create_group(self, obj, body):
        self.calls.append(("create_group", obj, body["name"]))
        self.groups[obj][body["name"]] = dict(body)
        return body

    def update_group(self, obj, name, body):
        self.calls.append(("update_group", obj, name))
        self.groups[obj][name].update(body)
        return self.groups[obj][name]

    def list_properties(self, obj):
        return [dict(p) for p in self.properties[obj].values()]

    def create_property(self, obj, body):
        self.calls.append(("create_property", obj, body["name"]))
        if body["groupName"] not in self.groups[obj]:
            raise HubSpotError(400, "POST", f"/properties/{obj}", {"message": f"group {body['groupName']} does not exist"})
        if body["name"] in self.properties[obj]:
            raise HubSpotError(409, "POST", f"/properties/{obj}", {"message": "property exists"})
        self.properties[obj][body["name"]] = {"hubspotDefined": False, "description": "", **body}
        return body

    def update_property(self, obj, name, body):
        self.calls.append(("update_property", obj, name))
        assert "name" not in body and "type" not in body and "hasUniqueValue" not in body
        self.properties[obj][name].update(body)
        return self.properties[obj][name]

    def list_pipelines(self, obj):
        return [{**p, "stages": [dict(s) for s in p["stages"]]} for p in self.pipelines[obj]]

    def create_pipeline(self, obj, body):
        self.calls.append(("create_pipeline", obj, body["label"]))
        for s in body["stages"]:
            assert "probability" in s["metadata"]
        pipe = {"id": self._id(), "label": body["label"], "displayOrder": body["displayOrder"],
                "stages": [{"id": self._id(), **s} for s in body["stages"]]}
        self.pipelines[obj].append(pipe)
        return pipe

    def _pipe(self, obj, pid):
        return next(p for p in self.pipelines[obj] if p["id"] == pid)

    def update_pipeline(self, obj, pid, body):
        self.calls.append(("update_pipeline", obj, pid))
        self._pipe(obj, pid).update(body)

    def create_stage(self, obj, pid, body):
        self.calls.append(("create_stage", obj, body["label"]))
        self._pipe(obj, pid)["stages"].append({"id": self._id(), **body})

    def update_stage(self, obj, pid, sid, body):
        self.calls.append(("update_stage", obj, sid))
        stage = next(s for s in self._pipe(obj, pid)["stages"] if s["id"] == sid)
        stage.update(body)

    # records
    def _find(self, obj, prop, value):
        return next((rid for rid, r in self.records[obj].items() if r.get(prop) == value), None)

    def batch_upsert(self, obj, id_property, records):
        self.calls.append(("batch_upsert", obj, len(records)))
        prop = self.properties[obj].get(id_property)
        if not prop or not prop.get("hasUniqueValue"):
            raise HubSpotError(400, "POST", "upsert", {"message": f"{id_property} is not a unique property"})
        for r in records:
            rid = self._find(obj, id_property, r["id"])
            if rid is None:
                rid = self._id()
                self.records[obj][rid] = {}
            self.records[obj][rid].update(r["properties"])
            self.records[obj][rid][id_property] = r["id"]

    def batch_read_ids(self, obj, id_property, keys):
        return {k: rid for k in keys if (rid := self._find(obj, id_property, k))}

    def associate_default(self, from_obj, to_obj, pairs):
        self.calls.append(("associate", from_obj, len(pairs)))
        self.associations.update(pairs)

    def search_ids(self, obj, prop):
        return [rid for rid, r in self.records[obj].items() if r.get(prop)]

    def batch_archive(self, obj, ids):
        for i in ids:
            self.records[obj].pop(i, None)
        self.associations = {(d, c) for d, c in self.associations if d in self.records["deals"] and c in self.records["contacts"]}

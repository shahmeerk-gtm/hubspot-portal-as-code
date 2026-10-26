from datetime import date
from pathlib import Path

import pytest
import yaml

from portal_as_code import seed as seed_mod
from portal_as_code.applier import apply_plan
from portal_as_code.cli import main
from portal_as_code.client import HubSpotClient, HubSpotError
from portal_as_code.config import ConfigError, load_config, parse_config
from portal_as_code.planner import CREATE, ERROR, UPDATE, WARN, build_plan

from fake_hubspot import FakeHubSpot

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "portal.yaml"


@pytest.fixture
def config():
    return load_config(CONFIG)


@pytest.fixture
def raw():
    return yaml.safe_load(CONFIG.read_text())


# -- config -------------------------------------------------------------------


def test_shipped_config_is_valid(config):
    assert {p.name for p in config.properties if p.object_type == "contacts"} >= {"icp_tier", "seed_key"}
    assert config.pipelines[0].stages[-1].probability == 0.0


def test_validation_reports_every_problem_at_once(raw):
    raw["properties"]["contacts"][0]["field_type"] = "number"  # wrong for enumeration
    raw["properties"]["contacts"][1]["group"] = "nope"
    raw["pipelines"]["deals"][0]["stages"] = [{"label": "Only", "probability": 0.5}]
    with pytest.raises(ConfigError) as e:
        parse_config(raw)
    text = str(e.value)
    assert "field_type 'number'" in text
    assert "group 'nope'" in text
    assert "closed-won" in text and "closed-lost" in text


def test_options_get_stable_values(config):
    tier = next(p for p in config.properties if p.name == "icp_tier")
    assert [o.value for o in tier.options] == ["tier_1", "tier_2", "tier_3", "not_a_fit"]


# -- plan / apply -------------------------------------------------------------


def test_fresh_portal_plan_creates_everything(config):
    plan = build_plan(config, FakeHubSpot())
    creates = [c for c in plan.changes if c.action == CREATE]
    assert len(creates) == len(config.groups) + len(config.properties) + 1  # +1 pipeline with stages inline
    assert not plan.has_errors


def test_apply_then_plan_is_clean(config):
    hs = FakeHubSpot()
    apply_plan(build_plan(config, hs), hs, log=lambda _: None)
    again = build_plan(config, hs)
    assert not again.has_changes, again.render()


def test_groups_are_created_before_properties(config):
    hs = FakeHubSpot()
    apply_plan(build_plan(config, hs), hs, log=lambda _: None)
    kinds = [c[0] for c in hs.calls]
    assert max(i for i, k in enumerate(kinds) if k == "create_group") < kinds.index("create_property")


def test_config_edit_becomes_targeted_update(raw):
    hs = FakeHubSpot()
    cfg = parse_config(raw)
    apply_plan(build_plan(cfg, hs), hs, log=lambda _: None)

    raw["properties"]["contacts"][0]["options"].append("Strategic")
    raw["pipelines"]["deals"][0]["stages"][2]["probability"] = 0.5
    raw["pipelines"]["deals"][0]["stages"].insert(3, {"label": "Security Review", "probability": 0.5})
    plan = build_plan(parse_config(raw), hs)

    updates = {c.target: c for c in plan.changes if c.action == UPDATE}
    assert "options added: ['Strategic']" in updates["contacts.icp_tier"].details
    assert any("probability" in d for d in updates["deals.New Business / Demo Completed"].details)
    assert any(c.action == CREATE and c.target.endswith("Security Review") for c in plan.changes)

    hs.calls.clear()
    apply_plan(plan, hs, log=lambda _: None)
    assert ("update_property", "contacts", "icp_tier") in hs.calls
    assert not build_plan(parse_config(raw), hs).has_changes


def test_type_change_blocks_apply(raw):
    hs = FakeHubSpot()
    apply_plan(build_plan(parse_config(raw), hs), hs, log=lambda _: None)
    raw["properties"]["contacts"][4]["type"] = "number"  # first_touch_campaign
    raw["properties"]["contacts"][4]["field_type"] = "number"
    plan = build_plan(parse_config(raw), hs)
    assert plan.has_errors
    with pytest.raises(RuntimeError):
        apply_plan(plan, hs)


def test_hubspot_default_property_name_is_rejected(raw):
    raw["properties"]["contacts"].append(
        {"name": "jobtitle", "label": "Job", "group": "gtm_ops", "type": "string", "field_type": "text"}
    )
    plan = build_plan(parse_config(raw), FakeHubSpot())
    assert any(c.action == ERROR and c.target == "contacts.jobtitle" for c in plan.changes)


def test_unmanaged_stages_are_flagged_not_deleted(raw):
    hs = FakeHubSpot()
    raw["pipelines"]["deals"][0]["id"] = "default"
    plan = build_plan(parse_config(raw), hs)
    assert any(c.action == WARN and "Appointment Scheduled" in c.details[0] for c in plan.changes)


# -- seed ---------------------------------------------------------------------


def test_seed_is_deterministic(config):
    stages = config.pipelines[0].stages
    args = (config, "p1", [s.label for s in stages], [s.probability for s in stages])
    a = seed_mod.generate(*args, today=date(2026, 10, 26))
    b = seed_mod.generate(*args, today=date(2026, 10, 26))
    assert a == b
    assert len(a.contacts) == 300 and len(a.deals) == 60
    assert all(c["properties"]["email"].endswith(".example") for c in a.contacts)


def test_seed_values_are_valid_options(config):
    stages = config.pipelines[0].stages
    data = seed_mod.generate(config, "p1", [s.label for s in stages], [s.probability for s in stages])
    allowed = {p.name: {o.value for o in p.options} for p in config.properties if p.options}
    for rec in data.contacts + data.deals:
        for k, v in rec["properties"].items():
            if k in allowed:
                assert v in allowed[k], (k, v)


def test_seed_end_to_end_is_idempotent(config):
    hs = FakeHubSpot()
    apply_plan(build_plan(config, hs), hs, log=lambda _: None)
    for _ in range(2):
        seed_mod.check_seed_ready(hs)
        pid, ids, probs = seed_mod.find_seed_pipeline(config, hs)
        data = seed_mod.generate(config, pid, ids, probs)
        seed_mod.push(data, hs, log=lambda _: None)
    assert len(hs.records["contacts"]) == 300
    assert len(hs.records["deals"]) == 60
    assert len(hs.associations) == len(data.associations)
    assert seed_mod.clean(hs, log=lambda _: None) == 360
    assert not hs.records["contacts"] and not hs.associations


def test_seed_refuses_before_apply(config):
    with pytest.raises(RuntimeError, match="Run apply first"):
        seed_mod.check_seed_ready(FakeHubSpot())


# -- client -------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.sent = []

    def request(self, method, url, json=None, timeout=None):
        self.sent.append((method, url, json))
        return self.responses.pop(0)


def test_client_retries_on_rate_limit_using_retry_after():
    waits = []
    session = FakeSession([FakeResponse(429, {}, {"Retry-After": "3"}), FakeResponse(200, {"results": []})])
    client = HubSpotClient("t", session=session, sleep=waits.append)
    assert client.list_groups("contacts") == []
    assert waits == [3.0]
    assert session.headers["Authorization"] == "Bearer t"


def test_client_explains_missing_scope():
    session = FakeSession([FakeResponse(403, {"message": "requires crm.schemas.deals.write"})])
    client = HubSpotClient("t", session=session, sleep=lambda _: None)
    with pytest.raises(HubSpotError, match="missing a scope"):
        client.list_properties("deals")


def test_client_requires_token():
    with pytest.raises(ValueError, match="HUBSPOT_ACCESS_TOKEN"):
        HubSpotClient("")


# -- cli ----------------------------------------------------------------------


def test_cli_full_cycle(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    hs = FakeHubSpot()
    run = lambda *a: main(["--config", str(CONFIG), *a], client_factory=lambda: hs)
    assert run("validate") == 0
    assert run("plan", "--detailed-exitcode") == 2
    assert run("apply", "--yes") == 0
    assert run("plan", "--detailed-exitcode") == 0
    assert run("seed") == 0
    assert run("seed", "--export", str(tmp_path)) == 0
    assert (tmp_path / "contacts.csv").exists()
    assert run("clean-seed", "--yes") == 0
    out = capsys.readouterr().out
    assert "Portal already matches portal.yaml." in out

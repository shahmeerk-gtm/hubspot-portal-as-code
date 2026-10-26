"""Deterministic fake CRM data for a test portal.

The same seed number always produces the same records with the same
`seed_key` values, and records are upserted on `seed_key`, so running
`seed` twice updates the existing records instead of duplicating them.
All emails use the reserved .example domain, so nothing can ever be
sent to a real address.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import PortalConfig

FIRST = ["Ava", "Liam", "Noah", "Mia", "Zara", "Omar", "Priya", "Lucas", "Hana", "Ethan", "Sofia", "Ravi",
         "Chloe", "Mateo", "Aisha", "Daniel", "Yuna", "Ibrahim", "Grace", "Leo", "Nadia", "Owen", "Elena",
         "Samir", "Julia", "Marcus", "Ines", "Kenji", "Laila", "Theo"]
LAST = ["Patel", "Nguyen", "Garcia", "Kim", "Okafor", "Silva", "Chen", "Rahman", "Muller", "Haddad",
        "Brooks", "Tanaka", "Rossi", "Kowalski", "Ali", "Moreau", "Hughes", "Santos", "Ivanova", "Park",
        "Mensah", "Larsen", "Costa", "Ahmed", "Fischer"]
PREFIX = ["North", "Blue", "Granite", "Summit", "Harbor", "Clear", "Iron", "Bright", "Cedar", "Pioneer",
          "Atlas", "Signal", "Lumen", "Keystone", "Vector"]
SUFFIX = ["Capital", "Analytics", "Logistics", "Health", "Labs", "Systems", "Partners", "Software",
          "Robotics", "Foods", "Energy", "Media"]
TITLES = ["VP Marketing", "Director of Revenue Operations", "Head of Growth", "Marketing Operations Manager",
          "CMO", "Demand Generation Manager", "VP Sales", "Sales Operations Analyst", "COO", "CRM Manager"]
CAMPAIGNS = ["q3-webinar-pipeline-math", "q3-linkedin-abm", "q4-guide-crm-hygiene", "q4-paid-search-brand",
             "q4-outbound-revops", "evergreen-referral"]

# How seeded deals spread across pipeline stages, by position in the pipeline
# (Closed Won and Closed Lost are the last two in portal.yaml).
STAGE_WEIGHTS = [14, 12, 10, 8, 6, 6, 4]


@dataclass
class SeedData:
    contacts: list[dict]  # {"id": seed_key, "properties": {...}}
    deals: list[dict]
    associations: list[tuple[str, str]]  # (deal seed_key, contact seed_key)


def _option_values(config: PortalConfig, obj: str, name: str) -> list[str]:
    for p in config.properties:
        if p.object_type == obj and p.name == name:
            return [o.value for o in p.options]
    return []


def _weighted(rng: random.Random, values: list[str], weights: list[int]) -> str:
    """Pick from values; weights apply in order and extra values get weight 1."""
    w = (weights + [1] * len(values))[: len(values)]
    return rng.choices(values, w)[0]


def _has(config: PortalConfig, obj: str, name: str) -> bool:
    return any(p.object_type == obj and p.name == name for p in config.properties)


def generate(
    config: PortalConfig,
    pipeline_id: str,
    stage_ids: list[str],
    stage_probabilities: list[float],
    n_companies: int = 75,
    contacts_per_company: int = 4,
    n_deals: int = 60,
    seed: int = 42,
    today: date | None = None,
) -> SeedData:
    rng = random.Random(seed)
    today = today or date.today()

    icp = _option_values(config, "contacts", "icp_tier")
    sources = _option_values(config, "contacts", "lead_source_detail")
    enrich = _option_values(config, "contacts", "enrichment_status")
    deal_sources = _option_values(config, "deals", "deal_source")

    companies = []
    used = set()
    while len(companies) < n_companies:
        name = f"{rng.choice(PREFIX)} {rng.choice(SUFFIX)}"
        if name in used:
            name = f"{name} {len(companies) + 1}"
        used.add(name)
        companies.append(name)

    contacts: list[dict] = []
    by_company: dict[str, list[str]] = {}
    for ci, company in enumerate(companies):
        domain = company.lower().replace(" ", "") + ".example"
        for k in range(contacts_per_company):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            key = f"seed-c-{ci:03d}-{k}"
            props = {
                "seed_key": key,
                "firstname": first,
                "lastname": last,
                "email": f"{first.lower()}.{last.lower()}.{ci}{k}@{domain}",
                "company": company,
                "jobtitle": rng.choice(TITLES),
                "lifecyclestage": rng.choices(
                    ["subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead"], [2, 5, 3, 2]
                )[0],
            }
            if icp:
                props["icp_tier"] = _weighted(rng, icp, [3, 4, 3, 1])
            if sources:
                props["lead_source_detail"] = rng.choice(sources)
            if enrich:
                status = _weighted(rng, enrich, [2, 6, 1, 1])
                props["enrichment_status"] = status
                if status == "enriched" and _has(config, "contacts", "enrichment_date"):
                    props["enrichment_date"] = (today - timedelta(days=rng.randint(1, 120))).isoformat()
            if _has(config, "contacts", "first_touch_campaign"):
                props["first_touch_campaign"] = rng.choice(CAMPAIGNS)
            contacts.append({"id": key, "properties": props})
            by_company.setdefault(company, []).append(key)

    contact_props = {c["id"]: c["properties"] for c in contacts}
    weights = (STAGE_WEIGHTS + [5] * len(stage_ids))[: len(stage_ids)]
    deals: list[dict] = []
    associations: list[tuple[str, str]] = []
    for d, company in enumerate(rng.sample(companies, min(n_deals, len(companies)))):
        stage_index = rng.choices(range(len(stage_ids)), weights)[0]
        probability = stage_probabilities[stage_index]
        closed = probability in (0.0, 1.0)
        close = today - timedelta(days=rng.randint(1, 150)) if closed else today + timedelta(days=rng.randint(7, 120))
        key = f"seed-d-{d:03d}"
        contact_keys = rng.sample(by_company[company], rng.choice([1, 1, 2]))
        lead = contact_props[contact_keys[0]]
        props = {
            "seed_key": key,
            "dealname": f"{company} - New Business",
            "amount": str(rng.choice([12, 18, 24, 36, 48, 60, 90, 120]) * 1000),
            "pipeline": pipeline_id,
            "dealstage": stage_ids[stage_index],
            "closedate": f"{close.isoformat()}T00:00:00Z",
        }
        if deal_sources:
            # Credit the deal to the lead contact's source when it's a valid option.
            src = lead.get("lead_source_detail")
            props["deal_source"] = src if src in deal_sources else rng.choice(deal_sources)
        if _has(config, "deals", "demo_completed_date") and (probability >= 0.4 or probability == 1.0):
            props["demo_completed_date"] = (close - timedelta(days=rng.randint(14, 60))).isoformat()
        deals.append({"id": key, "properties": props})
        for ck in contact_keys:
            associations.append((key, ck))
            contact_props[ck]["lifecyclestage"] = "customer" if probability == 1.0 else "opportunity"

    return SeedData(contacts=contacts, deals=deals, associations=associations)


def export_csv(data: SeedData, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in (("contacts", data.contacts), ("deals", data.deals)):
        fields = sorted({k for r in rows for k in r["properties"]})
        path = out / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r["properties"])
        paths.append(path)
    path = out / "deal_contact_associations.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["deal_seed_key", "contact_seed_key"])
        w.writerows(data.associations)
    paths.append(path)
    return paths


def find_seed_pipeline(config: PortalConfig, client) -> tuple[str, list[str], list[float]]:
    """Live pipeline id plus stage ids/probabilities, in the order portal.yaml lists them."""
    wanted = next((p for p in config.pipelines if p.object_type == "deals"), None)
    if wanted is None:
        raise RuntimeError("portal.yaml has no deal pipeline to seed into.")
    for live in client.list_pipelines("deals"):
        if (wanted.id and live["id"] == wanted.id) or (not wanted.id and live["label"] == wanted.label):
            stages = {s["label"]: s["id"] for s in live["stages"]}
            missing = [s.label for s in wanted.stages if s.label not in stages]
            if missing:
                raise RuntimeError(f"Pipeline is missing stages {missing}. Run apply first.")
            return (
                live["id"],
                [stages[s.label] for s in wanted.stages],
                [s.probability for s in wanted.stages],
            )
    raise RuntimeError(f"Pipeline {wanted.label!r} isn't in the portal yet. Run apply first.")


def check_seed_ready(client) -> None:
    for obj in ("contacts", "deals"):
        names = {p["name"] for p in client.list_properties(obj)}
        if "seed_key" not in names:
            raise RuntimeError(f"{obj}.seed_key doesn't exist yet. Run apply first.")


def push(data: SeedData, client, log=print) -> None:
    log(f"  upserting {len(data.contacts)} contacts")
    client.batch_upsert("contacts", "seed_key", data.contacts)
    log(f"  upserting {len(data.deals)} deals")
    client.batch_upsert("deals", "seed_key", data.deals)
    contact_ids = client.batch_read_ids("contacts", "seed_key", [c["id"] for c in data.contacts])
    deal_ids = client.batch_read_ids("deals", "seed_key", [d["id"] for d in data.deals])
    pairs = [
        (deal_ids[d], contact_ids[c])
        for d, c in data.associations
        if d in deal_ids and c in contact_ids
    ]
    if len(pairs) != len(data.associations):
        raise RuntimeError(
            f"Only {len(pairs)} of {len(data.associations)} associations could be resolved to record IDs."
        )
    log(f"  associating {len(pairs)} deal-contact pairs")
    client.associate_default("deals", "contacts", pairs)


def clean(client, log=print) -> int:
    total = 0
    for obj in ("deals", "contacts"):
        ids = client.search_ids(obj, "seed_key")
        if ids:
            client.batch_archive(obj, ids)
        log(f"  archived {len(ids)} seeded {obj}")
        total += len(ids)
    return total

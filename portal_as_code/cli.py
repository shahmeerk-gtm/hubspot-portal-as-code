"""Command line: validate | plan | apply | seed | clean-seed."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .applier import apply_plan
from .client import HubSpotClient, HubSpotError
from .config import ConfigError, load_config
from .planner import build_plan
from . import seed as seed_mod


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader so the project has no extra dependency for it."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client() -> HubSpotClient:
    return HubSpotClient(os.environ.get("HUBSPOT_ACCESS_TOKEN", ""))


def main(argv: list[str] | None = None, client_factory=make_client) -> int:
    parser = argparse.ArgumentParser(prog="portal_as_code", description=__doc__)
    parser.add_argument("--config", default="portal.yaml", help="path to the portal config (default: portal.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check portal.yaml without calling HubSpot")

    p_plan = sub.add_parser("plan", help="show what apply would change")
    p_plan.add_argument("--all", action="store_true", help="also list unchanged items")
    p_plan.add_argument(
        "--detailed-exitcode", action="store_true", help="exit 2 when there are changes (for CI drift checks)"
    )

    p_apply = sub.add_parser("apply", help="make the portal match portal.yaml")
    p_apply.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p_seed = sub.add_parser("seed", help="load deterministic fake contacts and deals")
    p_seed.add_argument("--export", metavar="DIR", help="write CSVs to DIR instead of calling HubSpot")
    p_seed.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    p_seed.add_argument("--deals", type=int, default=60)

    p_clean = sub.add_parser("clean-seed", help="archive every record the seed command created")
    p_clean.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    args = parser.parse_args(argv)
    load_dotenv()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1

    if args.command == "validate":
        print(
            f"{args.config} is valid: {len(config.groups)} groups, "
            f"{len(config.properties)} properties, {len(config.pipelines)} pipeline(s)."
        )
        return 0

    try:
        if args.command == "seed" and args.export:
            data = seed_mod.generate(
                config,
                pipeline_id="<pipeline id>",
                stage_ids=[s.label for s in config.pipelines[0].stages],
                stage_probabilities=[s.probability for s in config.pipelines[0].stages],
                n_deals=args.deals,
                seed=args.seed,
            )
            for path in seed_mod.export_csv(data, args.export):
                print(f"  wrote {path}")
            return 0

        client = client_factory()

        if args.command in ("plan", "apply"):
            plan = build_plan(config, client)
            print(plan.render(show_noop=getattr(args, "all", False)))
            if args.command == "plan":
                if plan.has_errors:
                    return 1
                return 2 if (args.detailed_exitcode and plan.has_changes) else 0
            if plan.has_errors:
                return 1
            if not plan.has_changes:
                return 0
            if not args.yes and input("\nApply these changes? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return 1
            n = apply_plan(plan, client)
            print(f"\nApplied {n} change(s).")
            return 0

        if args.command == "seed":
            seed_mod.check_seed_ready(client)
            pipeline_id, stage_ids, probs = seed_mod.find_seed_pipeline(config, client)
            data = seed_mod.generate(
                config, pipeline_id, stage_ids, probs, n_deals=args.deals, seed=args.seed
            )
            seed_mod.push(data, client)
            print(f"\nSeeded {len(data.contacts)} contacts and {len(data.deals)} deals.")
            return 0

        if args.command == "clean-seed":
            if not args.yes and input("Archive every seeded contact and deal? [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return 1
            n = seed_mod.clean(client)
            print(f"\nArchived {n} record(s). HubSpot keeps archived records restorable for 90 days.")
            return 0
    except (HubSpotError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

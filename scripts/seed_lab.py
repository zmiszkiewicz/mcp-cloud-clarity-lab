#!/usr/bin/env python3
"""
The one entry point that puts a tenant into the exact state the track expects.

    python3 seed_lab.py                      baseline, then every break, then assert
    python3 seed_lab.py --baseline-only      healthy footprint, no breaks
    python3 seed_lab.py --break dhcp_range_overlap   one break, on its own
    python3 seed_lab.py --assert-only        change nothing, just verify
    python3 seed_lab.py --fix zone_missing_auth_server   undo one break
    python3 seed_lab.py --list               show the break registry

Idempotent throughout. Running it twice against the same tenant converges on the
same state — every baseline builder looks its object up before creating it, and
every break is a PATCH to a known value rather than a relative change.

EXIT CODES
  0  everything asserted clean
  1  at least one break did not land, or the baseline is incomplete

Non-zero here is load-bearing. 01/setup-shell checks it, so a silent seeding
failure becomes a failed challenge at track start rather than a participant
spending fifteen minutes hunting a fault that was never applied.
"""

import argparse
import json
import sys

import baseline
import breaks
import lab_config as cfg
from csp_client import CspClient, LabTodo, info, ok


IDS_FILE = "seed_ids.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Seed and break the TechCorp tenant for the MCP cloud track."
    )
    parser.add_argument("--baseline-only", action="store_true",
                        help="build the healthy footprint, apply no breaks")
    parser.add_argument("--break", dest="break_name", action="append", default=[],
                        metavar="NAME",
                        help="apply one named break (repeatable)")
    parser.add_argument("--fix", dest="fix_name", action="append", default=[],
                        metavar="NAME",
                        help="undo one named break (repeatable)")
    parser.add_argument("--assert-only", action="store_true",
                        help="verify the seeded state without changing anything")
    parser.add_argument("--list", action="store_true",
                        help="list the available breaks and exit")
    parser.add_argument("--account-id", default=None,
                        help="CSP account UUID; defaults to sandbox_id.txt")
    return parser.parse_args()


def load_ids():
    """
    Reuse the ids from a previous run so --break and --assert-only do not have
    to rebuild the baseline just to learn object ids.
    """
    try:
        with open(IDS_FILE) as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return None


def save_ids(ids):
    with open(IDS_FILE, "w") as handle:
        json.dump(ids, handle, indent=2)
    info(f"object ids written to {IDS_FILE}")


def resolve_ids(client, args):
    """
    Get the object ids, rebuilding the baseline if we have none cached.

    --assert-only never builds: if the ids are missing the tenant was never
    seeded, and saying so is more useful than silently seeding it.
    """
    cached = load_ids()
    if cached:
        return cached

    if args.assert_only:
        raise SystemExit(
            f"❌ {IDS_FILE} not found. This tenant has not been seeded — "
            f"run `python3 seed_lab.py` first."
        )

    ids = baseline.build_all(client)
    save_ids(ids)
    return ids


def assert_all(client, ids, expected):
    """
    Re-read the tenant and confirm every expected break is present.

    Reporting all failures in one pass matters: if two breaks fail for the same
    underlying reason, the maintainer sees the pattern instead of fixing them
    one restart at a time.
    """
    failures = []
    print("\n=== Asserting seeded state ===", flush=True)
    for name in expected:
        passed, reason = breaks.BREAKS[name]["assert"](client, ids)
        if passed:
            ok(f"{name}: {reason}")
        else:
            print(f"❌ {name}: {reason}", flush=True)
            failures.append((name, reason))
    return failures


def main():
    args = parse_args()

    if args.list:
        print("Available breaks:\n")
        for name, spec in breaks.BREAKS.items():
            print(f"  {name:<26} part {spec['part']}  {spec['summary']}")
        return 0

    try:
        client = CspClient.as_admin(account_id=args.account_id)
    except LabTodo as todo:
        print(f"\n❌ Unresolved TODO reached during authentication:\n   {todo}\n",
              flush=True)
        return 1

    ok(f"authenticated against {cfg.CSP_URL} as the lab admin")
    info(f"sandbox account: {client.account_id}")

    failures = []
    selected = []
    try:
        ids = resolve_ids(client, args)

        # --fix runs alone: it is the reset path, not part of seeding.
        if args.fix_name:
            for name in args.fix_name:
                if name not in breaks.BREAKS:
                    raise SystemExit(f"❌ unknown break {name!r}; try --list")
                breaks.BREAKS[name]["fix"](client, ids)
            return 0

        if args.assert_only:
            expected = args.break_name or list(breaks.BREAKS)
            return 1 if assert_all(client, ids, expected) else 0

        if args.baseline_only:
            ok("baseline built; no breaks applied")
            return 0

        selected = args.break_name or list(breaks.BREAKS)
        for name in selected:
            if name not in breaks.BREAKS:
                raise SystemExit(f"❌ unknown break {name!r}; try --list")

        print("\n=== Applying breaks ===", flush=True)
        for name in selected:
            breaks.BREAKS[name]["apply"](client, ids)

        failures = assert_all(client, ids, selected)

    except LabTodo as todo:
        print(f"\n❌ Unresolved TODO reached while seeding:\n   {todo}\n"
              f"   Fill this in at lab_config.PATHS and re-run.\n", flush=True)
        return 1

    if failures:
        print(f"\n{'=' * 62}", flush=True)
        print(f"❌ {len(failures)} break(s) did not land. The lab is NOT ready.",
              flush=True)
        for name, reason in failures:
            print(f"   - {name}: {reason}", flush=True)
        print(f"{'=' * 62}", flush=True)
        return 1

    print(f"\n{'=' * 62}", flush=True)
    print("🎉 Tenant seeded and every break asserted. The lab is ready.", flush=True)
    print(f"   Zone:        {cfg.ZONE_FQDN}", flush=True)
    print(f"   DNS server:  {ids.get('dns_server_name', '?')} "
          f"({ids.get('auth_mode', '?')} mode)", flush=True)
    print(f"   Network:     {cfg.SUBNETS['dc-01']['address']}"
          f"/{cfg.SUBNETS['dc-01']['cidr']}", flush=True)
    print(f"   Branch:      {cfg.SUBNETS['branch-02']['address']}"
          f"/{cfg.SUBNETS['branch-02']['cidr']}", flush=True)
    print(f"   Breaks:      {', '.join(selected)}", flush=True)
    print(f"{'=' * 62}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Remove the DDI objects seed_lab.py created. Safe to run twice.

This is the belt to revoke_mcp_keys.py's braces, and to the broker's braces
after that. The broker deletes the subtenant outright a few minutes after
deallocation_subtenant.py runs, so in the normal case this script is redundant.
It exists for the case that is not normal: a track that failed halfway, a
sandbox that gets recycled rather than destroyed, or a maintainer iterating on
the seed against a long-lived test tenant. In all three, a half-fixed tenant
that nobody cleaned is what makes the NEXT run confusing.

Only objects tagged {instruqt-lab: <track slug>} are touched. Anything else in
the tenant is left alone — including the Universal DDI host, which this lab
never created and must not delete.

Deletion order is reverse-dependency: records before zones, ranges and
reservations before subnets, subnets before blocks before spaces. A 404 at any
step is success, not an error.

Usage:
    python3 teardown_lab.py                 remove the seeded objects
    python3 teardown_lab.py --reset         remove them, then re-seed from clean
    python3 teardown_lab.py --dry-run       list what would be deleted
"""

import argparse
import json
import os
import sys

import lab_config as cfg
from csp_client import (CspClient, CspError, LabTodo, info, object_url, ok,
                        read_state)


# Reverse-dependency order. Each entry is (registry key, human label).
DELETE_ORDER = [
    ("dns_record",          "DNS records"),
    ("dhcp_range",          "DHCP ranges"),
    ("dhcp_fixed_address",  "fixed addresses / reservations"),
    ("ipam_subnet",         "IPAM subnets"),
    ("ipam_address_block",  "IPAM address blocks"),
    ("ipam_ip_space",       "IPAM IP spaces"),
    ("dns_forward_zone",    "forward zones"),
    ("dns_auth_zone",       "authoritative zones"),
    # Created by the lab in nsg mode, so the lab removes it. Tagged, like
    # everything else, so a group the tenant already had is left alone.
    ("dns_auth_nsg",        "DNS server groups"),
    ("dns_view",            "DNS views"),
]


def is_ours(obj):
    """True only for objects this lab tagged. The guard against collateral damage."""
    tags = obj.get("tags") or {}
    return tags.get(cfg.LAB_TAG_KEY) == cfg.LAB_TAG_VALUE


def release_host_first(client, ids):
    """
    Clear the zone's Authoritative DNS Servers list before deleting the zone.

    Deleting a zone that a host is still serving either fails or leaves the host
    reconciling state the next seeding has to undo. Doing it explicitly costs
    one PATCH and saves debugging it later. The host itself is never deleted —
    the lab did not create it. A lab-created DNS server group IS deleted, by the
    tag-filtered purge below.
    """
    zone_id = ids.get("zone_id")
    if not zone_id:
        return
    try:
        # Both representations: we do not know which one this run used, and
        # clearing the wrong one leaves the zone pinned to an object we are
        # about to delete.
        client.patch(object_url(cfg.path("dns_auth_zone"), zone_id),
                     json_body={"internal_secondaries": [], "nsgs": []})
        info(f"released {cfg.ZONE_FQDN} from its authoritative servers")
    except (CspError, LabTodo) as exc:
        info(f"could not release the zone from its host (continuing): {exc}")


def purge(client, registry_key, label, dry_run):
    """Delete every tagged object under one collection. Returns a count."""
    try:
        collection = cfg.path(registry_key)
    except LabTodo as todo:
        info(f"skipping {label} — {todo}")
        return 0

    try:
        rows = client.list_results(collection)
    except CspError as exc:
        info(f"could not list {label} (continuing): {exc}")
        return 0

    ours = [row for row in rows if is_ours(row)]
    if not ours:
        info(f"no lab-tagged {label} to remove")
        return 0

    removed = 0
    for row in ours:
        obj_id = (row.get("id") or "").split("/")[-1]
        name = row.get("name") or row.get("fqdn") or obj_id

        if dry_run:
            print(f"   would delete {label[:-1]}: {name}", flush=True)
            removed += 1
            continue

        try:
            # csp_client.delete() accepts 404 — deleting twice is a no-op.
            client.delete(object_url(collection, obj_id))
            info(f"deleted {label[:-1]}: {name}")
            removed += 1
        except CspError as exc:
            # Never abort. One stubborn object must not strand the rest.
            print(f"⚠️  could not delete {label[:-1]} {name}: {exc}", flush=True)

    return removed


def load_ids():
    for directory in (os.getcwd(), cfg.SCRIPT_DIR):
        candidate = os.path.join(directory, "seed_ids.json")
        if os.path.exists(candidate):
            with open(candidate) as handle:
                return json.load(handle)
    return {}


def clear_ids():
    for directory in (os.getcwd(), cfg.SCRIPT_DIR):
        candidate = os.path.join(directory, "seed_ids.json")
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Remove the seeded lab objects from the tenant."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be deleted, change nothing")
    parser.add_argument("--reset", action="store_true",
                        help="after teardown, re-seed the tenant from clean")
    args = parser.parse_args()

    sandbox_id = read_state("sandbox_id", required=False)
    if not sandbox_id:
        print("⚠️  sandbox_id.txt not found — nothing was seeded, "
              "nothing to tear down", flush=True)
        return 0

    try:
        client = CspClient.as_admin(account_id=sandbox_id)
    except Exception as exc:                            # noqa: BLE001
        print(f"⚠️  could not authenticate against sandbox {sandbox_id}: {exc}",
              flush=True)
        print("   The account may already be gone. Nothing to tear down.",
              flush=True)
        return 0

    ids = load_ids()
    if not args.dry_run:
        release_host_first(client, ids)

    total = 0
    for registry_key, label in DELETE_ORDER:
        print(f"\n── {label} ──", flush=True)
        total += purge(client, registry_key, label, args.dry_run)

    if not args.dry_run:
        clear_ids()

    verb = "would remove" if args.dry_run else "removed"
    print(f"\n{'=' * 62}", flush=True)
    ok(f"teardown complete — {verb} {total} lab-tagged object(s)")
    print(f"{'=' * 62}", flush=True)

    if args.reset and not args.dry_run:
        print("\n=== Re-seeding ===", flush=True)
        import seed_lab
        return seed_lab.main()

    return 0


if __name__ == "__main__":
    sys.exit(main())

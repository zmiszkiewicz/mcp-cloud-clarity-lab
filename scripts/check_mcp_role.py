#!/usr/bin/env python3
"""
What is the MCP service user actually allowed to do?

Written because the Infoblox MCP Server refused a PATCH with

    Write actions (post, patch, put, delete) are not available.

which is the server declining a whole verb class, not CSP rejecting a
permission on an object. Two very different causes look identical from the
chat window:

  1. The key resolved to a READ-ONLY MCP role. provision_mcp_keys.py asks for
     ib-mcp-server-admin, but if that group is absent it falls back to
     discovery, and a tenant naming its groups differently could land the user
     somewhere read-only without the track noticing.

  2. Writes are disabled in this MCP build for everyone, regardless of role.

This script settles (1) by reading the user's group membership back from CSP,
and tests (2) by making the same class of write DIRECTLY against the CSP REST
API with the same key. If the direct write succeeds and MCP refuses it, the
key is fine and the server is the constraint.

    python3 check_mcp_role.py            report, change nothing
    python3 check_mcp_role.py --probe-write   also attempt a real write

The write probe creates and immediately deletes a throwaway DNS view. It is
off by default because this is a diagnostic, and a diagnostic that mutates a
tenant by surprise is not one.
"""

import argparse
import sys

import lab_config as cfg
from csp_client import CspClient, CspError, info, ok, read_state


def report_groups(admin):
    """The groups the MCP service user is actually in, read back from CSP."""
    recorded = read_state("mcp_service_user_id", required=False)
    if not recorded:
        info("no MCP service user recorded — was provision_mcp_keys.py run?")
        return

    groups_by_id = {row.get("id"): row.get("name")
                    for row in admin.list_results(cfg.path("groups"))}

    for user_id in (p.strip() for p in recorded.split(",") if p.strip()):
        try:
            payload = admin.get(f"{cfg.path('users')}/{user_id}")
        except CspError as exc:
            info(f"could not read user {user_id}: {exc}")
            continue

        user = payload.get("result", payload)
        names = [groups_by_id.get(gid, gid) for gid in user.get("group_ids", [])]
        print(f"\n  user:   {user.get('name')} ({user_id})")
        print(f"  groups: {', '.join(names) or '(none)'}")

        mcp = [n for n in names if n and "mcp" in n.lower()]
        if not mcp:
            print("  ⚠️  NO MCP GROUP. The server would refuse the connection "
                  "outright, so this is not the current symptom — but it is "
                  "wrong.")
        elif any("admin" in n.lower() for n in mcp):
            print("  → holds an MCP ADMIN role, so a read-only key is NOT the "
                  "explanation for a refused write.")
        else:
            print("  → holds only a read-only MCP role. THAT is why writes are "
                  "refused. Set MCP_RW_GROUP to the admin group's real name.")


def probe_write(api_key):
    """
    Attempt a write straight at the CSP REST API with the MCP key.

    This is the discriminator. The MCP server sits in front of the same API
    with the same credential, so:

      succeeds here, refused by MCP  -> the key can write; the SERVER is
                                        blocking the verb class
      refused here too               -> the key genuinely cannot write
    """
    client = CspClient.from_service_key(api_key)
    name = f"mcp-write-probe-{cfg.PARTICIPANT_ID}"

    try:
        created = client.post(cfg.path("dns_view"), json_body={
            "name": name,
            "comment": "temporary write probe; delete if you find this",
        })
    except CspError as exc:
        print(f"\n  direct CSP write: REFUSED (HTTP {exc.status})")
        print("  → the key itself cannot write. This is an RBAC problem, not "
              "an MCP server one.")
        return

    view_id = (created.get("result", created).get("id") or "")
    print("\n  direct CSP write: SUCCEEDED")
    print("  → the key CAN write. The Infoblox MCP Server is refusing the verb "
          "class itself, which no change on our side fixes.")

    try:
        from csp_client import object_url
        client.delete(object_url(cfg.path("dns_view"), view_id))
        info("probe view cleaned up")
    except CspError as exc:
        print(f"  ⚠️  could not delete the probe view {view_id}: {exc}")


def main():
    parser = argparse.ArgumentParser(
        description="Report what the MCP service user can do."
    )
    parser.add_argument("--probe-write", action="store_true",
                        help="also attempt a real write via the CSP REST API")
    args = parser.parse_args()

    print("=" * 62)
    print("MCP service user diagnostics")
    print("=" * 62)

    admin = CspClient.as_admin()
    ok(f"authenticated against sandbox {admin.account_id}")
    report_groups(admin)

    if args.probe_write:
        key = read_state("mcp_key", required=False)
        if not key:
            info("no mcp_key.txt — cannot probe")
        else:
            probe_write(key)
    else:
        print("\n  (run with --probe-write to test an actual write)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

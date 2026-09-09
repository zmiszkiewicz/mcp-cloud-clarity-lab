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
        print(f"\n  user:   {user.get('name')} ({user_id})")

        # GET /v2/users/{id} does NOT reliably return group_ids. An empty list
        # here means "not reported", not "no groups" — and reporting it as the
        # latter sent a real investigation down a false path once already.
        if "group_ids" not in user:
            print("  groups: not reported by this endpoint")
            print("  → cannot confirm role membership from here. Check the "
                  "Portal: System > User Access > Users. Note that if the user "
                  "truly had no MCP group the server would refuse the "
                  "connection, so a working read proves one is present.")
            continue

        names = [groups_by_id.get(gid, gid) for gid in user.get("group_ids") or []]
        print(f"  groups: {', '.join(names) or '(none reported)'}")

        mcp = [n for n in names if n and "mcp" in n.lower()]
        if not mcp:
            print("  ⚠️  no MCP group in the reported list. Treat with "
                  "suspicion rather than as fact — a working MCP read proves "
                  "one exists.")
        elif any("admin" in n.lower() for n in mcp):
            print("  → holds an MCP ADMIN role, so a read-only key is NOT the "
                  "explanation for a refused write.")
        else:
            print("  → holds only a read-only MCP role. THAT is why writes are "
                  "refused. Set MCP_RW_GROUP to the admin group's real name.")


def whoami(api_key):
    """
    Which CSP identity does this key actually authenticate as?

    The decisive question when someone has changed a role in the Portal and it
    has not taken effect: did they change the user this key belongs to? The
    key's own view is the only authoritative answer — the user's display name
    in the Portal need not match the name provision_mcp_keys.py asked for.
    """
    client = CspClient.from_service_key(api_key)

    try:
        payload = client.get(cfg.path("current_user"))
    except CspError as exc:
        print(f"\n  key identity: could not read /v2/current_user ({exc})")
        return

    user = payload.get("result", payload)
    print("\n  the MCP key authenticates as:")
    print(f"    name:  {user.get('name')}")
    print(f"    email: {user.get('email')}")
    print(f"    id:    {user.get('id')}")
    print("    → THIS is the user whose role governs the key. If you changed a "
          "different one in the Portal, the change will not apply.")

    for field in ("group_ids", "groups"):
        if user.get(field):
            print(f"    {field}: {user[field]}")

    try:
        account = client.get(cfg.path("current_account"))
        acct = account.get("result", account)
        print(f"    account: {acct.get('name')} ({acct.get('id')})")
    except CspError:
        pass


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

    key = read_state("mcp_key", required=False)
    if not key:
        info("no mcp_key.txt — cannot inspect the key")
        return 0

    whoami(key)

    if args.probe_write:
        probe_write(key)
    else:
        print("\n  (run with --probe-write to test an actual write)")

    print("\n  If you changed a role in the Portal and Claude Code still "
          "refuses writes:")
    print("    1. Restart Claude Code (Ctrl-C, then `claude`). It fetches the "
          "MCP tool list at connect time and caches it for the session.")
    print("    2. Confirm the user above is the one you edited.")
    print("    3. Re-run this with --probe-write. A direct CSP write that "
          "succeeds while MCP still refuses means the server, not the role.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

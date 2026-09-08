#!/usr/bin/env python3
"""
Revoke the participant's Service API keys and delete the service user.

SAFE TO RUN TWICE. Every step treats "already gone" as success:

  * a missing state file means that object was never created — skip, exit 0
  * a 404 on DELETE means someone already revoked it — log and continue
  * one failing step never stops the next one, so a key that cannot be revoked
    does not leave the service user behind as well

That matters more here than in most teardowns. These keys are live credentials
against a real CSP tenant; a half-finished cleanup that aborts on the first
error is how a key outlives the lab that issued it.

Ordering in the track's cleanup-shell:

    revoke_mcp_keys.py          revoke keys, delete service user   <- this
    teardown_lab.py             remove the seeded DDI objects
    user_provision.py --delete  delete the interactive portal user  (vendored)
    deallocation_subtenant.py   hand the sandbox back to the broker (vendored)

Keys first, because everything after this point only matters if the credentials
are already dead. The broker's own worker deletes the subtenant outright a few
minutes later, but we do not rely on that: an account deletion that silently
fails must not leave a working key behind.

Usage:
    python3 revoke_mcp_keys.py
    python3 revoke_mcp_keys.py --keep-user   revoke keys, leave the user
"""

import argparse
import os
import sys

import lab_config as cfg
from csp_client import (CspClient, CspError, LabTodo, info, ok, read_state)


def shred_key_files():
    """
    Remove the plaintext key files from the participant's VM.

    The sandbox is destroyed shortly after anyway, but the files are mode 0600
    secrets and removing them is free.
    """
    for state_key in ("mcp_ro_key", "mcp_rw_key"):
        filename = cfg.STATE_FILES[state_key]
        for directory in (os.getcwd(), cfg.SCRIPT_DIR):
            candidate = os.path.join(directory, filename)
            try:
                os.unlink(candidate)
                info(f"removed {candidate}")
            except FileNotFoundError:
                pass


def revoke_key(client, state_key, label):
    """
    Revoke one Service API key. Returns True if the key is gone afterwards,
    whether this call is what removed it or it was already absent.
    """
    key_id = read_state(state_key, required=False)
    if not key_id:
        info(f"no {label} key id recorded — nothing to revoke")
        return True

    try:
        # TODO-04 — confirm DELETE /api/iam/v2/keys/{id} is the right verb for a
        # Service API key and that it 404s (rather than 500s) on a second call.
        # csp_client.delete() already accepts 404 as success.
        client.delete(cfg.path("iam_key_by_id", key_id=key_id))
        ok(f"revoked {label} key {key_id}")
        return True
    except CspError as exc:
        print(f"⚠️  could not revoke {label} key {key_id}: {exc}", flush=True)
        return False


def delete_service_user(client):
    """
    Delete the MCP users. provision_mcp_keys.py records both ids (read-only and
    read/write) comma-separated in one state file, so split before deleting.

    One failing delete never stops the next — a user left behind is untidy, but
    a user left behind *because* another one failed is a bug.
    """
    recorded = read_state("mcp_service_user_id", required=False)
    if not recorded:
        info("no MCP user ids recorded — nothing to delete")
        return True

    outcomes = []
    for user_id in (part.strip() for part in recorded.split(",") if part.strip()):
        try:
            client.delete(f"{cfg.path('users')}/{user_id}")
            ok(f"deleted MCP user {user_id}")
            outcomes.append(True)
        except CspError as exc:
            print(f"⚠️  could not delete MCP user {user_id}: {exc}", flush=True)
            outcomes.append(False)
    return all(outcomes)


def main():
    parser = argparse.ArgumentParser(
        description="Revoke the participant's MCP service credentials."
    )
    parser.add_argument("--keep-user", action="store_true",
                        help="revoke the keys but leave the MCP users in place")
    args = parser.parse_args()

    # A missing sandbox id means allocation never got far enough to create
    # anything. Nothing to revoke; exiting 0 keeps cleanup-shell moving.
    sandbox_id = read_state("sandbox_id", required=False)
    if not sandbox_id:
        print("⚠️  sandbox_id.txt not found — nothing was provisioned, "
              "nothing to revoke", flush=True)
        return 0

    try:
        client = CspClient.as_admin(account_id=sandbox_id)
    except LabTodo as todo:
        print(f"❌ Unresolved TODO reached during teardown auth:\n   {todo}",
              flush=True)
        return 1
    except Exception as exc:                       # noqa: BLE001
        # If we cannot even authenticate the account is very likely already
        # gone. Say so plainly and let the rest of cleanup-shell run.
        print(f"⚠️  could not authenticate against sandbox {sandbox_id}: {exc}",
              flush=True)
        print("   The account may already have been deleted. Continuing.",
              flush=True)
        return 0

    outcomes = [
        revoke_key(client, "mcp_ro_key_id", "read-only"),
        revoke_key(client, "mcp_rw_key_id", "read/write"),
    ]

    if not args.keep_user:
        outcomes.append(delete_service_user(client))

    shred_key_files()

    print(f"\n{'=' * 62}", flush=True)
    if all(outcomes):
        print("✅ MCP credentials revoked", flush=True)
    else:
        # Still exit 0: cleanup-shell must continue to the sandbox deallocation,
        # which destroys the account and with it anything left behind.
        print("⚠️  MCP credential revocation was partial — see warnings above.",
              flush=True)
        print("   Continuing so the sandbox still gets deallocated.", flush=True)
    print(f"{'=' * 62}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

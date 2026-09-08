#!/usr/bin/env python3
"""
Create the participant's MCP service user and mint its Service API key.

ONE KEY, READ/WRITE.

This used to mint two — a read-only key for Parts 1 and 4, and a read/write key
handed over at Part 2 — so that the write-safety story was enforced by the
platform rather than asserted by the prose. It was a nice property and it cost
too much for what it bought: two CSP users, a key swap at two challenge
boundaries, and a Claude Code restart each time. Participants hit Part 2 unable
to make the change the assignment had just told them to make.

The write-safety lesson survives without it, because the thing actually doing
the teaching was never the key. It is Claude Code stopping before every tool
call and making the operator approve it, and the assistant being asked to state
its intended change first. Both still happen.

What is lost is Part 4's live access-denied demonstration. That part now uses a
boundary that is real with any key: account and user administration is not
exposed through the MCP Server at all, so asking the assistant to create a user
or change a role fails because the capability does not exist — which is the
sturdier lesson anyway.

To restore the two-key flow, set MCP_ROLES=read_only,read_write; the machinery
below still supports it.

Usage:
    python3 provision_mcp_keys.py            create the user and its key
    python3 provision_mcp_keys.py --verify   confirm the key authenticates
"""

import argparse
import datetime
import random
import string
import sys
import time

import lab_config as cfg
from csp_client import (CspClient, CspError, LabTodo, info, ok, read_state,
                        write_state)


# Every role this track knows how to provision. MCP_ROLES selects which of them
# actually get created — one, by default.
ALL_ROLES = {
    "read_only": {
        "label": "read-only",
        "user_prefix": "mcp-ro",
        "groups": cfg.MCP_RO_GROUPS,
        "group_override": cfg.MCP_RO_GROUP,
        "group_env": "MCP_RO_GROUP",
        "key_state": "mcp_ro_key",
        "key_id_state": "mcp_ro_key_id",
    },
    "read_write": {
        "label": "read/write",
        "user_prefix": "mcp-rw",
        "groups": cfg.MCP_RW_GROUPS,
        "group_override": cfg.MCP_RW_GROUP,
        "group_env": "MCP_RW_GROUP",
        "key_state": "mcp_key",
        "key_id_state": "mcp_key_id",
    },
}

ROLES = {name: ALL_ROLES[name] for name in cfg.MCP_ROLES if name in ALL_ROLES}
if not ROLES:
    raise SystemExit(
        f"❌ MCP_ROLES={cfg.MCP_ROLES} names no known role. "
        f"Valid: {', '.join(ALL_ROLES)}"
    )


def key_expiry():
    """
    The `expires_at` value for a new Service API key.

    REQUIRED by POST /v2/current_api_keys. Leaving it out returns
    HTTP 400 `HTTP interceptor error: invalid datetime or duration` — a message
    that names neither the missing field nor the endpoint's expectation.

    The format matters as much as the presence: RFC3339, UTC, millisecond
    precision, literal `Z` suffix. That is what the estate's deploy_api_key.py
    has always sent and what CSP accepts. `datetime.isoformat()` produces
    `+00:00` instead of `Z` and microsecond rather than millisecond precision,
    so it is built explicitly here rather than left to the default.
    """
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        hours=cfg.MCP_KEY_TTL_HOURS
    )
    return expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def generate_password(length=16):
    """
    A password meeting CSP's complexity rules.

    Same construction as the estate's user_provision.py: upper, lower, digits
    and 2+ specials, shuffled.
    """
    chars = (random.choices(string.ascii_uppercase, k=3)
             + random.choices(string.ascii_lowercase, k=5)
             + random.choices(string.digits, k=4)
             + random.choices("!@#$%&", k=4))
    random.shuffle(chars)
    return "".join(chars)


def discover_mcp_group(groups, role):
    """
    Fall back to guessing which group carries an MCP role, if the configured
    name is not in this sandbox.

    Only used when the default or overridden name is absent — a tenant with a
    different naming convention. Deliberately conservative: zero or several
    candidates means we do NOT guess, because binding the read-only key to a
    group that can write would hand the agent write access for the whole track
    and silently break the Part 4 RBAC exercise. Wrong is much worse than
    absent.
    """
    write_words = ("admin", "write", "readwrite", "read_write", "read-write", "rw")
    read_words = ("user", "readonly", "read_only", "read-only", "read", "view", "ro")

    candidates = []
    for name, gid in groups.items():
        if not name or "mcp" not in name.lower():
            continue
        lowered = name.lower()
        looks_write = any(w in lowered for w in write_words)
        looks_read = any(w in lowered for w in read_words)

        if role == "read_write" and looks_write:
            candidates.append((name, gid))
        elif role == "read_only" and looks_read and not looks_write:
            candidates.append((name, gid))

    return candidates[0] if len(candidates) == 1 else None


def resolve_role_groups(admin):
    """
    Resolve each role to the list of CSP group ids its user should be in.

    Each role needs THREE groups, not one: the base `user` group, an
    ib-mcp-server-* group granting access to the MCP Server, and an ib-ddi-*
    group granting access to the data behind it. An MCP role on its own gates
    the connection but not the data, so a user with only that would connect
    successfully and then have every tool call come back empty — which reads as
    a broken lab rather than a permissions problem.

    Missing non-MCP groups are a warning, not an error: a tenant with a
    slightly different group set should still come up. A missing MCP group IS
    fatal, because without it the server refuses the connection outright and
    Part 1 cannot pass.
    """
    groups = {row.get("name"): row.get("id")
              for row in admin.list_results(cfg.path("groups"))}

    resolved = {}
    fatal = []
    for role, spec in ROLES.items():
        wanted = list(spec["groups"])

        # A single-name override replaces just the MCP-server entry.
        if spec["group_override"]:
            wanted = [spec["group_override"] if "mcp" in g.lower() else g
                      for g in wanted]
            if not any("mcp" in g.lower() for g in wanted):
                wanted.append(spec["group_override"])

        ids, names, mcp_ok = [], [], False
        for name in wanted:
            gid = groups.get(name)

            if gid is None and "mcp" in name.lower():
                guess = discover_mcp_group(groups, role)
                if guess:
                    name, gid = guess
                    info(f"    {spec['label']}: MCP group auto-discovered as "
                         f"{name}")

            if gid is None:
                if "mcp" in name.lower():
                    fatal.append(
                        f"the MCP {spec['label']} group {name!r} does not exist "
                        f"in this sandbox — set {spec['group_env']}"
                    )
                else:
                    print(f"⚠️  {spec['label']}: group {name!r} not found in this "
                          f"sandbox, skipping", flush=True)
                continue

            ids.append(gid)
            names.append(name)
            if "mcp" in name.lower():
                mcp_ok = True

        if not mcp_ok and not any(f.startswith("the MCP") for f in fatal):
            fatal.append(
                f"no MCP group resolved for the {spec['label']} role — "
                f"set {spec['group_env']}"
            )

        resolved[role] = ids
        ok(f"MCP {spec['label']} user groups: {', '.join(names) or '(none)'}")

    if fatal:
        raise SystemExit(
            "❌ Could not resolve the MCP role groups (TODO-02):\n"
            + "".join(f"   - {m}\n" for m in fatal)
            + "\n   Groups that DO exist in this sandbox:\n"
            + "".join(f"     {n}\n" for n in sorted(g for g in groups if g))
            + "\n   Override with comma-separated lists if the convention "
              "differs:\n"
              "     MCP_RO_GROUPS=user,ib-mcp-server-user,ib-ddi-user\n"
              "     MCP_RW_GROUPS=user,ib-mcp-server-admin,ib-ddi-admin\n"
            + "\n   Without an MCP Server role the server refuses the "
              "connection outright, so Part 1 cannot pass."
        )
    return resolved


def ensure_user(admin, name, email, group_ids, password):
    """
    Create one MCP user in one group. Idempotent — a re-run finds the existing
    user by name rather than creating a second.

    Mirrors the estate's user_provision.py, including the 409-means-it-exists
    handling, but with the role-scoped groups above instead of user+act_admin.
    """
    existing = admin.find_by_name(cfg.path("users"), name)
    if existing:
        user_id = existing["id"].split("/")[-1]
        info(f"user {name} already exists ({user_id})")
    else:
        created = admin.post(cfg.path("users"), json_body={
            "name": name,
            "email": email,
            "type": "interactive",
            "group_ids": group_ids,
        })
        user_id = created.get("result", {}).get("id", "").split("/")[-1]
        if not user_id:
            raise SystemExit(f"❌ user create for {name} returned no id: {created}")
        ok(f"created user {name} ({user_id})")

    # Always (re)set the password — we need to know it to sign in as this user,
    # and on a re-run we do not have the one from last time.
    admin.post(cfg.path("user_password", user_id=user_id),
               json_body={"new_password": password})
    return user_id


def mint_key_as_user(email, password, account_id, label):
    """
    Sign in as the user we just created and mint a key for itself.

    This is the whole trick: /v2/current_api_keys mints for the caller, so we
    become the caller. The key inherits exactly the permissions of the group the
    user is in — which is what makes the read-only key genuinely read-only.
    """
    user_client = CspClient()

    # Fresh users occasionally are not yet accepted by sign_in immediately after
    # the password set. Retry rather than fail the whole track setup on a race.
    jwt = None
    for attempt in range(5):
        try:
            resp = user_client.session.post(
                f"{user_client.base_url}{cfg.path('signin')}",
                json={"email": email, "password": password},
                timeout=(5, 30),
            )
            if resp.status_code == 200:
                jwt = resp.json()["jwt"]
                break
            info(f"sign-in as {email} returned {resp.status_code}, retrying")
        except Exception as exc:                       # noqa: BLE001
            info(f"sign-in as {email} failed ({exc}), retrying")
        time.sleep(2 ** attempt)

    if not jwt:
        raise SystemExit(
            f"❌ Could not sign in as {email} to mint its {label} key.\n"
            f"   The user exists but its credentials were not accepted."
        )

    user_client._set_jwt(jwt)
    user_client.switch_account(account_id)

    expires_at = key_expiry()
    created = user_client.post(cfg.path("current_api_keys"), json_body={
        "name": f"mcp-{label.replace('/', '-')}-{cfg.PARTICIPANT_ID}",
        # Required. See key_expiry() — omitting this is a 400, not a default.
        "expires_at": expires_at,
    })
    result = created.get("result", created)

    key = result.get("key")
    key_id = (result.get("id") or "").split("/")[-1]
    if not key:
        raise SystemExit(
            f"❌ no plaintext key in the {label} response. "
            f"Fields returned: {sorted(result)}"
        )

    ok(f"minted {label} Service API key ({key_id}), expires {expires_at}")
    return key_id, key


def verify_key(api_key, label):
    """
    Confirm a key authenticates, using the same header the Infoblox MCP Server
    uses: `Authorization: Token <key>`.

    A read is enough to prove the key is live and its role attached. We do NOT
    attempt a write with the read/write key — that would mutate the tenant
    during setup, and the entire write-safety story is that writes happen only
    when the operator approves them.
    """
    client = CspClient.from_service_key(api_key)
    try:
        views = client.list_results(cfg.path("dns_view"))
    except CspError as exc:
        return False, f"{label} key could not read DNS views: {exc}"
    info(f"{label} key authenticated, sees {len(views)} DNS view(s)")
    return True, None


def main():
    parser = argparse.ArgumentParser(
        description="Provision the participant's MCP users and Service API keys."
    )
    parser.add_argument("--verify", action="store_true",
                        help="verify existing keys instead of creating new ones")
    args = parser.parse_args()

    cfg.require_env()

    try:
        if args.verify:
            failures = []
            for spec in ROLES.values():
                passed, reason = verify_key(read_state(spec["key_state"]),
                                            spec["label"])
                if not passed:
                    failures.append(reason)
            if failures:
                for reason in failures:
                    print(f"❌ {reason}", flush=True)
                return 1
            ok("both Service API keys authenticate")
            return 0

        admin = CspClient.as_admin()
        account_id = admin.account_id
        ok(f"authenticated; sandbox account {account_id}")

        role_groups = resolve_role_groups(admin)

        for role, spec in ROLES.items():
            name = f"{spec['user_prefix']}-{cfg.PARTICIPANT_ID}"
            email = f"{name}@{cfg.USER_DOMAIN}"
            password = generate_password()

            user_id = ensure_user(admin, name, email,
                                  role_groups[role], password)
            key_id, key = mint_key_as_user(email, password, account_id,
                                           spec["label"])

            write_state(spec["key_id_state"], key_id)
            write_state(spec["key_state"], key, secret=True)
            spec["_user_id"] = user_id

        # Recorded so revoke_mcp_keys.py can delete every user at teardown,
        # however many were created.
        write_state("mcp_service_user_id",
                    ",".join(ROLES[r]["_user_id"] for r in ROLES))

    except LabTodo as todo:
        print(f"\n❌ Unresolved TODO reached while provisioning keys:\n"
              f"   {todo}\n   Fill this in at lab_config.PATHS and re-run.\n",
              flush=True)
        return 1

    print(f"\n{'=' * 62}", flush=True)
    print("🔐 MCP credentials provisioned", flush=True)
    for spec in ROLES.values():
        print(f"   {spec['label']:<11} user mcp-… ({spec['_user_id']}), "
              f"key id {read_state(spec['key_id_state'])}", flush=True)
    print(f"   Keys written to mcp_ro_key.txt / mcp_rw_key.txt (mode 0600)",
          flush=True)
    print(f"   MCP endpoint: {cfg.MCP_SERVER_URL}", flush=True)
    print(f"{'=' * 62}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

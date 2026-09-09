#!/usr/bin/env python3
"""
Register a NIOS-X host and turn on its DNS service.

    python3 niosx_host.py --token        mint a join token, print it, exit
    python3 niosx_host.py                wait for the host, enable DNS
    python3 niosx_host.py --status       report only, change nothing
    python3 niosx_host.py --wait-only    wait for registration, enable nothing

WHY A HOST AND NOT A SERVICE DEPLOYMENT
---------------------------------------
Part 3 originally tried NIOS-X as a Service: `universalservices` and
`endpoints` in an Infoblox point of presence, reached over IPsec. A sandbox
tenant cannot do that. Every service location the lab offered came back

    HTTP 400: Service location us-east-1 not supported

because placing an endpoint in a PoP is an entitlement, and a broker-allocated
sandbox has none. No payload fixes that.

A NIOS-X *host* needs no PoP. It is an EC2 instance built from a privately
shared Infoblox AMI whose entire bootstrap is a join token: on first boot it
calls csp.infoblox.com, registers against the tenant that issued the token, and
appears under Infrastructure > Hosts. It runs in our own VPC, so there is no
tunnel, no access location and no circular dependency on VPN addresses.

The approach is lifted from nios-rpz-genai-block, which does the same thing for
a DNS Forwarding Proxy. What differs here is the service type — `dns` rather
than `dfp` — and that this host is authoritative for the lab's zone rather than
forwarding for a desktop.

THE SPLIT WITH TERRAFORM
------------------------
Terraform can launch the instance but cannot finish the job: the service has to
be attached to the host's *pool*, and the pool does not exist until the host has
registered, which happens minutes after `apply` returns. There is nothing for
Terraform to reference, so the CSP-side half lives here.

Order at track start:

    1. niosx_host.py --token     -> TF_VAR_infoblox_join_token
    2. terraform apply           -> the instance boots and registers
    3. niosx_host.py             -> wait for the host, enable DNS on its pool
"""

import argparse
import contextlib
import sys
import time
from datetime import datetime, timezone

import lab_config as cfg
from csp_client import (CspClient, CspError, info, object_url, ok,
                        read_state, write_state)


# Host activation has moved between releases, so the tenant is asked rather
# than assumed. These are POST-only, so they cannot be probed with a GET —
# they are tried in order on the real call.
JOIN_TOKEN_PATHS = (
    "/atlas-host-activation/v1/jointoken",
    "/api/host-activation/v1/jointoken",
    "/atlas-host-activation/v1/join_token",
)

# What the infra plane calls an authoritative DNS service. `dns` is the
# capability name on a Universal Service so it is tried first; the infra plane
# has used the longer form too.
DNS_SERVICE_TYPES = ("dns", "dns_server", "authoritative_dns")

# States a host will never recover from. Anything else — including a value
# nobody here has seen — is treated as possibly-fine, for the reason in
# host_is_ready().
TERMINAL_STATES = ("error", "failed", "disconnected", "terminated", "deleted",
                   "unavailable")

# Fields that might carry a status. Used for REPORTING, not for deciding.
# The Portal shows a NIOS-X server's health as several separate things, so
# there is no single field to read even if the vocabulary were documented.
STATUS_KEYS = ("connection_status", "status", "composite_status", "state",
               "host_status", "current_state", "desired_state",
               "configuration_status", "platform_status", "application_status",
               "composite_state", "last_seen", "version")

RUNNING_STATES = ("start", "started", "running", "active", "online", "ready")


def _bare(value):
    """Trailing segment of a resource URI: infra/pool/X -> X."""
    return str(value or "").split("/")[-1]


# --------------------------------------------------------------------------- #
# Join token
# --------------------------------------------------------------------------- #

def create_join_token(client, name=None):
    """
    Mint the token that enrols the host into this tenant.

    It goes into the instance's cloud-config as `host_setup: jointoken:`.
    Nothing else is needed to register a host.
    """
    name = name or f"instruqt-{cfg.PARTICIPANT_ID}"
    last = None

    for path in JOIN_TOKEN_PATHS:
        try:
            body = client.post(path, json_body={"name": name})
        except CspError as exc:
            # 404 means the wrong path; keep looking. Anything else is a real
            # failure on the right path and must surface now.
            if exc.status == 404:
                last = exc
                continue
            raise

        result = body.get("result") or {}
        token = (body.get("join_token") or result.get("join_token")
                 or body.get("token") or result.get("token"))
        if token:
            ok(f"created join token {name!r} via {path}")
            write_state("join_token_name", name)
            return token, name
        last = CspError("POST", path, 200, f"no join_token in response: {body}")

    raise CspError("POST", JOIN_TOKEN_PATHS[0], 404,
                   f"no host-activation endpoint answered. Tried "
                   f"{', '.join(JOIN_TOKEN_PATHS)}. Last error: {last}")


# --------------------------------------------------------------------------- #
# Finding the host
# --------------------------------------------------------------------------- #

# Addresses that appear in host records and identify nothing. A NIOS-X host
# reports 0.0.0.0 for an interface it has not bound, and matching on it would
# make every host look like every other host.
PLACEHOLDER_ADDRESSES = {"0.0.0.0", "255.255.255.255", "::", "127.0.0.1"}


def _host_addresses(host):
    """Every usable IP string anywhere in a host record."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("address", "ip_address", "ipv4_address") \
                        and isinstance(value, str):
                    addr = value.split("/")[0].strip()
                    if addr and addr not in PLACEHOLDER_ADDRESSES:
                        found.add(addr)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(host)
    return found


def _pool_id(host):
    """
    The host's pool id, searched rather than assumed.

    The obvious read is host["pool"]["pool_id"], but that shape is not
    guaranteed across releases and a missing pool id is what makes a readiness
    check fail closed on a host that is actually fine. Walking the record for
    anything pool-shaped costs nothing and cannot be wrong the same way.
    """
    pool = host.get("pool")
    if isinstance(pool, dict):
        found = pool.get("pool_id") or pool.get("id")
        if found:
            return found
    if isinstance(pool, str) and pool:
        return pool
    for key in ("pool_id", "poolId"):
        if host.get(key):
            return host[key]

    result = []

    def walk(node):
        if result:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("pool_id", "poolId") and isinstance(value, str) and value:
                    result.append(value)
                    return
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(host)
    return result[0] if result else None


def find_host(client, ip=None):
    """
    This lab's NIOS-X host. Three strategies, most reliable first.

      1. By join-token name. A host enrolled with a token registers as
         ZTP_<token name>_<suffix>, which is ours by construction — better than
         guessing at addresses.
      2. By private IP. Correct only once the host has reported its interfaces,
         which does not happen immediately.
      3. The only host in the tenant. A sandbox has exactly one, so this is
         safe here in a way it would not be in a real deployment.

    Address matching alone is not enough: interfaces are absent from the record
    for the first minutes after registration, exactly when this is polled.
    """
    ip = ip or cfg.NIOSX_HOST_IP
    hosts = client.list_results(cfg.path("detail_hosts"))
    if not hosts:
        return None

    token_name = read_state("join_token_name", required=False)
    if token_name:
        for host in hosts:
            name = str(host.get("display_name") or host.get("host_name") or "")
            if token_name.lower() in name.lower():
                return host

    for host in hosts:
        if ip and ip in _host_addresses(host):
            return host

    if len(hosts) == 1:
        return hosts[0]

    info(f"{len(hosts)} hosts in the tenant and none matches token name "
         f"{token_name!r} or address {ip}")
    return None


def host_status(host):
    """Every status-looking field the record actually exposes."""
    return {key: host[key] for key in STATUS_KEYS
            if key in host and host[key] not in (None, "")}


def describe_host(host):
    """One line naming the host and whatever the CSP says about it."""
    name = (host.get("display_name") or host.get("host_name")
            or host.get("id") or "?")
    status = host_status(host)
    rendered = ", ".join(f"{k}={v}" for k, v in status.items()) \
        or "no status fields"
    return f"{name} ({rendered}, pool={_pool_id(host) or 'none'})"


def host_is_ready(host):
    """
    Whether the host is far enough along to attach a service to.

    DELIBERATELY PERMISSIVE, and that is a correction rather than laziness. The
    RPZ lab learned this the hard way: an earlier version there required the
    status field to equal one of "connected"/"active"/"online"/"ready", and
    blocked for a full fifteen minutes on a host that had registered perfectly
    well, because the real value was not in that list. `detail_hosts` is not a
    documented API and its status vocabulary was never confirmed.

    So this asks only the two questions it can answer honestly: is the host in
    a state it can never recover from, and does it have a pool? The
    authoritative test is whether the service can actually be created, which
    fails with a real error message.
    """
    for key, value in host_status(host).items():
        if str(value).strip().lower() in TERMINAL_STATES:
            info(f"host reports {key}={value!r}, which will not recover")
            return False
    return bool(_pool_id(host))


def wait_for_host(client, timeout=900, interval=20, ip=None):
    """
    Block until the host is ready to have a service attached.

    A NIOS-X host takes roughly four to eight minutes from instance launch to
    appear. Polling the real signal beats a fixed sleep: it returns as soon as
    the host is there, and it fails loudly if it never arrives.

    What the host reports is logged on first sighting and whenever it changes,
    because the vocabulary is undocumented and this log is the only way anyone
    learns what it really says.
    """
    deadline = time.time() + timeout
    attempt = 0
    last_status = None

    while time.time() < deadline:
        attempt += 1
        try:
            host = find_host(client, ip)
        except CspError as exc:
            info(f"detail_hosts not readable yet: {exc}")
            host = None

        if host:
            status = host_status(host)
            if status != last_status:
                info(f"host: {describe_host(host)}")
                last_status = status
            if host_is_ready(host):
                ok(f"host is ready: {describe_host(host)}")
                return host

        remaining = int(deadline - time.time())
        state = "not registered" if not host else "registered, no pool yet"
        print(f"⏳ waiting for the NIOS-X host ({state}, attempt {attempt}, "
              f"{max(remaining, 0)}s left)...", flush=True)
        time.sleep(interval)

    # Say what was actually seen. A bare timeout sends people looking at join
    # tokens and egress rules when the host registered fine and only the
    # readiness predicate was wrong.
    seen = None
    try:
        seen = find_host(client, ip)
    except CspError:
        pass

    if seen:
        raise TimeoutError(
            f"the NIOS-X host registered but never became ready within "
            f"{timeout}s. What the CSP reports: {describe_host(seen)}. "
            f"Record keys: {', '.join(sorted(seen))}. If it looks healthy, "
            f"host_is_ready() needs to accept this state."
        )
    raise TimeoutError(
        f"the NIOS-X host did not register within {timeout}s. Check that the "
        f"join token was valid and that the instance has outbound 443 to "
        f"csp.infoblox.com."
    )


# --------------------------------------------------------------------------- #
# The DNS service
# --------------------------------------------------------------------------- #

def find_dns_service(client, pool_id=None):
    """An existing DNS service in this tenant, or None."""
    for path in (cfg.path("detail_services"), cfg.path("infra_services")):
        try:
            services = client.list_results(path)
        except CspError:
            continue
        for service in services:
            stype = str(service.get("service_type", "")).lower()
            if stype in DNS_SERVICE_TYPES:
                if pool_id and _bare(service.get("pool_id", "")) != _bare(pool_id):
                    continue
                return service
    return None


def enable_dns_service(client, pool_id, name=None):
    """
    Create the DNS service on the host's pool.

    `pool_id` must be sent in resource-URI form — `infra/pool/<uuid>` — which
    is NOT what detail_hosts hands back. Same trap as everywhere else in this
    API: full resource identifier in a body, bare id in a URL.
    """
    name = name or cfg.DNS_SERVICE_NAME
    existing = find_dns_service(client, pool_id)
    if existing:
        info(f"DNS service already present: "
             f"{existing.get('name', existing.get('id', '?'))}")
        return existing, False

    now = datetime.now(timezone.utc).isoformat()
    last = None

    for service_type in DNS_SERVICE_TYPES:
        payload = {
            "name": name,
            "service_type": service_type,
            "pool_id": f"infra/pool/{_bare(pool_id)}",
            "desired_state": "start",
            "created_at": now,
            "updated_at": now,
            "tags": {cfg.LAB_TAG_KEY: cfg.LAB_TAG_VALUE},
        }
        try:
            body = client.post(cfg.path("infra_services"), json_body=payload)
        except CspError as exc:
            # A rejected service_type is a 400 or 422; try the others. Anything
            # else is a real failure and should surface now.
            if exc.status in (400, 422):
                info(f"service_type {service_type!r} rejected: {exc.body[:160]}")
                last = exc
                continue
            raise

        service = body.get("result") or body
        ok(f"enabled DNS service {name!r} (service_type={service_type!r})")
        return service, True

    raise CspError("POST", cfg.path("infra_services"), 400,
                   f"no accepted service_type for DNS. Tried "
                   f"{', '.join(DNS_SERVICE_TYPES)}. Last error: {last}")


def service_current_state(service):
    """
    What the service IS, never what it was asked to be.

    `desired_state` is the field we set when creating it, so reading it back
    proves only that the POST body arrived. Returns None when the CSP has not
    yet said.
    """
    for key in ("current_state", "status", "state", "composite_state",
                "service_status", "operational_state"):
        value = service.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def wait_for_service(client, timeout=600, interval=20):
    """
    Block until the DNS service reports that it has actually started.

    Only current_state counts. If the CSP never publishes one, this gives up
    quietly rather than guessing — the DNS query that follows is the
    authoritative test and will settle it properly.
    """
    deadline = time.time() + timeout
    attempt = 0
    unknown_since = None

    while time.time() < deadline:
        attempt += 1
        service = find_dns_service(client)

        if not service:
            print(f"⏳ DNS service not visible yet (attempt {attempt})...",
                  flush=True)
        else:
            state = service_current_state(service)
            if state in RUNNING_STATES:
                ok(f"DNS service reports {state!r}")
                return service
            if state is None:
                unknown_since = unknown_since or time.time()
                if time.time() - unknown_since > 120:
                    info("the CSP publishes no current_state for this service; "
                         "falling through to the DNS check, which is the real "
                         "test")
                    return service
                print(f"⏳ DNS service has no current_state yet "
                      f"(attempt {attempt})...", flush=True)
            else:
                print(f"⏳ DNS service state is {state!r} (attempt {attempt})...",
                      flush=True)
        time.sleep(interval)

    info(f"DNS service did not report running within {timeout}s. It may still "
         f"be starting; the DNS check will settle it.")
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def join_server_group(client, host_id):
    """
    Put this host into the lab's DNS server group.

    THE FIELD THAT MAKES PART 2 AND PART 3 ONE STORY. Part 2's fault is a zone
    with no authoritative servers, and its fix is to point the zone at the
    server group `techcorp-dc-servers`. Until now that group had no members, so
    the fix was correct configuration that nothing could act on — which is what
    produced the complaint that the assistant "says there are no host members
    and no hosts registered at all". It was right.

    Adding the registered host as a member means Part 2's fix genuinely makes
    the zone servable, and Part 3's query against the same zone gets a real
    answer from a real server.

    DELIBERATELY DOES NOT TOUCH THE ZONE. Part 2's break lives on the zone's
    own `nsgs` field and this runs concurrently with seeding — writing the zone
    here would race the break and could silently undo it. The group is a
    different object and nothing in Part 2 changes it.
    """
    group = client.find_by_name(cfg.path("dns_auth_nsg"),
                                cfg.DNS_SERVER_GROUP_NAME)
    if not group:
        info(f"no server group {cfg.DNS_SERVER_GROUP_NAME!r} to join — seeding "
             f"may not have run yet")
        return False

    members = group.get("internal_secondaries") or []
    if any(_bare(m.get("host")) == _bare(host_id) for m in members
           if isinstance(m, dict)):
        info(f"host is already a member of {cfg.DNS_SERVER_GROUP_NAME}")
        return True

    try:
        client.patch(
            object_url(cfg.path("dns_auth_nsg"), group["id"]),
            json_body={"internal_secondaries": members + [{"host": host_id}]},
        )
    except CspError as exc:
        info(f"could not add the host to {cfg.DNS_SERVER_GROUP_NAME}: {exc}")
        return False

    ok(f"host added to server group {cfg.DNS_SERVER_GROUP_NAME}")
    return True


def build(client, timeout=900, service_timeout=300):
    """
    Wait for the host, enable DNS on it, and report what exists.

    Returns the ids for seed_ids.json. Raises TimeoutError if the host never
    registers — the caller decides whether that is fatal.
    """
    print("\n=== NIOS-X host: registration and DNS service ===", flush=True)

    host = wait_for_host(client, timeout=timeout)
    pool_id = _pool_id(host)
    if not pool_id:
        raise CspError("GET", cfg.path("detail_hosts"), 200,
                       f"host registered but exposes no pool id, so no service "
                       f"can be attached. Record keys: {', '.join(sorted(host))}")

    service, created = enable_dns_service(client, pool_id)
    if created:
        wait_for_service(client, timeout=service_timeout)

    in_group = join_server_group(client, host.get("id"))

    return {
        "niosx_in_server_group": in_group,
        "niosx_host_id": host.get("id"),
        "niosx_host_name": (host.get("display_name") or host.get("host_name")),
        "niosx_host_ip": cfg.NIOSX_HOST_IP,
        "niosx_pool_id": pool_id,
        "dns_service_id": service.get("id"),
        "dns_service_name": service.get("name", cfg.DNS_SERVICE_NAME),
    }


def show_status(client):
    """Print everything this script manages."""
    print("=" * 62)
    print("  NIOS-X host status")
    print("=" * 62)

    host = find_host(client)
    if not host:
        print("  host           NOT REGISTERED")
    else:
        print(f"  host           {describe_host(host)}")
        addresses = sorted(_host_addresses(host))
        if addresses:
            print(f"  addresses      {', '.join(addresses)}")

    service = find_dns_service(client)
    if not service:
        print("  DNS service    NOT ENABLED — nothing will answer queries")
    else:
        state = (service_current_state(service)
                 or service.get("desired_state") or "unknown")
        print(f"  DNS service    {service.get('name', '?')} "
              f"(type={service.get('service_type')}, state={state})")
    print("=" * 62)


def main():
    parser = argparse.ArgumentParser(
        description="Register the NIOS-X host and enable its DNS service.")
    parser.add_argument("--token", action="store_true",
                        help="mint a join token, print it to stdout, and exit")
    parser.add_argument("--status", action="store_true", help="report only")
    parser.add_argument("--wait-only", action="store_true",
                        help="wait for registration, enable nothing")
    parser.add_argument("--timeout", type=int, default=900,
                        help="seconds to wait for host registration")
    parser.add_argument("--account-id", default=None)
    args = parser.parse_args()

    client = CspClient.as_admin(account_id=args.account_id)

    if args.token:
        # stdout must be the token and nothing else, so setup-shell can capture
        # it with $(...). ok() and info() print to stdout, so they are
        # redirected for the duration of the call — a stray progress line here
        # would be silently baked into the instance's cloud-config as part of
        # the token, and the host would fail to register with no clue why.
        with contextlib.redirect_stdout(sys.stderr):
            token, _name = create_join_token(client)
        print(token)
        return 0

    if args.status:
        show_status(client)
        return 0

    if args.wait_only:
        wait_for_host(client, timeout=args.timeout)
        return 0

    ids = build(client, timeout=args.timeout)
    show_status(client)
    print(f"\n✅ NIOS-X host ready at {ids['niosx_host_ip']} serving DNS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError as exc:
        print(f"❌ {exc}", flush=True)
        sys.exit(1)
    except CspError as exc:
        print(f"❌ {exc}", flush=True)
        sys.exit(1)

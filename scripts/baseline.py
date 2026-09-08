#!/usr/bin/env python3
"""
Build the healthy TechCorp footprint the track breaks in Part 2 and Part 4.

Every function here is idempotent: it looks the object up by name first and
returns the existing one rather than creating a duplicate. seed_lab.py may be
re-run against a half-seeded tenant after a failed setup, and must converge.

Everything created is tagged {instruqt-lab: <track slug>} so teardown_lab.py can
identify its own objects and leave anything else in the tenant alone. The
Universal DDI host is never created and never deleted — it is part of the
sandbox the broker hands over.

Paths and payloads are taken from Infoblox's OpenAPI-generated Go client
(github.com/infobloxopen/universal-ddi-go-client), not guessed. Three of its
findings shape this file and are worth knowing before editing:

  * An AuthZone's `internal_secondaries` is a list of {"host": <id>} objects and
    is freely PATCHable. That list is what the Portal renders as "Authoritative
    DNS Servers" — the single object this whole track turns on.
  * A DHCP range hangs off `space` + `parent`, not a `subnet` field.
  * A fixed address reserves exactly ONE address, so the "reserved block" is N
    objects rather than one object with bounds.
"""

import lab_config as cfg
from csp_client import info, ok


LAB_TAGS = {cfg.LAB_TAG_KEY: cfg.LAB_TAG_VALUE}


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #

def ensure_dns_view(client, name=None):
    """The DNS view holding svc.techcorp.internal."""
    name = name or cfg.DNS_VIEW_NAME
    existing = client.find_by_name(cfg.path("dns_view"), name)
    if existing:
        info(f"DNS view {name} already present")
        return existing

    created = client.post(cfg.path("dns_view"), json_body={
        "name": name,
        "comment": "TechCorp production DNS view (Instruqt lab)",
        "tags": LAB_TAGS,
    })
    ok(f"created DNS view {name}")
    return created.get("result", created)


def ensure_auth_zone(client, view_id, fqdn=None):
    """
    The internal zone the cloud team queries.

    primary_type "cloud" means the zone data is owned by Universal DDI rather
    than by an external primary — which is what lets a Universal DDI host serve
    it, and therefore what makes the Authoritative DNS Servers list meaningful.
    Both `fqdn` and `primary_type` are read-only after creation, so getting them
    right here matters; everything else can be patched later.
    """
    fqdn = fqdn or cfg.ZONE_FQDN
    existing = client.find_by_name(cfg.path("dns_auth_zone"), fqdn, field="fqdn")
    if existing:
        info(f"auth zone {fqdn} already present")
        return existing

    created = client.post(cfg.path("dns_auth_zone"), json_body={
        "fqdn": fqdn,
        "primary_type": "cloud",
        "view": view_id,
        "comment": "TechCorp internal service zone",
        "tags": LAB_TAGS,
    })
    ok(f"created auth zone {fqdn}")
    return created.get("result", created)


def ensure_records(client, zone_id):
    """
    A and CNAME records inside the zone, including payments.svc.techcorp.internal
    — the name the INC-4471 ticket reports failing and the name Part 3's test VM
    digs for.
    """
    created = []

    for label, spec in cfg.BASELINE_A_RECORDS.items():
        created.append(_ensure_record(client, zone_id, label, "A", {
            "address": spec["address"],
        }, spec["comment"]))

    for label, spec in cfg.BASELINE_CNAME_RECORDS.items():
        # CNAME rdata is keyed `cname`, not `target`.
        created.append(_ensure_record(client, zone_id, label, "CNAME", {
            "cname": spec["target"],
        }, spec["comment"]))

    ok(f"{len(created)} records present in {cfg.ZONE_FQDN}")
    return created


def _ensure_record(client, zone_id, name_in_zone, rtype, rdata, comment):
    params = {"_filter": f'zone=="{zone_id}" and name_in_zone=="{name_in_zone}"'}
    for row in client.list_results(cfg.path("dns_record"), params=params):
        if row.get("type") == rtype:
            return row

    created = client.post(cfg.path("dns_record"), json_body={
        "zone": zone_id,
        "name_in_zone": name_in_zone,
        "type": rtype,
        "rdata": rdata,
        "comment": comment,
        "tags": LAB_TAGS,
    })
    return created.get("result", created)


def find_dc_host(client):
    """
    The Universal DDI host serving the data-centre network — [DNS SERVER] in the
    track flow, and the host Part 2's break removes from the zone.

    Not created here: the host is part of the sandbox the broker hands us. We
    try the configured name first, then auto-discover — if exactly one host
    exists in the tenant it is unambiguously the one, so we use it and log the
    name so the operator can pin LAB_DC_HOST for future runs.

    Zero hosts, or several with no name match, is a hard failure. There is no
    safe way to guess which host should be authoritative for the zone, and
    guessing wrong produces a lab where the break is real but the fix the
    participant is told to make is the wrong one.
    """
    host = (client.find_by_name(cfg.path("dns_host"), cfg.DC_HOST_NAME)
            or client.find_by_name(cfg.path("dns_host"), cfg.DC_HOST_NAME,
                                   field="absolute_name"))
    if host:
        return host

    all_hosts = client.list_results(cfg.path("dns_host"))
    if len(all_hosts) == 1:
        host = all_hosts[0]
        discovered = host.get("name") or host.get("absolute_name", "<unnamed>")
        print(
            f"⚠️  LAB_DC_HOST={cfg.DC_HOST_NAME!r} not matched; auto-selected "
            f"the only host in this tenant: {discovered!r}.\n"
            f"   Pin it for future runs: export LAB_DC_HOST={discovered!r}\n"
            f"   NOTE: the assignment prose names {cfg.DC_HOST_NAME!r}. If the "
            f"discovered name differs, 02/assignment.md reads wrong to the "
            f"participant — set LAB_DC_HOST as an Instruqt secret instead.",
            flush=True,
        )
        return host

    names = [h.get("name") or h.get("absolute_name", "<unnamed>") for h in all_hosts]
    if all_hosts:
        raise SystemExit(
            f"❌ LAB_DC_HOST={cfg.DC_HOST_NAME!r} not found and there are "
            f"{len(all_hosts)} hosts — cannot auto-select.\n"
            f"   Set LAB_DC_HOST to one of: {', '.join(names)}"
        )
    raise SystemExit(
        f"❌ No DNS hosts found in this tenant at all.\n"
        f"   The broker sandbox must have at least one Universal DDI host "
        f"registered before this track can seed.\n"
        f"   Check that allocation_subtenant.py succeeded and the sandbox is "
        f"healthy."
    )


def set_authoritative_servers(client, zone_id, host_ids):
    """
    Write the zone's Authoritative DNS Servers list.

    `internal_secondaries` is the API name for the list the Infoblox Portal
    labels "Authoritative DNS Servers" on an auth zone. Each entry is
    {"host": "<dns/host resource id>"}. The field is a plain PATCH, which is
    what makes Part 2 work end to end: the participant can add the host through
    the agent or through the Portal, and check_c2() reads the same field either
    way.

    Passing an empty list is the break; passing [dc_host] is the healthy state
    and the fix.
    """
    client.patch(cfg.path("dns_auth_zone") + f"/{zone_id}", json_body={
        "internal_secondaries": [{"host": host_id} for host_id in host_ids],
    })
    return host_ids


def authoritative_server_ids(client, zone_id):
    """The host ids currently on the zone's Authoritative DNS Servers list."""
    zone = client.get(cfg.path("dns_auth_zone") + f"/{zone_id}")
    zone = zone.get("result", zone)
    return [
        entry.get("host")
        for entry in (zone.get("internal_secondaries") or [])
        if entry.get("host")
    ]


# --------------------------------------------------------------------------- #
# IPAM / DHCP
# --------------------------------------------------------------------------- #

def ensure_ip_space(client):
    existing = client.find_by_name(cfg.path("ipam_ip_space"), cfg.IP_SPACE_NAME)
    if existing:
        info(f"IP space {cfg.IP_SPACE_NAME} already present")
        return existing

    created = client.post(cfg.path("ipam_ip_space"), json_body={
        "name": cfg.IP_SPACE_NAME,
        "comment": "TechCorp authoritative IP space (Instruqt lab)",
        "tags": LAB_TAGS,
    })
    ok(f"created IP space {cfg.IP_SPACE_NAME}")
    return created.get("result", created)


def ensure_address_block(client, space_id):
    block = cfg.ADDRESS_BLOCK
    existing = client.find_by_name(cfg.path("ipam_address_block"), block["name"])
    if existing:
        info(f"address block {block['address']}/{block['cidr']} already present")
        return existing

    created = client.post(cfg.path("ipam_address_block"), json_body={
        "address": block["address"],
        "cidr": block["cidr"],
        "space": space_id,
        "name": block["name"],
        "tags": LAB_TAGS,
    })
    ok(f"created address block {block['address']}/{block['cidr']}")
    return created.get("result", created)


def ensure_subnets(client, space_id):
    """DC-01 ([NETWORK]) and Branch-02 ([SUBNET])."""
    subnets = {}
    for key, spec in cfg.SUBNETS.items():
        existing = client.find_by_name(cfg.path("ipam_subnet"), spec["name"])
        if existing:
            info(f"subnet {spec['name']} already present")
            subnets[key] = existing
            continue

        created = client.post(cfg.path("ipam_subnet"), json_body={
            "address": spec["address"],
            "cidr": spec["cidr"],
            "space": space_id,
            "name": spec["name"],
            "comment": spec["comment"],
            "tags": LAB_TAGS,
        })
        ok(f"created subnet {spec['name']} ({spec['address']}/{spec['cidr']})")
        subnets[key] = created.get("result", created)
    return subnets


def ensure_reserved_block(client, space_id, subnet_id):
    """
    The reserved addresses in Branch-02 that the DHCP range must not collide
    with. Seeded healthy; Part 4's break moves the RANGE onto them, not the
    other way round, so these objects stay constant across the whole track.

    A fixed address in Universal DDI reserves exactly ONE address — the object
    has a single `address` field, not bounds. So a reserved "block" is N
    objects, one per address across .10-.30. That is what makes the Part 4
    symptom real: once the range is narrowed onto this span every address in it
    is already spoken for and utilization pins at 100%.

    `match_type`/`match_value` are required. Synthetic MACs derived from the
    address keep them deterministic across re-seeds.
    """
    reserved = cfg.BRANCH_RESERVED_BLOCK
    existing = {
        row.get("address")
        for row in client.list_results(cfg.path("dhcp_fixed_address"))
        if (row.get("tags") or {}).get(cfg.LAB_TAG_KEY) == cfg.LAB_TAG_VALUE
    }

    created = []
    for address in addresses_between(reserved["start"], reserved["end"]):
        if address in existing:
            continue
        octets = address.split(".")
        mac = "02:42:" + ":".join(f"{int(o):02x}" for o in octets)
        row = client.post(cfg.path("dhcp_fixed_address"), json_body={
            "address": address,
            "ip_space": space_id,
            "parent": subnet_id,
            "name": f"{reserved['name']} {address}",
            "comment": "Branch-02 infrastructure - do not assign",
            "match_type": "mac",
            "match_value": mac,
            "tags": LAB_TAGS,
        })
        created.append(row.get("result", row))

    if created:
        ok(f"created {len(created)} fixed addresses across "
           f"{reserved['start']}-{reserved['end']}")
    else:
        info(f"reserved addresses {reserved['start']}-{reserved['end']} "
             f"already present")
    return created


def ensure_dhcp_range(client, space_id, subnet_id, bounds=None):
    """
    The Branch-02 DHCP range. Seeded healthy (.100-.200); Part 4's break narrows
    it onto the reserved addresses.

    A range hangs off the IP space via `space` and off its subnet via `parent` —
    there is no `subnet` field.
    """
    bounds = bounds or cfg.BRANCH_RANGE_HEALTHY
    existing = client.find_by_name(cfg.path("dhcp_range"), bounds["name"])
    if existing:
        info(f"DHCP range {bounds['name']} already present")
        return existing

    created = client.post(cfg.path("dhcp_range"), json_body={
        "start": bounds["start"],
        "end": bounds["end"],
        "space": space_id,
        "parent": subnet_id,
        "name": bounds["name"],
        "comment": "Branch-02 client pool",
        "tags": LAB_TAGS,
    })
    ok(f"created DHCP range {bounds['start']}-{bounds['end']}")
    return created.get("result", created)


def addresses_between(start, end):
    """Every IPv4 address from start to end inclusive."""
    def to_int(addr):
        a, b, c, d = (int(part) for part in addr.split("."))
        return (a << 24) | (b << 16) | (c << 8) | d

    def to_str(value):
        return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))

    return [to_str(value) for value in range(to_int(start), to_int(end) + 1)]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build_all(client):
    """
    Build the entire healthy footprint and return the ids the break functions
    and the checks need. Idempotent end to end.
    """
    print("\n=== Baseline: DNS ===", flush=True)
    view = ensure_dns_view(client)
    zone = ensure_auth_zone(client, view["id"])
    ensure_records(client, zone["id"])
    dc_host = find_dc_host(client)

    # Healthy state: the DC host IS on the zone's Authoritative DNS Servers
    # list. Part 2's break takes it off.
    set_authoritative_servers(client, zone["id"], [dc_host["id"]])
    ok(f"{cfg.ZONE_FQDN} is authoritative on "
       f"{dc_host.get('name') or dc_host.get('absolute_name')}")

    print("\n=== Baseline: IPAM / DHCP ===", flush=True)
    space = ensure_ip_space(client)
    ensure_address_block(client, space["id"])
    subnets = ensure_subnets(client, space["id"])
    branch = subnets["branch-02"]
    ensure_reserved_block(client, space["id"], branch["id"])
    dhcp_range = ensure_dhcp_range(client, space["id"], branch["id"])

    return {
        "view_id": view["id"],
        "zone_id": zone["id"],
        "dc_host_id": dc_host["id"],
        "dc_host_name": dc_host.get("name") or dc_host.get("absolute_name"),
        "space_id": space["id"],
        "dc_subnet_id": subnets["dc-01"]["id"],
        "branch_subnet_id": branch["id"],
        "range_id": dhcp_range["id"],
    }

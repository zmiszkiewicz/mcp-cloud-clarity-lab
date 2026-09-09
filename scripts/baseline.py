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
from csp_client import info, object_url, ok


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


def inventory(client):
    """
    What this tenant actually contains, as far as DNS authority is concerned.

    Exists because "no DNS hosts" and "no hosts" are different statements and
    the first was being reported as the second. `/dns/host` lists only hosts
    with a DNS view of themselves; a host registered in the infrastructure but
    with no DNS service on it does not appear there. Telling an operator "no
    hosts at all" when three are registered would send them to debug the wrong
    thing.
    """
    def safe(key):
        try:
            return client.list_results(cfg.path(key))
        except Exception as exc:                        # noqa: BLE001
            info(f"could not list {key}: {exc}")
            return []

    return {
        "dns_host": safe("dns_host"),
        "infra_hosts": safe("infra_hosts"),
        "infra_services": safe("infra_services"),
        "dns_auth_nsg": safe("dns_auth_nsg"),
    }


def _describe_inventory(data):
    def names(rows):
        return [r.get("name") or r.get("absolute_name") or r.get("display_name")
                or "<unnamed>" for r in rows] or ["(none)"]

    return (
        f"   DNS hosts (/dns/host):        {', '.join(names(data['dns_host']))}\n"
        f"   Infra hosts (/infra/hosts):   {', '.join(names(data['infra_hosts']))}\n"
        f"   Services (/infra/services):   {', '.join(names(data['infra_services']))}\n"
        f"   Server groups (/auth_nsg):    {', '.join(names(data['dns_auth_nsg']))}\n"
    )


def find_dc_host(client, data=None):
    """
    The Universal DDI host serving the data-centre network, or None.

    Not created here: a host is part of the sandbox, if the sandbox has one.
    Tries the configured name first, then auto-selects when exactly one host
    exists. Several hosts with no name match returns None rather than guessing —
    picking the wrong one produces a lab where the break is real but the fix the
    participant is told to make is the wrong one.
    """
    hosts = (data or {}).get("dns_host")
    if hosts is None:
        hosts = client.list_results(cfg.path("dns_host"))

    for field in ("name", "absolute_name"):
        match = next((h for h in hosts if h.get(field) == cfg.DC_HOST_NAME), None)
        if match:
            return match

    if len(hosts) == 1:
        host = hosts[0]
        discovered = host.get("name") or host.get("absolute_name", "<unnamed>")
        print(
            f"⚠️  LAB_DC_HOST={cfg.DC_HOST_NAME!r} not matched; auto-selected "
            f"the only host in this tenant: {discovered!r}.\n"
            f"   Pin it for future runs by setting LAB_DC_HOST={discovered!r}.",
            flush=True,
        )
        return host

    if hosts:
        names = [h.get("name") or h.get("absolute_name", "<unnamed>") for h in hosts]
        print(f"⚠️  LAB_DC_HOST={cfg.DC_HOST_NAME!r} not found and there are "
              f"{len(hosts)} hosts — refusing to guess. Set LAB_DC_HOST to one "
              f"of: {', '.join(names)}", flush=True)
    return None


def ensure_dns_server_group(client):
    """
    The DNS Server Group that stands in for the data-centre servers.

    An AuthNSG requires only a `name`, which is the whole reason this path
    exists: it gives the track a real, Portal-visible object to make
    authoritative for the zone without needing a registered host.
    """
    existing = client.find_by_name(cfg.path("dns_auth_nsg"),
                                   cfg.DNS_SERVER_GROUP_NAME)
    if existing:
        info(f"DNS server group {cfg.DNS_SERVER_GROUP_NAME} already present")
        return existing

    created = client.post(cfg.path("dns_auth_nsg"), json_body={
        "name": cfg.DNS_SERVER_GROUP_NAME,
        "comment": "Data centre authoritative DNS servers",
        "tags": LAB_TAGS,
    })
    ok(f"created DNS server group {cfg.DNS_SERVER_GROUP_NAME}")
    return created.get("result", created)


def resolve_dns_authority(client):
    """
    Decide WHAT gets made authoritative for the zone, and return a descriptor:

        {"mode": "host"|"nsg", "id": <resource id>, "name": <display name>}

    Honours LAB_AUTH_MODE. In `auto` — the default — a registered host wins
    because it is the higher-fidelity story, and a server group is the fallback
    when the tenant has none.

    A hard failure here only happens when the operator explicitly asked for
    `host` and there is none. Failing a track start on a missing host when a
    perfectly good host-free path exists would be a bad trade.
    """
    mode = cfg.AUTH_MODE
    if mode not in ("auto", "host", "nsg"):
        raise SystemExit(
            f"❌ LAB_AUTH_MODE={mode!r} is not one of auto, host, nsg."
        )

    data = inventory(client)
    print("── tenant inventory ──\n" + _describe_inventory(data), flush=True)

    if mode in ("auto", "host"):
        host = find_dc_host(client, data)
        if host:
            name = host.get("name") or host.get("absolute_name")
            ok(f"DNS authority: host {name} (LAB_AUTH_MODE={mode})")
            return {"mode": "host", "id": host["id"], "name": name}

        if mode == "host":
            raise SystemExit(
                "❌ LAB_AUTH_MODE=host was requested but this tenant has no "
                "usable Universal DDI host.\n"
                + _describe_inventory(data)
                + "   Either register a host in the sandbox, pin LAB_DC_HOST to "
                  "one of the names above, or use LAB_AUTH_MODE=nsg / auto to "
                  "run the host-free variant."
            )

        print("ℹ️  No Universal DDI host in this tenant — falling back to a DNS "
              "server group. The configuration fault is identical; what is lost "
              "is a server that genuinely answers, so there is no live NXDOMAIN "
              "to dig for. Set LAB_AUTH_MODE=host once the sandbox ships a "
              "host.", flush=True)

    group = ensure_dns_server_group(client)
    ok(f"DNS authority: server group {cfg.DNS_SERVER_GROUP_NAME} "
       f"(LAB_AUTH_MODE={mode})")
    return {"mode": "nsg", "id": group["id"],
            "name": cfg.DNS_SERVER_GROUP_NAME}


def set_authoritative_servers(client, zone_id, authority, attached=True):
    """
    Write the zone's Authoritative DNS Servers list.

    Both fields below are what the Infoblox Portal renders under "Authoritative
    DNS Servers" on a zone's edit page — `internal_secondaries` for hosts,
    `nsgs` for server groups. Both are plain PATCHes, which is what makes Part 2
    work end to end: the participant can fix it through the assistant or through
    the Portal, and check_c2() reads back the same field either way.

    `attached=False` is the break; True is the healthy state and the fix.
    """
    if authority["mode"] == "host":
        body = {"internal_secondaries":
                [{"host": authority["id"]}] if attached else []}
    else:
        body = {"nsgs": [authority["id"]] if attached else []}

    client.patch(object_url(cfg.path("dns_auth_zone"), zone_id), json_body=body)
    return body


def authoritative_server_ids(client, zone_id):
    """
    Everything currently on the zone's Authoritative DNS Servers list, in both
    representations, as a flat list of resource ids.

    Reads both regardless of mode on purpose. A participant who fixes this in
    the Portal might well add whichever kind the UI offered them, and the check
    should credit that rather than insisting on the one the seeder used.
    """
    zone = client.get(object_url(cfg.path("dns_auth_zone"), zone_id))
    zone = zone.get("result", zone)

    ids = [entry.get("host")
           for entry in (zone.get("internal_secondaries") or [])
           if entry.get("host")]
    ids += [nsg for nsg in (zone.get("nsgs") or []) if nsg]
    return ids


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


def ensure_reserved_block(client, space_id):
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
        # NO `parent` HERE. The live API rejects it —
        #   "The 'parent' field is read-only and cannot be provided."
        # — even though the OpenAPI-generated Go client documents it as an
        # ordinary optional writable. CSP derives the parent subnet from
        # `ip_space` plus the address itself, so passing it adds nothing.
        row = client.post(cfg.path("dhcp_fixed_address"), json_body={
            "address": address,
            "ip_space": space_id,
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


def ensure_dhcp_range(client, space_id, bounds=None):
    """
    The Branch-02 DHCP range. Seeded healthy (.100-.200); Part 4's break narrows
    it onto the reserved addresses.

    A range hangs off the IP space via `space`. It also has a `parent` naming
    its subnet, but that field is READ-ONLY on the live API — same story as
    fixed_address, and same disagreement with the OpenAPI client, which
    documents it as writable. CSP derives the parent from `space` plus the
    range bounds, so 10.30.2.100-.200 lands under Branch-02 on its own.
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
    authority = resolve_dns_authority(client)

    # Healthy state: the DNS server IS on the zone's Authoritative DNS Servers
    # list. Part 2's break takes it off.
    set_authoritative_servers(client, zone["id"], authority, attached=True)
    ok(f"{cfg.ZONE_FQDN} is authoritative on {authority['name']}")

    # Part 3's DNS server is NOT built here.
    #
    # It used to be: scripts/service_deployment.py tried to create a NIOS-X as
    # a Service deployment over /api/universalinfra. A sandbox tenant cannot
    # have one — every service location was refused, because placing an
    # endpoint in an Infoblox point of presence is an entitlement a sandbox
    # does not get.
    #
    # What works instead is a NIOS-X *host*: an EC2 instance in the lab's own
    # VPC that registers itself with a join token. That needs the VPC to exist,
    # so it happens in scripts/niosx_host.py, driven from
    # /opt/lab/build-niosx.sh, concurrently with this seeding rather than
    # inside it. service_deployment.py is kept for the day a tenant does have
    # the entitlement.
    service = {}

    print("\n=== Baseline: IPAM / DHCP ===", flush=True)
    space = ensure_ip_space(client)
    ensure_address_block(client, space["id"])
    subnets = ensure_subnets(client, space["id"])
    branch = subnets["branch-02"]
    ensure_reserved_block(client, space["id"])
    dhcp_range = ensure_dhcp_range(client, space["id"])

    return {
        "view_id": view["id"],
        "zone_id": zone["id"],
        # The descriptor the break, the fix and check_c2 all work from, plus
        # flattened copies so a human reading seed_ids.json can see at a glance
        # which variant this tenant got.
        "authority": authority,
        "auth_mode": authority["mode"],
        "dns_server_id": authority["id"],
        "dns_server_name": authority["name"],
        "space_id": space["id"],
        # Part 3's ids, already namespaced. Empty when the tenant could not
        # support them; Part 3's check reports that rather than KeyError-ing.
        **service,
        "dc_subnet_id": subnets["dc-01"]["id"],
        "branch_subnet_id": branch["id"],
        "range_id": dhcp_range["id"],
    }

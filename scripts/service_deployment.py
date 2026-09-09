#!/usr/bin/env python3
"""
Build the NIOS-X as a Service side of Part 3, at track start.

WHY SEEDING AND NOT THE PARTICIPANT
-----------------------------------
The Infoblox MCP Server is read-only, so nothing in the lab can create these
objects during Part 3 — not the assistant, not the participant through it. The
alternative was roughly twenty-five Portal form fields before the interesting
part began, which is not a lab, it is a data-entry exercise.

So the whole Infoblox side is built here, over the CSP REST API with admin
credentials, before the participant sees anything. Part 3 then becomes what it
was always meant to be: one sentence, and the assistant wires up the AWS half
against a service that already exists.

ORDER IS STRICT. Each object needs the id of the one above it:

    universalservices  ->  endpoints  ->  accesslocations

WHAT IS NOT BUILT HERE, AND WHY
-------------------------------
The Access Location needs `wan_ip_addresses` — the customer-side public IPs
that will initiate the IPsec tunnels. On AWS those belong to a Site-to-Site VPN
connection, which does not exist until Part 3 creates it. So it genuinely
cannot be pre-created: it depends on something the participant has not done
yet. `ensure_access_location()` exists and works, but it is only callable once
those addresses are known.

Paths and payload shapes were read out of the MCP server's own tool schemas
against a live tenant; they are not in the public universal-ddi-go-client.
"""

import lab_config as cfg
from csp_client import CspError, info, object_url, ok


LAB_TAGS = {cfg.LAB_TAG_KEY: cfg.LAB_TAG_VALUE}


class ServiceSeedingUnavailable(RuntimeError):
    """
    A prerequisite this tenant does not have.

    Distinct from a bug: a sandbox with no configured Locations or no VPN
    credential cannot have an Access Location, and that is a property of the
    tenant rather than a fault in this script. seed_lab.py treats it as a
    warning so Parts 1, 2 and 4 still come up.
    """


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #

def pick_size(client, wanted=None):
    """
    A valid endpoint size. Validated rather than assumed — the accepted values
    are whatever GET /supportedsizes says, and they differ between tenants.
    """
    wanted = wanted or cfg.ENDPOINT_SIZE
    try:
        sizes = client.list_results(cfg.path("supported_sizes"))
    except CspError as exc:
        info(f"could not list supported sizes ({exc}); using {wanted} unchecked")
        return wanted

    names = [row.get("name") or row.get("size") or row
             for row in sizes] if sizes else []
    names = [n for n in names if isinstance(n, str)]

    if not names:
        info(f"no sizes reported; using {wanted} unchecked")
        return wanted
    if wanted in names:
        return wanted

    # Case-insensitive match before giving up — SMALL vs Small is not worth
    # failing a track start over.
    for name in names:
        if name.lower() == wanted.lower():
            return name

    info(f"size {wanted!r} not offered; this tenant supports {names}. "
         f"Using {names[0]!r}.")
    return names[0]


def pick_location(client):
    """
    A Location for the Access Location to sit at.

    Not created here. Locations carry a real postal address and are an account
    -level concept; inventing one would put fictional geography in a customer's
    tenant. If the sandbox has none, the Access Location is skipped and Part 3
    says so.
    """
    locations = client.list_results(cfg.path("infra_locations"))
    if not locations:
        raise ServiceSeedingUnavailable(
            "this tenant has no Locations (GET /api/infra/v1/locations is "
            "empty). An Access Location requires one, and Locations need a "
            "country and postcode that only a human should choose. Create one "
            "in the Portal under Configure > Locations."
        )
    chosen = locations[0]
    info(f"using location {chosen.get('name', chosen.get('id'))}")
    return chosen["id"]


# --------------------------------------------------------------------------- #
# The objects
# --------------------------------------------------------------------------- #

# Spellings to try for the DNS capability, in order of confidence.
#
# A live sandbox answered `{"type": "DNS"}` with
#
#     HTTP 400: HTTP interceptor error: capability 'DNS' is not allowed
#
# which is ambiguous between two very different causes: the enum wants a
# different string, or the account is not entitled to DNS as a universal
# service capability at all. Cheaper to try the plausible spellings than to
# guess which, and the fallback below distinguishes them either way.
CAPABILITY_TYPES = ("DNS", "dns", "DNS_SERVER")


def _capability_refused(exc):
    """
    True when CSP rejected the capability specifically, rather than the rest of
    the request. Narrow on purpose — retrying a different capability against a
    400 caused by a bad name or a duplicate would just produce three identical
    failures and a confusing log.
    """
    return exc.status == 400 and "capability" in (exc.body or "").lower()


def _report_allowed_capabilities(client, body):
    """
    Ask the interceptor what it WOULD accept, by sending something it cannot.

    Whoever has to get this sandbox entitled for DNS needs to know what it is
    entitled for now, and "capability 'DNS' is not allowed" does not say. So
    send a capability type that certainly does not exist: the request is
    guaranteed to fail, which means it creates nothing, and validation errors
    of this kind often enumerate the permitted values in the rejection.

    Purely diagnostic. Any outcome is fine; nothing downstream depends on it.
    """
    probe = "ZZZ_NOT_A_CAPABILITY"
    try:
        client.post(cfg.path("universal_service"),
                    json_body=dict(body, capabilities=[{"type": probe}]))
    except CspError as exc:
        detail = (exc.body or "").strip()
        # Only worth printing if it says something the earlier refusals did
        # not — otherwise it is the same sentence with a different noun.
        if detail and probe not in detail:
            print(f"    the API's response to an invalid capability, which may "
                  f"name the allowed set:\n      {detail[:300]}", flush=True)
        return
    except Exception:                                   # noqa: BLE001
        return

    # It accepted a capability that cannot exist. Nothing to learn, and now
    # there is a stray object — say so rather than leaving it silently.
    print("    ⚠️  the API accepted a nonsense capability type; a stray "
          f"'{cfg.SERVICE_DEPLOYMENT_NAME}' service may need removing.",
          flush=True)


def ensure_universal_service(client, profile_id=None):
    """
    The Universal Service, carrying a DNS capability if the tenant allows one.

    `capabilities[].profile_id` points at a dns/server config profile, and that
    is what actually makes the service serve DNS — the association between a
    service and the views and zones it answers for is expressed there rather
    than on the zone.

    If every capability spelling is refused, the service is created WITHOUT
    one. That is deliberate: a service with no DNS capability cannot serve the
    zone, so Part 3 will not fully work, but it gives the participant a real
    object to inspect and it makes the tenant's actual limitation visible in
    the log instead of leaving a bare 400 behind.
    """
    existing = client.find_by_name(cfg.path("universal_service"),
                                   cfg.SERVICE_DEPLOYMENT_NAME)
    if existing:
        info(f"universal service {cfg.SERVICE_DEPLOYMENT_NAME} already present")
        return existing

    body = {
        "name": cfg.SERVICE_DEPLOYMENT_NAME,
        "description": "DNS for the TechCorp AI VPC (Instruqt lab)",
        "tags": LAB_TAGS,
    }

    refusals = []
    for cap_type in CAPABILITY_TYPES:
        capability = {"type": cap_type, "anycast_enabled": False}
        if profile_id:
            capability["profile_id"] = profile_id

        try:
            created = client.post(cfg.path("universal_service"),
                                  json_body=dict(body, capabilities=[capability]))
        except CspError as exc:
            if not _capability_refused(exc):
                raise
            refusals.append(f"{cap_type}: {exc.body[:120]}")
            continue

        ok(f"created universal service {cfg.SERVICE_DEPLOYMENT_NAME} "
           f"with a {cap_type} capability")
        return created.get("result", created)

    # Every spelling refused. Create the service bare so there is something to
    # look at, and say clearly what is missing.
    print(f"⚠️  this tenant refused every DNS capability spelling:", flush=True)
    for line in refusals:
        print(f"      {line}", flush=True)
    print("    That is an entitlement on the account, not a payload this "
          "script can fix.", flush=True)
    _report_allowed_capabilities(client, body)

    created = client.post(cfg.path("universal_service"), json_body=body)
    print(f"⚠️  created {cfg.SERVICE_DEPLOYMENT_NAME} WITHOUT a DNS "
          f"capability. It cannot serve {cfg.ZONE_FQDN}, so Part 3 Step 4 "
          f"will not resolve.", flush=True)
    return created.get("result", created)


def ensure_endpoint(client, service_id, size=None):
    """
    The endpoint: where the service actually runs, and the address it answers on.

    `service_ip` is pinned in lab_config rather than chosen at runtime. Part 3's
    check has to know where to send its query, and an address the assistant
    invents mid-conversation is not something a check can predict.
    """
    existing = client.find_by_name(cfg.path("endpoints"), cfg.ENDPOINT_NAME)
    if existing:
        info(f"endpoint {cfg.ENDPOINT_NAME} already present")
        return existing

    created = client.post(cfg.path("endpoints"), json_body={
        "name": cfg.ENDPOINT_NAME,
        "universal_service_id": service_id,
        "service_location": cfg.VPC_REGION,
        "service_ip": cfg.SERVICE_IP,
        "size": size or cfg.ENDPOINT_SIZE,
        # Required, and legitimately empty: BGP peers are configured on the
        # access location once the tunnels exist.
        "neighbour_ips": [],
        "description": "Serves svc.techcorp.internal to the TechCorp AI VPC",
        "tags": LAB_TAGS,
    })
    ok(f"created endpoint {cfg.ENDPOINT_NAME} at {cfg.SERVICE_IP} "
       f"({cfg.VPC_REGION})")
    return created.get("result", created)


def ensure_access_location(client, endpoint_id, wan_ip_addresses,
                           credential_id=None):
    """
    The tunnel-facing half. NOT called during seeding.

    `wan_ip_addresses` are the customer-side public addresses that initiate the
    IPsec tunnels — on AWS, the outside addresses of a Site-to-Site VPN
    connection, which does not exist until Part 3 creates it. That circular
    dependency is inherent: Infoblox needs AWS's addresses, AWS needs
    Infoblox's, and one side has to go first.

    Kept here so the sequence is complete and callable once those addresses
    are known.
    """
    if not wan_ip_addresses:
        raise ServiceSeedingUnavailable(
            "an Access Location needs wan_ip_addresses — the AWS VPN "
            "connection's outside addresses, which do not exist until the "
            "VPN is created."
        )

    body = {
        "name": cfg.ACCESS_LOCATION_NAME,
        "endpoint_id": endpoint_id,
        "location_id": pick_location(client),
        "wan_ip_addresses": list(wan_ip_addresses),
        "type": "Cloud VPN",
        "cloud_type": "AWS",
        "cloud_region": cfg.VPC_REGION,
        "tags": LAB_TAGS,
    }
    if credential_id:
        body["credential_id"] = credential_id

    created = client.post(cfg.path("access_locations"), json_body=body)
    ok(f"created access location {cfg.ACCESS_LOCATION_NAME}")
    return created.get("result", created)


def associations(client, service_id):
    """
    What the Universal Service is currently serving.

    Read-only by design — there is no POST on this path. Useful as the
    verification step: it reports each view, auth zone and forward zone with
    whether it is actually assigned.
    """
    path = cfg.PATHS["us_associations"].format(
        service_id=str(service_id).rsplit("/", 1)[-1]
    )
    try:
        return client.list_results(path)
    except CspError as exc:
        info(f"could not read service associations: {exc}")
        return []


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build(client, profile_id=None):
    """
    Everything on the Infoblox side that can exist before Part 3 runs.

    Returns the ids, or raises ServiceSeedingUnavailable if the tenant is
    missing a prerequisite. seed_lab.py catches that and carries on: a lab
    without Part 3's service is still a lab with Parts 1, 2 and 4.
    """
    print("\n=== Baseline: NIOS-X as a Service (Part 3) ===", flush=True)

    size = pick_size(client)
    service = ensure_universal_service(client, profile_id=profile_id)
    service_id = service["id"]

    endpoint = ensure_endpoint(client, service_id, size=size)

    linked = associations(client, service_id)
    if linked:
        kinds = ", ".join(sorted({row.get("type", "?") for row in linked}))
        ok(f"service reports {len(linked)} association(s): {kinds}")
    else:
        info("service reports no associations yet — expected until a zone with "
             "primary_type 'cloud' is attached to its DNS capability")

    info("access location NOT created: it needs the AWS VPN's outside "
         "addresses, which Part 3 produces")

    # Whether the service can actually serve DNS, as opposed to merely
    # existing. Part 3's check reads this to tell "the track failed to build
    # it" apart from "the tenant is not entitled to it" — two different
    # messages for the participant, and only one of them is worth their time.
    capabilities = service.get("capabilities") or []
    has_dns = any("dns" in str(cap.get("type", "")).lower()
                  for cap in capabilities)

    return {
        "universal_service_id": service_id,
        "universal_service_name": cfg.SERVICE_DEPLOYMENT_NAME,
        "endpoint_id": endpoint["id"],
        "endpoint_name": cfg.ENDPOINT_NAME,
        "service_ip": cfg.SERVICE_IP,
        "endpoint_size": size,
        "has_dns_capability": has_dns,
    }

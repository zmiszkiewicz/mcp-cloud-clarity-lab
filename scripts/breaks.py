#!/usr/bin/env python3
"""
The deterministic misconfigurations. One function per break, plus one assertion
and one fix per break, all registered together.

Contract every break honours:

  * Independently callable.   seed_lab.py --break zone_missing_auth_server
  * Idempotent.               Applying twice leaves the same state.
  * Self-asserting.           apply() is followed by assert_applied(), which
                              re-reads the tenant and returns (bool, reason).

A break that applies but does not assert is worse than no break at all — the
participant spends fifteen minutes hunting a fault that is not there. seed_lab.py
exits non-zero on any failed assertion, which becomes a red "setup failed"
screen instead of a confusing lab.

  Part 2  zone_missing_auth_server   INC-4471. The DNS server is not on the
                                     zone's Authoritative DNS Servers list.
  Part 4  dhcp_range_overlap         Unguided. The Branch-02 DHCP range was
                                     narrowed onto the reserved fixed-address
                                     block during an IPAM cleanup.

Two breaks, not four. Part 2 is the guided incident and Part 4 is the unguided
skills test; anything extra just adds noise to a 45-minute track.

`fix` lives next to `apply` and `assert` so the remediation cannot drift when
the seed changes. solve-shell and `seed_lab.py --fix` both use it.
"""

import lab_config as cfg
from csp_client import info, ok


# --------------------------------------------------------------------------- #
# Break 1 (Part 2) — the DNS server is not authoritative for the zone
# --------------------------------------------------------------------------- #

def break_zone_missing_auth_server(client, ids):
    """
    Empty the zone's Authoritative DNS Servers list.

    This is INC-4471: "queries to svc.techcorp.internal return NXDOMAIN from the
    DNS server on 10.30.1.0/24 since last night's cutover."

    The zone exists. Its records exist. Nothing about the zone object looks
    wrong at a glance — which is precisely why it is a good teaching fault. The
    host was simply never added back to the zone after the cutover, so it has no
    idea it is supposed to answer for that name and returns NXDOMAIN like any
    server asked about a zone it does not host.

    In the API this is `internal_secondaries` (a host) or `nsgs` (a DNS server
    group) on the auth zone; in the Portal both are the "Authoritative DNS
    Servers" list on the zone's edit page. Same field either way, so a
    participant who fixes this in the Portal instead of through the assistant
    still passes the check.
    """
    import baseline

    baseline.set_authoritative_servers(client, ids["zone_id"], _authority(ids),
                                       attached=False)
    ok(f"break applied: {cfg.ZONE_FQDN} has no authoritative DNS servers")


def _authority(ids):
    """
    The authority descriptor seed_lab recorded.

    Falls back to reconstructing it from the flattened fields so a seed_ids.json
    written by an older build still loads rather than KeyError-ing halfway
    through a track.
    """
    if ids.get("authority"):
        return ids["authority"]
    return {
        "mode": ids.get("auth_mode", "host"),
        "id": ids.get("dns_server_id") or ids.get("dc_host_id"),
        "name": ids.get("dns_server_name") or ids.get("dc_host_name")
                or cfg.DC_HOST_NAME,
    }


def assert_zone_missing_auth_server(client, ids):
    import baseline

    servers = baseline.authoritative_server_ids(client, ids["zone_id"])
    if servers:
        return False, (
            f"expected {cfg.ZONE_FQDN} to have no authoritative servers, "
            f"found {len(servers)}: {servers}"
        )
    return True, (
        f"{cfg.ZONE_FQDN} has an empty Authoritative DNS Servers list — "
        f"{_authority(ids)['name']} is not serving it"
    )


def fix_zone_missing_auth_server(client, ids):
    """The remediation, for solve-shell and for reset between runs."""
    import baseline

    authority = _authority(ids)
    baseline.set_authoritative_servers(client, ids["zone_id"], authority,
                                       attached=True)
    ok(f"added {authority['name']} back to the authoritative servers for "
       f"{cfg.ZONE_FQDN}")


# --------------------------------------------------------------------------- #
# Break 2 (Part 4, unguided) — DHCP range collides with the reserved block
# --------------------------------------------------------------------------- #

def break_dhcp_range_overlap(client, ids):
    """
    Narrow the Branch-02 DHCP range from .100-.200 down to .10-.30, which is
    precisely the reserved fixed-address block.

    Result: the range exists, DHCP is "configured", utilization reads at or near
    100%, and no client can get a lease.

    Deliberately NOT explained anywhere in the assignment prose. Part 4 tells the
    participant only that something else is wrong and asks them to find it with
    no scaffolding, which is the best assessment signal in the track.
    """
    broken = cfg.BRANCH_RANGE_BROKEN
    client.patch(cfg.path("dhcp_range") + f"/{ids['range_id']}", json_body={
        "start": broken["start"],
        "end": broken["end"],
    })
    ok(f"break applied: Branch-02 range narrowed to "
       f"{broken['start']}-{broken['end']} (collides with the reserved block)")


def assert_dhcp_range_overlap(client, ids):
    dhcp_range = client.get(cfg.path("dhcp_range") + f"/{ids['range_id']}")
    dhcp_range = dhcp_range.get("result", dhcp_range)
    start, end = dhcp_range.get("start"), dhcp_range.get("end")

    if (start, end) != (cfg.BRANCH_RANGE_BROKEN["start"],
                        cfg.BRANCH_RANGE_BROKEN["end"]):
        return False, (
            f"expected Branch-02 range {cfg.BRANCH_RANGE_BROKEN['start']}-"
            f"{cfg.BRANCH_RANGE_BROKEN['end']}, found {start}-{end}"
        )

    if not ranges_overlap(start, end,
                          cfg.BRANCH_RESERVED_BLOCK["start"],
                          cfg.BRANCH_RESERVED_BLOCK["end"]):
        return False, (
            "the range was narrowed but does not overlap the reserved block — "
            "the DHCP symptom will not reproduce"
        )

    return True, "Branch-02 DHCP range overlaps the reserved fixed-address block"


def fix_dhcp_range_overlap(client, ids):
    healthy = cfg.BRANCH_RANGE_HEALTHY
    client.patch(cfg.path("dhcp_range") + f"/{ids['range_id']}", json_body={
        "start": healthy["start"],
        "end": healthy["end"],
    })
    ok(f"restored Branch-02 range to {healthy['start']}-{healthy['end']}")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

BREAKS = {
    "zone_missing_auth_server": {
        "part": 2,
        "summary": "the DNS server is not on the zone's Authoritative DNS "
                   "Servers list (INC-4471)",
        "apply": break_zone_missing_auth_server,
        "assert": assert_zone_missing_auth_server,
        "fix": fix_zone_missing_auth_server,
    },
    "dhcp_range_overlap": {
        "part": 4,
        "summary": "Branch-02 DHCP range collides with the reserved block "
                   "(unguided)",
        "apply": break_dhcp_range_overlap,
        "assert": assert_dhcp_range_overlap,
        "fix": fix_dhcp_range_overlap,
    },
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def ip_to_int(addr):
    octets = [int(part) for part in addr.split(".")]
    return (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]


def ranges_overlap(a_start, a_end, b_start, b_end):
    """Inclusive overlap test on two IPv4 ranges."""
    return (ip_to_int(a_start) <= ip_to_int(b_end)
            and ip_to_int(b_start) <= ip_to_int(a_end))


def apply_break(client, name, ids):
    """Apply one break and assert it landed. Returns (bool, reason)."""
    spec = BREAKS[name]
    info(f"applying break {name}: {spec['summary']}")
    spec["apply"](client, ids)
    return spec["assert"](client, ids)

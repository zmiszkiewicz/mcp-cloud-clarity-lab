#!/usr/bin/env python3
"""
Validate each part's END STATE against the Infoblox API and the AWS API.

    verify_lab.py --stage c1     Part 1  the connection is real
    verify_lab.py --stage c2     Part 2  INC-4471 is actually fixed
    verify_lab.py --stage c3     Part 3  DNS resolves from inside the VPC
    verify_lab.py --stage c4     Part 4  the unguided break was found and fixed
    verify_lab.py --stage all    everything, for a maintainer smoke test

NEVER PARSE THE CHAT TRANSCRIPT. The agent's wording is nondeterministic — the
same correct fix can be described five different ways, and a check that greps
for phrasing will fail a participant who did everything right. Every assertion
below reads the world and asks whether it is in the state the part requires,
regardless of how the participant got it there. Someone who fixes the zone in
the Portal instead of through the agent passes, and that is correct: the part is
about the outcome.

On failure, csp_client.fail() writes a one-sentence reason to
/tmp/cloud_clarity_check_reason.txt, which check-shell hands straight to
Instruqt's fail-message. Failure text is written as advice, and where the fault
is the environment's rather than the participant's it says so in as many words.
"""

import argparse
import json
import os
import sys

import baseline
import lab_config as cfg
from csp_client import (CspClient, CspError, LabTodo, clear_reason, fail, info,
                        object_url, ok, read_state)


def load_ids():
    """Object ids recorded by seed_lab.py."""
    for directory in (os.getcwd(), cfg.SCRIPT_DIR):
        candidate = os.path.join(directory, "seed_ids.json")
        if os.path.exists(candidate):
            with open(candidate) as handle:
                return json.load(handle)
    fail("The lab's seeded state is missing — the environment did not finish "
         "provisioning. Restart the track.")


# --------------------------------------------------------------------------- #
# Part 1 — Connect to the Infoblox MCP Server
# --------------------------------------------------------------------------- #

def check_c1(client, ids):
    """
    Confirm the assistant is genuinely connected to THIS participant's tenant.

    A READINESS GATE, not a test of the participant. Part 1 is read-only —
    they connect, look around, and ask what the server exposes. Nothing in the
    tenant changes, so there is no participant-authored end state to assert.

    What IS worth asserting is that the connection behind all of it is real.
    The read-only Service API key is checked with the same
    `Authorization: Token` header the Infoblox MCP Server uses, so a key that
    works here is the same key working there. If it does not, the participant
    had a hollow conversation and every later part inherits the problem — far
    better to say so now than to let them discover it mid-incident in Part 2.

    The failure messages are written accordingly: they say plainly that this is
    an environment fault, not the participant's mistake.
    """
    key = read_state("mcp_key")
    key_client = CspClient.from_service_key(key)

    try:
        services = key_client.list_results(cfg.path("infra_services"))
    except CspError as exc:
        fail("Your read-only service key cannot reach Infoblox. Access to the "
             "MCP Server is gated by role-based access control — if no MCP "
             "Server role is assigned, the connection is refused outright "
             f"rather than degraded. (HTTP {exc.status}) Tell your facilitator.")

    ok(f"read-only key authenticates against your tenant "
       f"({len(services)} infrastructure service(s) visible)")

    # The DDI data behind the MCP server has to be readable too. An
    # ib-mcp-server-* role on its own gates the connection but grants no access
    # to DNS, DHCP or IPAM — a key with only that connects and then returns
    # nothing, which reads as a broken lab rather than a permissions problem.
    views = key_client.list_results(cfg.path("dns_view"))
    if not views:
        fail("Your service key connects but can see no DNS views at all. That "
             "usually means the service user has an MCP Server role but no "
             "matching DDI role. It is an environment fault, not your mistake "
             "— tell your facilitator.")
    ok(f"the key can read DDI data ({len(views)} DNS view(s) visible)")


# --------------------------------------------------------------------------- #
# Part 2 — Troubleshoot and remediate the DNS issue (INC-4471)
# --------------------------------------------------------------------------- #

def check_c2(client, ids):
    """
    Two assertions, exactly as the track flow lists them:

      1. [DNS SERVER] appears in the Authoritative DNS Servers list for
         [ZONE NAME].
      2. The affected zone resolves correctly from the client host.

    (1) is read from the same `internal_secondaries` field the Portal writes, so
    the participant can have fixed it either way. (2) is deliberately separate:
    a configuration that says a server should answer is not the same thing as a
    server that does, and the whole lesson of the incident is that gap.
    """
    import breaks

    authority = breaks._authority(ids)
    servers = baseline.authoritative_server_ids(client, ids["zone_id"])

    if not servers:
        fail(f"{cfg.ZONE_FQDN.rstrip('.')} still has no authoritative DNS "
             f"servers assigned, so {authority['name']} has no idea it is "
             f"supposed to answer for that zone. Ask the assistant to add it.")

    if authority["id"] not in servers:
        fail(f"{cfg.ZONE_FQDN.rstrip('.')} now has {len(servers)} "
             f"authoritative server(s), but {authority['name']} — the one the "
             f"ticket points at — is not among them. That is what is serving "
             f"{cfg.SUBNETS['dc-01']['address']}/"
             f"{cfg.SUBNETS['dc-01']['cidr']}.")

    ok(f"{authority['name']} is authoritative for {cfg.ZONE_FQDN.rstrip('.')}")

    # -- The zone actually answers ------------------------------------------
    check_zone_resolves()


def check_zone_resolves():
    """
    Resolve the affected name the way the participant does from the Terminal tab.

    Skipped, loudly, when there is no resolver to ask. A skipped probe is
    honest; a probe against the check container's own resolver would pass or
    fail for reasons that have nothing to do with the participant's work.

    Set LAB_DC_RESOLVER to the address of the Universal DDI host serving
    [NETWORK] to turn this on. See README.md TODO-22.
    """
    import shutil
    import subprocess

    resolver = os.environ.get("LAB_DC_RESOLVER")
    if not resolver:
        info("LAB_DC_RESOLVER not set (TODO-22) — skipping the independent "
             "resolution probe; the configuration assertion above still holds")
        return

    if not shutil.which("dig"):
        info("dig not installed in the check container — skipping the "
             "independent resolution probe")
        return

    fqdn = cfg.APP_FQDN.rstrip(".")
    result = subprocess.run(
        ["dig", "+short", "+timeout=3", "+tries=2", f"@{resolver}", fqdn, "A"],
        capture_output=True, text=True,
    )
    answers = [line for line in result.stdout.split() if line]

    if not answers:
        fail(f"{fqdn} still does not resolve from {resolver}. The zone "
             f"configuration is correct but the server is not yet answering — "
             f"give it a moment, then ask the assistant to verify the fix.")

    ok(f"{fqdn} resolves to {', '.join(answers)} from {resolver}")


# --------------------------------------------------------------------------- #
# Part 3 — Deploy DNS services in a cloud VPC
# --------------------------------------------------------------------------- #

def check_c3(client, ids):
    """
    The track flow asks for two things:

      1. The new DNS service exists in Universal DDI and reports healthy.
      2. A query from the test VM against the service resolves the internal zone.

    (2) is the load-bearing one and it runs unconditionally. It is also the only
    one of the two that cannot be satisfied by a half-built deployment: a
    Service Deployment that exists but has no working path into the VPC will
    pass an object-existence check and fail a dig, and the dig is what the cloud
    team actually cares about.

    (1) is best-effort. The NIOS-X as a Service REST surface is not in the
    public API client (TODO-31), so where the Infoblox-side object cannot be
    read the check says so and leans on (2).
    """
    import cloud_vpc
    from cloud_vpc import CloudUnavailable

    # -- 1. Infoblox side ----------------------------------------------------
    try:
        deployments = client.list_results(cfg.path("service_deployment"))
        named = [d for d in deployments
                 if d.get("name") == cfg.SERVICE_DEPLOYMENT_NAME]
        if not named:
            fail(f"No Service Deployment named "
                 f"'{cfg.SERVICE_DEPLOYMENT_NAME}' exists in Universal DDI. "
                 f"Ask the assistant to create the DNS service for "
                 f"{cfg.VPC_NAME} before checking.")
        ok(f"Service Deployment {cfg.SERVICE_DEPLOYMENT_NAME} exists")
    except LabTodo as todo:
        info(f"Infoblox-side service check unavailable — {todo}")
    except CspError as exc:
        info(f"Infoblox-side service lookup failed: {exc}")

    # A DNS service on a Universal DDI host is readable today, so check that
    # too. In `forwarder` mode this is the object doing the work.
    try:
        services = client.list_results(cfg.path("infra_services"))
        dns_services = [s for s in services
                        if (s.get("service_type") or "").lower() == "dns"]
        if dns_services:
            unhealthy = [s.get("name") for s in dns_services
                         if s.get("desired_state") == "start"
                         and (s.get("status") or "").lower() not in
                         ("", "started", "healthy", "running")]
            if unhealthy:
                info(f"DNS services not reporting healthy: {unhealthy}")
            ok(f"{len(dns_services)} DNS service(s) registered in Universal DDI")
    except CspError as exc:
        info(f"could not list infrastructure services: {exc}")

    # -- 2. Resolution from inside the VPC ----------------------------------
    #
    # Everything from here on talks to AWS. boto3 missing, or credentials that
    # do not resolve, raises CloudUnavailable from deep inside these helpers —
    # so the whole block is wrapped rather than each call, and the participant
    # is told plainly that this one is not their fault.
    try:
        _check_c3_cloud(cloud_vpc)
    except CloudUnavailable as exc:
        fail(f"{exc} That is an environment fault, not your mistake — tell "
             f"your facilitator.")


def _check_c3_cloud(cloud_vpc):
    """The AWS half of Part 3's check. Split out so one wrapper catches it all."""
    cloud_vpc.describe()

    if cfg.C3_MODE == "as-a-service":
        up, total, detail = cloud_vpc.vpn_tunnel_state()
        if not total:
            # Nothing to bring up yet. Different advice from "it exists and is
            # down", which is why vpn_tunnel_state distinguishes the two.
            fail(detail)
        if not up:
            fail(f"A Site-to-Site VPN to the Infoblox point of presence exists, "
                 f"but no tunnel is up yet ({detail}). Until at least one comes "
                 f"up, DNS queries from {cfg.VPC_NAME} cannot reach the "
                 f"service. Tunnels take a couple of minutes to establish — "
                 f"check again shortly.")
        ok(f"{up}/{total} IPsec tunnel(s) up ({detail})")
    else:
        targets, detail = cloud_vpc.resolver_rule_targets()
        if not targets:
            fail(detail)
        ok(detail)

    # The same probe the participant runs with `lab-dig`, so there is never an
    # "it works for the check but not for me".
    #
    # An explicit resolver is used when one is known. When it is not, the query
    # goes to the VM's own resolver, which is the stricter and more realistic
    # test: it asks whether the VPC itself has been pointed at the new service,
    # not merely whether the service answers if you aim at it.
    fqdn = cfg.APP_FQDN.rstrip(".")
    resolver = cloud_vpc.dns_service_ip()
    answers, detail = cloud_vpc.resolve_from_test_vm(fqdn, resolver)

    if not answers:
        hint = (" The service may be up but the VPC not yet pointed at it — "
                "ask the assistant what the VPC's workloads currently use for "
                "DNS, and what it would take to send this zone to the new "
                "service.") if not resolver else ""
        fail(f"{fqdn} does not resolve from the test VM inside {cfg.VPC_NAME}. "
             f"{detail}{hint}")

    expected = cfg.BASELINE_A_RECORDS[cfg.APP_LABEL]["address"]
    if expected not in answers:
        fail(f"{fqdn} resolved from the test VM, but to {', '.join(answers)} "
             f"rather than {expected}. The query is reaching a resolver, but "
             f"not the Universal DDI zone — check which server the VPC is "
             f"forwarding to.")

    ok(f"{fqdn} resolves to {expected} from inside {cfg.VPC_NAME} "
       f"— end-to-end DNS is working for cloud workloads")


# --------------------------------------------------------------------------- #
# Part 4 — Make it yours (optional)
# --------------------------------------------------------------------------- #

def check_c4(client, ids):
    """
    The access-control exercise and the tool-discovery exercise are both
    read-only and leave nothing behind to verify — they are for the
    participant's understanding. What IS verifiable is that the unguided break
    was found and fixed with no prompt scaffolding, which is the real skills
    test in this part.
    """
    import breaks

    # -- If a read-only key exists, confirm it really is read-only -----------
    #
    # Only present when MCP_ROLES includes read_only. With the default single
    # read/write key there is nothing to probe: Part 4's access-control lesson
    # now rests on the MCP Server not exposing user administration at all,
    # which needs no assertion because the tool simply is not there.
    ro_key = read_state("mcp_ro_key", required=False)
    if ro_key:
        ro_client = CspClient.from_service_key(ro_key)
        try:
            ro_client.post(cfg.path("dns_view"), json_body={
                "name": f"rbac-probe-{cfg.PARTICIPANT_ID}",
            }, expect={403, 401})
            ok("read-only key is correctly refused write access")
        except CspError as exc:
            if exc.status in (200, 201):
                fail("The read-only key was able to write. This is a lab "
                     "defect, not your mistake — tell your facilitator.")
            info(f"read-only write probe returned {exc.status} "
                 f"(treated as denied)")

    # -- The unguided break has been fixed -----------------------------------
    dhcp_range = client.get(object_url(cfg.path("dhcp_range"), ids["range_id"]))
    dhcp_range = dhcp_range.get("result", dhcp_range)
    start, end = dhcp_range.get("start"), dhcp_range.get("end")

    if breaks.ranges_overlap(start, end,
                             cfg.BRANCH_RESERVED_BLOCK["start"],
                             cfg.BRANCH_RESERVED_BLOCK["end"]):
        fail(f"There is still a misconfiguration in this tenant that nobody "
             f"described to you. Something in "
             f"{cfg.SUBNETS['branch-02']['address']}/"
             f"{cfg.SUBNETS['branch-02']['cidr']} means clients there cannot "
             f"be given an address. Use the assistant to find it — start by "
             f"asking what looks wrong with DHCP on that network.")

    ok(f"Branch-02 DHCP range {start}-{end} is clear of the reserved "
       f"fixed-address block")

    # Utilization is the symptom the participant was chasing; it should have
    # come back down once the range moved.
    util = dhcp_range.get("utilization")
    pct = None
    if isinstance(util, dict):
        pct = util.get("utilization", util.get("dhcp_utilization"))
    elif util is not None:
        pct = util

    if pct is not None and float(pct) >= cfg.UTILIZATION_THRESHOLD_PCT:
        fail(f"Branch-02 utilization is still {pct}%. The range no longer "
             f"overlaps the reserved block, but there are still effectively no "
             f"assignable addresses in it — widen it further.")
    if pct is not None:
        ok(f"Branch-02 utilization is {pct}%")

    check_lease_issued(client, ids)


def check_lease_issued(client, ids):
    """
    Confirm the branch client host actually got an address.

    TODO-15 — the lease-listing endpoint is the unknown. This is the most
    valuable assertion in Part 4 if it can be made to work: a range that looks
    correct but issues no leases is exactly the failure mode being taught, so
    "the config looks right" is not really good enough. Skipped until the
    endpoint is confirmed.
    """
    try:
        leases = client.list_results(
            cfg.path("dhcp_lease"),
            params={"_filter": f'hardware=="{cfg.CLIENT_HOST_MAC}"'},
        )
    except LabTodo as todo:
        info(f"lease check unavailable — {todo}")
        return

    if not leases:
        fail(f"No DHCP lease has been issued to {cfg.CLIENT_HOST_NAME} yet. "
             f"The range looks right — request a fresh lease from the client "
             f"host, then click Check again.")

    addresses = [lease.get("address") for lease in leases]
    ok(f"{cfg.CLIENT_HOST_NAME} holds a lease: "
       f"{', '.join(a for a in addresses if a)}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

STAGES = {
    "c1": check_c1,
    "c2": check_c2,
    "c3": check_c3,
    "c4": check_c4,
}


def main():
    parser = argparse.ArgumentParser(
        description="Validate each part's end state via the Infoblox and AWS APIs."
    )
    parser.add_argument("--stage", required=True, choices=list(STAGES) + ["all"])
    args = parser.parse_args()

    clear_reason()

    try:
        client = CspClient.as_admin()
    except LabTodo as todo:
        fail(f"The lab is not fully configured yet: {todo}")
    except Exception as exc:                            # noqa: BLE001
        fail(f"Could not reach Infoblox to verify your work: {exc}")

    stages = list(STAGES) if args.stage == "all" else [args.stage]
    ids = load_ids()

    for stage in stages:
        print(f"\n── checking {stage} ──", flush=True)
        try:
            STAGES[stage](client, ids)
        except LabTodo as todo:
            fail(f"This check is not finished yet: {todo}")

    ok(f"{args.stage} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

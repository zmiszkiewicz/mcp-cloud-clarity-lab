#!/usr/bin/env python3
"""
Validate each part's END STATE against the Infoblox API and the AWS API.

    verify_lab.py --stage c1     Part 1  the connection is real
    verify_lab.py --stage c2     Part 2  INC-4471 is actually fixed
    verify_lab.py --stage c3     Part 3  DNS resolves from inside the VPC
    verify_lab.py --stage c4     Part 4  IPAM and AWS agree on the new /24
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
    Two conditions, weighted by who is responsible for them:

      1. The NIOS-X host is serving DNS.      TRACK SETUP built this, not the
                                              participant, and gated the start
                                              on it. A failure here is the
                                              environment's fault and the
                                              wording says so.
      2. The test VM resolves the zone.       Participant work, load-bearing.

    (2) is the one that matters. It is the only one a half-finished deployment
    cannot fake: a host that is healthy and a VPC that has not been pointed at
    it passes every object-existence check and fails a dig, and the dig is what
    the cloud team actually cares about.

    THIS USED TO CHECK A UNIVERSAL SERVICE AND AN ACCESS LOCATION. Both belong
    to NIOS-X as a Service, which a sandbox tenant cannot have — the PoP
    entitlement is missing, so every service location was refused. The lab
    builds a NIOS-X host in its own VPC instead. The old check outlived the
    architecture by several commits and failed Part 3 with "the Universal
    Service does not exist", which was true, permanent, and nothing to do with
    the participant.
    """
    import cloud_vpc
    from cloud_vpc import CloudUnavailable

    # -- 1. The host and service track setup built ---------------------------
    try:
        services = client.list_results(cfg.path("infra_services"))
        dns_services = [s for s in services
                        if (s.get("service_type") or "").lower() == "dns"]

        if not dns_services:
            # Not the participant's fault, and the wording has to say so.
            fail(f"No DNS service exists in this tenant. The NIOS-X host and "
                 f"its DNS service are built when the track starts, not by "
                 f"you, so this is an environment fault — tell your "
                 f"facilitator. The boot log's 'NIOS-X DNS host' section says "
                 f"what happened.")

        named = [s for s in dns_services
                 if s.get("name") == cfg.DNS_SERVICE_NAME]
        service = (named or dns_services)[0]

        state = (service.get("composite_state") or service.get("status")
                 or service.get("desired_state") or "unknown")
        ok(f"DNS service {service.get('name', '?')} exists "
           f"(state={state}, built at track start)")
    except LabTodo as todo:
        info(f"Infoblox-side service check unavailable — {todo}")
    except CspError as exc:
        info(f"Infoblox-side service lookup failed: {exc}")

    # -- 3. Resolution from inside the VPC ----------------------------------
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
    """
    The AWS half of Part 3's check. Split out so one wrapper catches it all.

    THE PARTICIPANT'S ACTUAL WORK IS THE DHCP OPTIONS SET. This used to check
    IPsec tunnels to an Infoblox point of presence, which no longer exist in
    any mode — the DNS host lives inside the VPC, so there is nothing to
    tunnel to. A check for a VPN that the lab never creates fails permanently
    and blames the participant for not building it.
    """
    cloud_vpc.describe()

    expected = cloud_vpc.dns_service_ip()
    servers, detail = cloud_vpc.vpc_dhcp_dns_servers()

    if not servers:
        fail(f"{cfg.VPC_NAME} is not handing out the Infoblox DNS host as its "
             f"resolver: {detail}. Workloads in the VPC will keep using "
             f"AmazonProvidedDNS, which knows nothing about "
             f"{cfg.ZONE_FQDN}. Attach a DHCP options set naming the DNS "
             f"host to the VPC.")

    if expected and expected not in servers:
        fail(f"The VPC's DHCP options hand out {', '.join(servers)}, which "
             f"does not include the DNS host at {expected}. Workloads will "
             f"ask the wrong resolver.")

    ok(f"{cfg.VPC_NAME} hands out {', '.join(servers)} as its resolver")

    # The same probe the participant runs with `lab-dig`, so there is never an
    # "it works for the check but not for me".
    #
    # An explicit resolver is used when one is known. When it is not, the query
    # goes to the VM's own resolver, which is the stricter and more realistic
    # test: it asks whether the VPC itself has been pointed at the new service,
    # not merely whether the service answers if you aim at it.
    fqdn = cfg.APP_FQDN.rstrip(".")
    resolver = cloud_vpc.dns_service_ip()

    # boot_wait, because the expected way to finish this challenge is to
    # reboot the test VM: a DHCP options set only reaches an instance when it
    # renews its lease, so the VM has to restart to use the new resolver. A
    # participant who does the right thing and clicks Check straight away
    # would otherwise be failed for SSH being down for thirty seconds —
    # punished for the step that made it work.
    answers, detail = cloud_vpc.resolve_from_test_vm(fqdn, resolver,
                                                     boot_wait=180)

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
    Part 4 is one exercise with two halves, and the point is that they have to
    agree:

      1. The next free /24 exists as a subnet in Infoblox IPAM.
      2. A VPC with that exact CIDR exists in AWS.

    Neither half is interesting alone. A subnet in IPAM that nobody built is a
    spreadsheet entry; a VPC whose range nobody recorded is how two teams
    allocate the same /24 and find out at the worst possible moment. What is
    being checked is that the address space the cloud is using is the address
    space IPAM says it is using.

    THE EXPECTED CIDR IS DECLARED, NOT DERIVED. cfg.PART4_SUBNET is written
    down rather than computed from the seeded subnets, because a check that
    works the answer out the same way the participant does would agree with
    them even when both are wrong.
    """
    import cloud_vpc
    from cloud_vpc import CloudUnavailable

    expected = f"{cfg.PART4_SUBNET['address']}/{cfg.PART4_SUBNET['cidr']}"

    # -- 1. The allocation, recorded in IPAM ---------------------------------
    try:
        subnets = client.list_results(cfg.path("ipam_subnet"))
    except CspError as exc:
        fail(f"Could not read IPAM subnets to verify the allocation: {exc}")

    def _cidr_of(subnet):
        address = subnet.get("address") or ""
        prefix = subnet.get("cidr")
        return f"{address}/{prefix}" if address and prefix else ""

    found = [s for s in subnets if _cidr_of(s) == expected]
    if not found:
        allocated = sorted(filter(None, (_cidr_of(s) for s in subnets)))
        fail(f"No subnet for {expected} exists in IPAM yet. Ask the assistant "
             f"for the next available /24 in {cfg.IP_SPACE_NAME}, then create "
             f"it in the Portal under Configure -> Networking -> IPAM/DHCP. "
             f"Currently allocated: {', '.join(allocated) or 'nothing'}.")

    ok(f"{expected} is allocated in IPAM as "
       f"{found[0].get('name') or '(unnamed)'}")

    # -- 2. The VPC built from it --------------------------------------------
    try:
        _check_c4_cloud(cloud_vpc, expected)
    except CloudUnavailable as exc:
        fail(f"{exc} That is an environment fault, not your mistake — tell "
             f"your facilitator.")


def _check_c4_cloud(cloud_vpc, expected):
    """The AWS half. Split out so one wrapper catches CloudUnavailable."""
    vpcs, detail = cloud_vpc.find_vpcs_by_cidr(expected)

    if not vpcs:
        fail(f"IPAM says {expected} is allocated, but no VPC in AWS uses it. "
             f"{detail} Ask the assistant to create one with that CIDR — it "
             f"has write access to AWS, so it can do this itself with your "
             f"approval.")

    names = [v["name"] or v["id"] for v in vpcs]
    ok(f"VPC {', '.join(names)} in AWS uses {expected}, matching IPAM")

    if len(vpcs) > 1:
        info(f"{len(vpcs)} VPCs share {expected} — harmless here, but in a "
             f"real account that is the collision IPAM exists to prevent")


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

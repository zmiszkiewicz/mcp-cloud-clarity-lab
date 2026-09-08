#!/usr/bin/env python3
"""
The AWS side of Part 3 — everything the check needs to know about the VPC.

Why this file exists at all: Part 3's success criterion is not "an object was
created in Universal DDI", it is "a workload inside the VPC can resolve an
internal name". Those are very different claims, and only the second one is
worth a challenge. So the load-bearing assertion runs `dig` ON THE TEST VM,
inside the VPC, through whatever DNS path the participant just built — and this
module is how the check reaches in to do that.

Reaching in is done with SSM Run Command rather than SSH. No key material to
distribute, no security-group hole to punch for port 22, and the test VM needs
an SSM agent and an instance profile anyway to be a realistic workload. If
`ssm` is missing from the `services:` list in config.yml, every probe here
degrades to "could not reach the test VM" rather than a false failure.

Nothing here creates infrastructure. Terraform does that at track setup; this
module only observes.
"""

import json
import os
import time

import lab_config as cfg
from csp_client import info


class CloudUnavailable(RuntimeError):
    """boto3 or AWS credentials are not usable from this container."""


def _boto3():
    try:
        import boto3
    except ImportError as exc:                          # noqa: BLE001
        raise CloudUnavailable(
            "boto3 is not installed in this container — the AWS-side probes "
            "cannot run. `pip3 install boto3`."
        ) from exc
    return boto3


def _client(service):
    boto3 = _boto3()
    return boto3.client(service, region_name=cfg.VPC_REGION)


# --------------------------------------------------------------------------- #
# Terraform outputs
# --------------------------------------------------------------------------- #

def outputs():
    """
    The Terraform outputs captured at track setup.

    Written as flat JSON by track_scripts/setup-shell:

        {"vpc_id": "...", "test_vm_instance_id": "...",
         "vpn_connection_id": "...", "resolver_endpoint_id": "...",
         "workload_subnet_id": "...", "vpc_name": "..."}

    Returns {} when the file is absent, which every caller treats as "the cloud
    side did not provision" rather than as a failure of the participant.
    """
    filename = cfg.STATE_FILES["vpc_outputs"]
    for directory in (os.getcwd(), cfg.SCRIPT_DIR, cfg.TERRAFORM_DIR):
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            try:
                with open(candidate) as handle:
                    return json.load(handle)
            except ValueError:
                return {}
    return {}


def test_vm_instance_id():
    """
    The test VM's instance id — from Terraform output, falling back to a tag
    lookup so a rebuilt VM is still found.
    """
    recorded = outputs().get("test_vm_instance_id")
    if recorded:
        return recorded

    ec2 = _client("ec2")
    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [f"{cfg.TEST_VM_NAME}*"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]).get("Reservations", [])
    for reservation in reservations:
        for instance in reservation.get("Instances", []):
            return instance["InstanceId"]
    return None


# --------------------------------------------------------------------------- #
# The load-bearing probe: resolve from inside the VPC
# --------------------------------------------------------------------------- #

def resolve_from_test_vm(fqdn, resolver=None, timeout=90):
    """
    Run `dig` on the test VM and return the answers it got.

    Returns (answers, detail):

        (["10.30.1.40"], "…")   resolved
        ([], "<why not>")       did not resolve, with a reason worth showing

    A resolver of None means "use whatever the VM's own resolv.conf says", which
    is the more interesting question in `as-a-service` mode: if the participant
    wired DNS into the VPC properly, the VM's default resolver already reaches
    Universal DDI and no explicit @server is needed.
    """
    instance_id = test_vm_instance_id()
    if not instance_id:
        return [], ("The test VM in the VPC could not be found. That is an "
                    "environment fault, not your mistake — tell your "
                    "facilitator.")

    server = f"@{resolver} " if resolver else ""
    command = (
        f"dig +short +timeout=3 +tries=2 {server}{fqdn} A "
        f"|| echo __DIG_FAILED__"
    )

    ssm = _client("ssm")
    try:
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=60,
        )
    except Exception as exc:                            # noqa: BLE001
        return [], (f"Could not run a command on the test VM through SSM "
                    f"({exc}). Check that `ssm` is in the AWS services list.")

    command_id = sent["Command"]["CommandId"]
    deadline = time.time() + timeout
    invocation = None

    while time.time() < deadline:
        time.sleep(3)
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
            break

    if not invocation:
        return [], "The dig probe on the test VM did not return in time."

    stdout = (invocation.get("StandardOutputContent") or "").strip()
    if "__DIG_FAILED__" in stdout or not stdout:
        return [], (f"`dig {server}{fqdn}` on the test VM returned nothing. "
                    f"The query is not reaching a server that is authoritative "
                    f"for {fqdn}.")

    answers = [
        line.strip() for line in stdout.splitlines()
        if line.strip() and not line.startswith(";")
    ]
    return answers, stdout


# --------------------------------------------------------------------------- #
# Mode-specific readiness
# --------------------------------------------------------------------------- #

def vpn_tunnel_state():
    """
    `as-a-service` mode: are the Site-to-Site VPN tunnels to the Infoblox PoP up?

    Returns (up_count, total_count, detail). Two tunnels is the norm — NIOS-X as
    a Service always deploys a pair across availability zones — and one up is
    enough to resolve, so the check treats >=1 as success and mentions the
    second in passing.

    The connection is DISCOVERED rather than read from Terraform output, because
    Terraform does not create it: the participant does, in Part 3, once they
    have the Cloud Service IPs from the Infoblox side. All Terraform leaves
    behind is the Virtual Private Gateway to attach it to, so that is what we
    search by.

    total_count of 0 means "no VPN connection exists yet", which the caller
    distinguishes from "it exists and no tunnel is up" — those need different
    advice.
    """
    vgw_id = outputs().get("vpn_gateway_id")
    if not vgw_id:
        return 0, 0, ("No Virtual Private Gateway was provisioned for this VPC, "
                      "so there is nothing to attach a tunnel to. That is an "
                      "environment fault, not your mistake.")

    ec2 = _client("ec2")
    try:
        connections = ec2.describe_vpn_connections(Filters=[
            {"Name": "vpn-gateway-id", "Values": [vgw_id]},
            {"Name": "state", "Values": ["pending", "available"]},
        ])["VpnConnections"]
    except Exception as exc:                            # noqa: BLE001
        return 0, 0, f"Could not read VPN connections for {vgw_id}: {exc}"

    if not connections:
        return 0, 0, ("No Site-to-Site VPN connection is attached to this VPC "
                      "yet. The Infoblox side may be configured, but nothing "
                      "carries DNS traffic from the VPC to it.")

    telemetry = []
    for connection in connections:
        telemetry.extend(connection.get("VgwTelemetry", []))

    up = [t for t in telemetry if t.get("Status") == "UP"]
    detail = ", ".join(
        f"{t.get('OutsideIpAddress')}={t.get('Status')}" for t in telemetry
    ) or "no tunnel telemetry yet"
    return len(up), len(telemetry), detail


def resolver_rule_targets():
    """
    `forwarder` mode: which IPs is the VPC's Route 53 Resolver rule forwarding
    the zone to?

    Returns (targets, detail). Empty targets means the participant has not
    created or associated the rule yet.
    """
    vpc_id = outputs().get("vpc_id")
    zone = cfg.ZONE_FQDN.rstrip(".")

    r53 = _client("route53resolver")
    try:
        rules = r53.list_resolver_rules()["ResolverRules"]
    except Exception as exc:                            # noqa: BLE001
        return [], f"Could not list Route 53 Resolver rules: {exc}"

    matching = [
        rule for rule in rules
        if (rule.get("DomainName") or "").rstrip(".").lower() == zone.lower()
    ]
    if not matching:
        return [], (f"No Route 53 Resolver rule forwards {zone}. Ask the agent "
                    f"to create a forwarding rule for it and associate it with "
                    f"the VPC.")

    rule = matching[0]
    targets = [t.get("Ip") for t in rule.get("TargetIps", []) if t.get("Ip")]

    if vpc_id:
        try:
            associations = r53.list_resolver_rule_associations(Filters=[
                {"Name": "ResolverRuleId", "Values": [rule["Id"]]},
                {"Name": "VPCId", "Values": [vpc_id]},
            ])["ResolverRuleAssociations"]
        except Exception:                               # noqa: BLE001
            associations = []
        if not associations:
            return targets, (f"A forwarding rule for {zone} exists but is not "
                             f"associated with {vpc_id}, so workloads in the "
                             f"VPC never see it.")

    return targets, f"{zone} forwards to {', '.join(targets) or '(no targets)'}"


def dns_service_ip():
    """
    The [DNS SERVICE IP] the test VM should be querying.

    Resolution order, most trustworthy first:

      1. LAB_DNS_SERVICE_IP, if a facilitator pinned it.
      2. The Route 53 Resolver rule's target, in `forwarder` mode — that IS the
         address workloads reach.
      3. Whatever Terraform recorded.

    Returns None when nothing is known, in which case the dig probe runs against
    the VM's own resolver instead of an explicit @server. That is not a
    degradation: it is the stricter test.
    """
    if cfg.DNS_SERVICE_IP:
        return cfg.DNS_SERVICE_IP

    if cfg.C3_MODE == "forwarder":
        targets, _ = resolver_rule_targets()
        if targets:
            return targets[0]

    return outputs().get("dns_service_ip")


def describe():
    """One-line summary of the cloud side, for setup and check logs."""
    data = outputs()
    if not data:
        info("no terraform outputs recorded for the VPC")
        return
    info(f"VPC {data.get('vpc_name')} ({data.get('vpc_id')}) "
         f"in {cfg.VPC_REGION}, test VM {data.get('test_vm_instance_id')}")

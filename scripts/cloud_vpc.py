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
an SSM agent and an instance profile anyway to be a realistic workload.

SSM NEEDS THREE IAM SERVICE PREFIXES, and config.yml must list all of them:
`ssm` (the heartbeat, which is what sets PingStatus to Online), `ssmmessages`
(the control channel Run Command is delivered over) and `ec2messages`. With only
`ssm` allowed, every probe in this module reports a VM that is Online and will
not run anything — which reads as a network fault and is not one. That cost
three rounds of debugging aimed at the subnet; see the note above
`aws_internet_gateway` in terraform/main.tf.

The VM reaches SSM over the PUBLIC endpoints, via an internet gateway, and its
console carries a diagnostic block (`LAB_SSM_DIAG`) written by user_data.
`diagnose_test_vm()` reads it. That path deliberately does not depend on SSM,
because the situations worth diagnosing are the ones where SSM is what broke.

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

# A DNS client in pure Python, run on the test VM.
#
# NOT `dig`. The test VM sits in a private subnet with no internet gateway and
# no NAT — deliberately, because that is what a real workload subnet looks like
# — so `dnf install bind-utils` in user_data could never have worked, and
# Amazon Linux 2023 does not ship bind-utils. The probe was calling a binary
# that was not there.
#
# Rather than add a NAT gateway to install one package, ask the interpreter
# that IS there. This does a UDP query with the standard library only, and
# unlike `getent hosts` it can be pointed at a specific resolver — which Part 3
# needs, since the whole question is whether a particular DNS service answers.
_DNS_PROBE = r'''
import socket, struct, sys, random

name = sys.argv[1].rstrip(".")
server = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

if not server:
    # Whatever the VM itself would use. The stricter test: it asks whether the
    # VPC has been pointed at the new DNS service, not merely whether the
    # service answers when aimed at directly.
    try:
        for line in open("/etc/resolv.conf"):
            if line.startswith("nameserver"):
                server = line.split()[1]
                break
    except OSError:
        pass
if not server:
    print("NO_RESOLVER", file=sys.stderr); sys.exit(2)

qid = random.randint(0, 0xFFFF)
query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
for label in name.split("."):
    query += bytes([len(label)]) + label.encode()
query += b"\x00" + struct.pack(">HH", 1, 1)          # A, IN

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)
try:
    sock.sendto(query, (server, 53))
    data, _ = sock.recvfrom(4096)
except Exception as exc:
    print(f"QUERY_FAILED {server} {exc}", file=sys.stderr); sys.exit(3)

rcode = data[3] & 0x0F
if rcode == 3:
    print(f"NXDOMAIN via {server}", file=sys.stderr); sys.exit(4)
if rcode != 0:
    print(f"RCODE {rcode} via {server}", file=sys.stderr); sys.exit(5)

def read_name(buf, off):
    while True:
        length = buf[off]
        if length == 0:
            return off + 1
        if length & 0xC0 == 0xC0:
            return off + 2
        off += 1 + length

off = 12
off = read_name(data, off) + 4                        # skip the question
answers = []
for _ in range(struct.unpack(">H", data[6:8])[0]):
    off = read_name(data, off)
    rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
    off += 10
    if rtype == 1 and rdlen == 4:
        answers.append(".".join(str(b) for b in data[off:off + 4]))
    off += rdlen

if not answers:
    print(f"NO_A_RECORD via {server}", file=sys.stderr); sys.exit(6)
for a in answers:
    print(a)
'''


def ssm_instance_info(instance_id):
    """
    The SSM registration record for THIS instance, or None.

    Matches on InstanceId explicitly rather than trusting the filter and taking
    row [0]. The old version did the latter, which is only correct if the
    filter is applied as expected — and if it ever is not, it reports the ping
    status of some unrelated instance in the account as though it were ours.
    That would produce exactly the symptom we have been chasing: a confident
    "Online" for a VM whose agent has never checked in.
    """
    ssm = _client("ssm")
    rows = ssm.describe_instance_information(Filters=[
        {"Key": "InstanceIds", "Values": [instance_id]},
    ])["InstanceInformationList"]

    for row in rows:
        if row.get("InstanceId") == instance_id:
            return row

    # Nothing matched. Say what the filter DID return, because "the filter is
    # not doing what I think" is a real possibility worth ruling out.
    if rows:
        others = ", ".join(r.get("InstanceId", "?") for r in rows[:5])
        raise LookupError(
            f"the SSM filter returned {len(rows)} record(s) but none for "
            f"{instance_id} — got: {others}"
        )
    return None


def ssm_registered(instance_id, wait=0):
    """
    Is THIS VM managed by SSM, and currently Online? Optionally wait for it.

    Returns (bool, detail) where detail carries the agent version and last ping
    when known, because "Online" alone has repeatedly turned out to be true and
    unhelpful.
    """
    deadline = time.time() + max(wait, 0)
    while True:
        try:
            row = ssm_instance_info(instance_id)
            if row is None:
                status = "not registered with SSM at all"
            else:
                ping = row.get("PingStatus")
                last = row.get("LastPingDateTime")
                agent = row.get("AgentVersion", "?")
                status = (f"{ping} (agent {agent}, "
                          f"last ping {last:%H:%M:%S}" if last else
                          f"{ping} (agent {agent}")
                status += ")"
                if ping == "Online":
                    return True, status
        except Exception as exc:                        # noqa: BLE001
            status = f"lookup failed: {exc}"
        if time.time() >= deadline:
            return False, status
        time.sleep(5)


def diagnose_test_vm(instance_id=None):
    """
    Everything knowable about the VM from outside it, in one block.

    Exists because four separate theories about why Run Command sits in
    Pending have each been wrong, and each cost a track restart to disprove.
    Printing the facts once is cheaper than another round of hypotheses.
    """
    instance_id = instance_id or test_vm_instance_id()
    lines = [f"instance: {instance_id}"]

    try:
        ec2 = _client("ec2")
        reservations = ec2.describe_instances(
            InstanceIds=[instance_id])["Reservations"]
        inst = reservations[0]["Instances"][0]
        profile = (inst.get("IamInstanceProfile") or {}).get("Arn", "NONE")
        lines += [
            f"  state:          {inst['State']['Name']}",
            f"  ami:            {inst.get('ImageId')}",
            f"  public ip:      {inst.get('PublicIpAddress', 'NONE')}",
            f"  subnet:         {inst.get('SubnetId')}",
            f"  instance profile: {profile.rsplit('/', 1)[-1]}",
            f"  launched:       {inst.get('LaunchTime')}",
        ]
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"  EC2 lookup failed: {exc}")

    try:
        row = ssm_instance_info(instance_id)
        if row is None:
            lines.append("  SSM: NOT REGISTERED — the agent has never checked in")
        else:
            lines += [
                f"  SSM ping:       {row.get('PingStatus')}",
                f"  SSM last ping:  {row.get('LastPingDateTime')}",
                f"  SSM agent:      {row.get('AgentVersion')} "
                f"(latest={row.get('IsLatestVersion')})",
                f"  SSM platform:   {row.get('PlatformName')} "
                f"{row.get('PlatformVersion')}",
            ]
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"  SSM lookup failed: {exc}")

    # Every managed instance in the account, to show whether the filter above
    # is telling the truth.
    try:
        ssm = _client("ssm")
        allrows = ssm.describe_instance_information()["InstanceInformationList"]
        lines.append(f"  managed instances in this account: "
                     f"{[r.get('InstanceId') for r in allrows][:8]}")
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"  account-wide SSM listing failed: {exc}")

    # The agent's own account of itself, read WITHOUT using SSM.
    lines.append("")
    lines.append(console_diagnostics(instance_id))

    return "\n".join(lines)


def console_diagnostics(instance_id=None):
    """
    The `LAB_SSM_DIAG` blocks the VM writes to its serial console at boot.

    This is the only instrument here that does not depend on the thing it is
    used to diagnose. Every other probe in this module asks SSM whether SSM is
    working; when the answer is "no" they report a symptom and nothing about the
    cause, which is how four consecutive theories about the Pending problem each
    survived a whole track restart before being disproved.

    user_data (terraform/main.tf) writes the agent's unit state and the tail of
    /var/log/amazon/ssm/amazon-ssm-agent.log to /dev/console at roughly t+60s,
    t+180s and t+420s. An IAM denial on ssmmessages appears there in full, named.

    Needs only ec2:GetConsoleOutput. Empty for the first minute or so after boot
    while the buffer fills, which is not an error.
    """
    instance_id = instance_id or test_vm_instance_id()
    if not instance_id:
        return "console: no instance id"

    try:
        output = _client("ec2").get_console_output(
            InstanceId=instance_id, Latest=True
        ).get("Output", "")
    except Exception as exc:                            # noqa: BLE001
        return f"console: could not be read: {exc}"

    if not output:
        return ("console: empty — normal for the first minute after boot, "
                "before the serial buffer is flushed")

    # Keep only the newest diagnostic block. The console buffer also holds the
    # whole kernel boot, which is a lot of text and none of it relevant.
    blocks = output.split("=== LAB_SSM_DIAG start")
    if len(blocks) < 2:
        return ("console: readable, but no LAB_SSM_DIAG block yet — the first "
                "is written about 60s after boot. If the VM has been up for "
                "several minutes and there is still none, user_data did not "
                "run; check the tail of the console for a cloud-init error.")

    newest = blocks[-1].split("=== LAB_SSM_DIAG end")[0]
    body = "\n".join(f"    {line}" for line in newest.strip().splitlines())
    return f"  console (newest LAB_SSM_DIAG block):\n{body}"


def run_on_test_vm(command, timeout=180, instance_id=None):
    """
    Run one shell command on the test VM. Returns a result dict:

        {"ok": bool, "status": "Success", "rc": 0,
         "stdout": "...", "stderr": "...", "detail": "..."}

    Everything the probe does goes through here so that a failure reports what
    actually happened. An earlier version returned only "no output from the
    probe", which was true and useless: it did not say whether the command
    failed, timed out, or ran fine and printed nothing.
    """
    instance_id = instance_id or test_vm_instance_id()
    if not instance_id:
        return {"ok": False, "status": "no-instance", "rc": None,
                "stdout": "", "stderr": "",
                "detail": "no test VM instance id available"}

    ssm = _client("ssm")
    try:
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            # SSM marks a command DeliveryTimedOut once this elapses without
            # the agent collecting it. We poll for LONGER than this on purpose:
            # racing it produced an ambiguous "Pending" where SSM would have
            # told us plainly that the agent never picked the command up.
            TimeoutSeconds=120,
        )
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "status": "send-failed", "rc": None,
                "stdout": "", "stderr": "",
                "detail": f"SSM send_command failed: {exc}"}

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
        return {"ok": False, "status": "no-invocation", "rc": None,
                "stdout": "", "stderr": "",
                "detail": f"no SSM invocation appeared within {timeout}s"}

    status = invocation.get("Status")
    rc = invocation.get("ResponseCode")
    stdout = (invocation.get("StandardOutputContent") or "").strip()
    stderr = (invocation.get("StandardErrorContent") or "").strip()

    detail = f"status={status} rc={rc}"
    if stderr:
        detail += f" stderr={stderr[:300]}"
    if not stdout and not stderr:
        detail += " (both streams empty)"

    # Pending at the deadline means the agent never picked the command up, which
    # is a different fault from one that ran and failed. Say so, because the
    # remedy is different too.
    if status == "Pending":
        detail += (" — the SSM agent accepted the command but never ran it. "
                   "'Registered' and 'commandable' are different states: the "
                   "heartbeat is ssm:UpdateInstanceInformation, the delivery "
                   "path is ssmmessages:*, and they can be permitted "
                   "separately. THE FIRST THING TO CHECK is that `ssmmessages` "
                   "and `ec2messages` are both in the services list in "
                   "config.yml — with only `ssm` there, this is exactly the "
                   "symptom. Then check the public IP and the route to the "
                   "internet gateway. `diagnose_test_vm()` prints the agent's "
                   "own log from the console, which names the denied call")

    return {"ok": status == "Success" and rc == 0,
            "status": status, "rc": rc,
            "stdout": stdout, "stderr": stderr, "detail": detail}


def test_vm_can_run_commands(instance_id=None, attempts=2, timeout=150):
    """
    Can we execute anything at all on the VM, and is python3 there?

    Separate from the DNS question on purpose. These two are what Part 3
    genuinely requires — its check runs a Python DNS client over SSM — whereas
    whether a *public* name resolves at boot is a convenience, and not even the
    path Part 3 exercises. Conflating them meant a public-DNS quirk failed the
    whole track start.

    RETRIED, because the failure it guards against is a timing one. The agent's
    control channel takes a little while to establish after boot, and a single
    attempt turns "not ready yet" into "will never work". Terraform now boots
    the VM only after its route, its subnet association and its IAM policy all
    exist, which should make the first attempt succeed; these retries are what
    stops a merely slow agent failing a whole track start anyway.

    Note that no number of retries fixes a missing `ssmmessages` permission —
    that failure is permanent and looks identical from here. run_on_test_vm()
    says so in its Pending detail, and diagnose_test_vm() can prove it.
    """
    last = "no attempt made"
    for attempt in range(1, attempts + 1):
        result = run_on_test_vm(
            "echo LAB_EXEC_OK; python3 -c 'import sys; print(sys.version.split()[0])'",
            timeout=timeout, instance_id=instance_id,
        )

        if result["ok"] and "LAB_EXEC_OK" in result["stdout"]:
            version = [l for l in result["stdout"].splitlines()
                       if l != "LAB_EXEC_OK"]
            if not version:
                # A hard no: nothing about waiting longer installs python3.
                return False, ("python3 is not available on the test VM. The "
                               "DNS probe needs it. Amazon Linux 2023 ships "
                               "it, so this almost certainly means the AMI "
                               "filter in terraform/main.tf matched something "
                               "else — a minimal variant, most likely.")
            return True, f"commands run; python3 {version[0]}"

        last = result["detail"]
        if result["status"] not in ("Pending", "InProgress", "Delayed",
                                    "no-invocation"):
            # Ran and genuinely failed. Retrying will not change the answer.
            break
        if attempt < attempts:
            info(f"    attempt {attempt}: {last} — retrying")

    return False, f"could not run a command on the VM — {last}"


def resolve_from_test_vm(fqdn, resolver=None, timeout=120, ssm_wait=0):
    """
    Resolve a name FROM INSIDE the VPC and return the answers.

    Returns (answers, detail):

        (["10.30.1.40"], "…")   resolved
        ([], "<why not>")       did not, with a reason worth acting on

    A resolver of None means "whatever the VM's own resolv.conf says", which is
    the more interesting question: if DNS was wired into the VPC properly, the
    VM's default resolver already reaches Universal DDI.
    """
    import base64

    instance_id = test_vm_instance_id()
    if not instance_id:
        return [], ("The test VM in the VPC could not be found. That is an "
                    "environment fault, not your mistake — tell your "
                    "facilitator.")

    online, status = ssm_registered(instance_id, wait=ssm_wait)
    if not online:
        return [], (f"The test VM ({instance_id}) is not reachable through SSM "
                    f"— ping status: {status}. Nothing can run on it, so this "
                    f"is an environment fault rather than a DNS problem. The "
                    f"usual causes are the `ssmmessages` / `ec2messages` "
                    f"services missing from config.yml, the instance profile "
                    f"missing its policy, or no route to the internet gateway.")

    payload = base64.b64encode(_DNS_PROBE.encode()).decode()
    command = (f"echo {payload} | base64 -d > /tmp/dnsprobe.py && "
               f"python3 /tmp/dnsprobe.py {fqdn} {resolver or ''}")

    result = run_on_test_vm(command, timeout=timeout, instance_id=instance_id)

    answers = [line.strip() for line in result["stdout"].splitlines()
               if line.strip()]
    if answers:
        return answers, result["stdout"]

    where = f"via {resolver}" if resolver else "via the VM's own resolver"
    return [], (f"{fqdn} did not resolve from inside the VPC {where}. "
                f"{result['detail']}")


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

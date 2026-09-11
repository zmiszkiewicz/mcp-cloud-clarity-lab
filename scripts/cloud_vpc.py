#!/usr/bin/env python3
"""
The AWS side of Part 3 — everything the check needs to know about the VPC.

Why this file exists at all: Part 3's success criterion is not "an object was
created in Universal DDI", it is "a workload inside the VPC can resolve an
internal name". Those are very different claims, and only the second one is
worth a challenge. So the load-bearing assertion runs `dig` ON THE TEST VM,
inside the VPC, through whatever DNS path the participant just built — and this
module is how the check reaches in to do that.

Reaching in is done over SSH, with a keypair Terraform generates per track run
and writes to the path in the `test_vm_ssh_key_path` output. `run_on_test_vm()`
is the one function here that touches the wire; everything else is built on it.

WHY NOT SSM RUN COMMAND, WHICH IS THE BETTER TOOL FOR THIS. Systems Manager
needs three IAM service prefixes — `ssm` for the heartbeat, `ssmmessages` for
the channel Run Command is delivered over, `ec2messages` for agent startup — and
this Instruqt team's accounts can only be granted `ssm`. One of three produces a
VM that registers, reports PingStatus "Online", and then never executes
anything: a symptom that reads as a network fault and is not one. See "HOW THE
CHECKS REACH THE TEST VM" in terraform/main.tf. If the other two prefixes are
ever enabled, moving back is a change to this file alone — the instance profile
is still attached to the VM.

The VM's console carries a diagnostic block (`LAB_VM_DIAG`) written by user_data
and read by `diagnose_test_vm()` over `ec2:GetConsoleOutput`. That path shares
nothing with the exec path above it, deliberately: the situation worth
diagnosing is the one where the exec path is what broke.

Nothing here creates infrastructure. Terraform does that at track setup; this
module only observes.
"""

import json
import os
import socket
import subprocess
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
# NOT `dig`. Amazon Linux 2023 does not ship bind-utils, and nothing is
# installed on the VM at boot — a boot that depends on a package mirror is a
# boot that can fail for reasons this lab does not care about. An earlier
# version called `dig` anyway and was calling a binary that was not there.
#
# So ask the interpreter that IS there. This does a UDP query with the standard
# library only, and unlike `getent hosts` it can be pointed at a specific
# resolver — which Part 3 needs, since the whole question is whether a
# particular DNS service answers.
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


# --------------------------------------------------------------------------- #
# How we reach the VM: SSH
# --------------------------------------------------------------------------- #

SSH_USER = "ec2-user"           # Amazon Linux 2023's default login
SSH_KEY_FALLBACK = "/opt/lab/test_vm_key"

# StrictHostKeyChecking=no and a null known_hosts file, because the VM is built
# fresh every track run and its host key has therefore never been seen before.
# The alternative is a prompt that a check script would hang on forever.
#
# BatchMode=yes turns every would-be prompt into an immediate failure, which is
# what a non-interactive caller wants: a check that fails in 5s beats one that
# blocks until Instruqt kills the challenge.
SSH_BASE_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",             # suppresses the known-hosts warning
    "-o", "ConnectTimeout=10",
]


def ssh_key_path():
    """Where Terraform wrote the private key."""
    return outputs().get("test_vm_ssh_key_path") or SSH_KEY_FALLBACK


def test_vm_public_ip():
    """
    The address the checks SSH to — from Terraform output, falling back to a
    live describe so a VM that was replaced is still reachable.
    """
    recorded = outputs().get("test_vm_public_ip")
    if recorded:
        return recorded

    instance_id = test_vm_instance_id()
    if not instance_id:
        return None
    try:
        reservations = _client("ec2").describe_instances(
            InstanceIds=[instance_id])["Reservations"]
        return reservations[0]["Instances"][0].get("PublicIpAddress")
    except Exception:                                   # noqa: BLE001
        return None


def test_vm_reachable(instance_id=None, wait=0):
    """
    Can we open an SSH session to the VM? Optionally wait for it.

    Returns (bool, detail). Replaces the old `ssm_registered()`, and the
    difference is worth stating: that function asked AWS whether the agent had
    checked in, which turned out to be a different question from whether we
    could run anything — a VM could report "Online" and be entirely unusable.
    This asks the question we actually care about by attempting the thing we
    actually want to do, so a pass here cannot be a false positive.

    Two stages, because they fail for different reasons and the distinction is
    the first thing anyone debugging this needs:

      * TCP 22 refused/timed out — the security group, the route, or sshd not
        up yet. Nothing to do with keys.
      * TCP open but authentication rejected — the key pair, or cloud-init not
        having installed authorized_keys yet.
    """
    instance_id = instance_id or test_vm_instance_id()
    address = test_vm_public_ip()
    if not address:
        return False, ("the test VM has no public IP — check that "
                       "map_public_ip_on_launch is set on the workload subnet")

    key = ssh_key_path()
    if not os.path.exists(key):
        return False, (f"the SSH key {key} does not exist. Terraform writes it "
                       f"at apply; if the apply succeeded, check "
                       f"local_sensitive_file.test_vm_key in terraform/main.tf")

    deadline = time.time() + max(wait, 0)
    status = "no attempt made"
    while True:
        # Stage 1: is anything listening?
        try:
            with socket.create_connection((address, 22), timeout=5):
                pass
            port_open = True
            status = f"port 22 open on {address}, but no session yet"
        except OSError as exc:
            port_open = False
            status = f"port 22 on {address} not reachable: {exc}"

        # Stage 2: does a session actually establish?
        if port_open:
            probe = _ssh(["true"], timeout=20)
            if probe.returncode == 0:
                return True, f"SSH to {SSH_USER}@{address} established"
            stderr = (probe.stderr or "").strip().splitlines()
            status = (f"port 22 open on {address} but SSH failed: "
                      f"{stderr[-1] if stderr else 'no error output'}")

        if time.time() >= deadline:
            return False, status
        time.sleep(5)


def _ssh(remote_argv, timeout=60, stdin_data=None):
    """
    One SSH invocation. Returns the CompletedProcess unexamined.

    Split out so run_on_test_vm() and test_vm_reachable() cannot drift in how
    they connect — the retry logic differs between them, the connection details
    must not.
    """
    address = test_vm_public_ip()
    argv = (["ssh", "-i", ssh_key_path()] + SSH_BASE_OPTS
            + [f"{SSH_USER}@{address}"] + list(remote_argv))
    return subprocess.run(
        argv,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def container_egress_ip():
    """
    This container's public address, as AWS sees it. None if it cannot be found.

    The same lookup track_scripts/setup-shell does to set TF_VAR_ssh_ingress_cidr.
    Repeated here rather than read back from Terraform on purpose: the point is
    to catch the case where the two DISAGREE, which is what happens if the
    container's egress address changes part-way through a track.
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com",
                                    timeout=10) as response:
            return response.read().decode().strip()
    except Exception:                                   # noqa: BLE001
        return None


def diagnose_test_vm(instance_id=None):
    """
    Everything knowable about the VM from outside it, in one block.

    Exists because four separate theories about why Run Command sat in Pending
    were each wrong, and each cost a track restart to disprove. The exec path is
    SSH now, but the trap is the same shape — a VM you cannot reach is a VM you
    cannot ask why — so this stays, and everything in it is read from the AWS
    API or the serial console rather than from the VM itself.
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
            f"  key pair:       {inst.get('KeyName', 'NONE')}",
            f"  instance profile: {profile.rsplit('/', 1)[-1]}",
            f"  launched:       {inst.get('LaunchTime')}",
        ]
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"  EC2 lookup failed: {exc}")

    # The SSH path, from this side. Both halves of it, because "the key is
    # missing" and "the security group does not let me in" are different faults
    # with the same symptom.
    key = ssh_key_path()
    lines.append(f"  ssh key:        {key} "
                 f"({'present' if os.path.exists(key) else 'MISSING'})")

    address = test_vm_public_ip()
    if address:
        try:
            with socket.create_connection((address, 22), timeout=5):
                lines.append(f"  tcp 22:         open on {address}")
        except OSError as exc:
            lines.append(f"  tcp 22:         UNREACHABLE on {address} — {exc}")
            lines.append("                  (security group ssh_ingress_cidr, "
                         "or the route to the internet gateway)")

    # What the security group actually permits inbound, since the most likely
    # cause of an unreachable port 22 is that this container's egress address is
    # not the one Terraform was told about.
    try:
        groups = _client("ec2").describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [f"*{cfg.VPC_NAME}*test-vm*"]},
        ])["SecurityGroups"]
        for group in groups[:2]:
            for rule in group.get("IpPermissions", []):
                if rule.get("FromPort") == 22:
                    allowed = [r.get("CidrIp") for r in rule.get("IpRanges", [])]
                    lines.append(f"  sg 22 allows:   {allowed}")
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"  security group lookup failed: {exc}")

    # The line above and the line below are meant to be read together. If this
    # container's egress address is not inside what the security group allows,
    # that is the whole fault — and it is the one failure mode this design has
    # that SSM did not, because setup-shell pins the rule to an address
    # discovered once at track start.
    lines.append(f"  this container:  {container_egress_ip() or 'unknown'}")

    # The agent's own account of itself, read WITHOUT using SSM.
    lines.append("")
    lines.append(console_diagnostics(instance_id))

    return "\n".join(lines)


def console_diagnostics(instance_id=None):
    """
    The `LAB_VM_DIAG` blocks the VM writes to its serial console at boot.

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
    blocks = output.split("=== LAB_VM_DIAG start")
    if len(blocks) < 2:
        return ("console: readable, but no LAB_VM_DIAG block yet — the first "
                "is written about 60s after boot. If the VM has been up for "
                "several minutes and there is still none, user_data did not "
                "run; check the tail of the console for a cloud-init error.")

    newest = blocks[-1].split("=== LAB_VM_DIAG end")[0]
    body = "\n".join(f"    {line}" for line in newest.strip().splitlines())
    return f"  console (newest LAB_VM_DIAG block):\n{body}"


def run_on_test_vm(command, timeout=180, instance_id=None):
    """
    Run one shell command on the test VM. Returns a result dict:

        {"ok": bool, "status": "Success", "rc": 0,
         "stdout": "...", "stderr": "...", "detail": "..."}

    Everything the probe does goes through here so that a failure reports what
    actually happened. An earlier version returned only "no output from the
    probe", which was true and useless: it did not say whether the command
    failed, timed out, or ran fine and printed nothing.

    `instance_id` is accepted and ignored. Addressing is by public IP now that
    this goes over SSH rather than SSM; the parameter stays so callers that pass
    it — and the shape of the SSM version, if it ever comes back — are unchanged.
    """
    address = test_vm_public_ip()
    if not address:
        return {"ok": False, "status": "no-address", "rc": None,
                "stdout": "", "stderr": "",
                "detail": "the test VM has no public IP to connect to"}

    key = ssh_key_path()
    if not os.path.exists(key):
        return {"ok": False, "status": "no-key", "rc": None,
                "stdout": "", "stderr": "",
                "detail": f"the SSH key {key} does not exist — Terraform "
                          f"writes it at apply time"}

    # The command goes over STDIN rather than as an argv element, so it needs no
    # shell quoting on this side and can contain anything. `bash -s` reads the
    # script from stdin; the remote shell never sees it as a word to split.
    try:
        proc = _ssh(["bash", "-s"], timeout=timeout, stdin_data=command)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "rc": None,
                "stdout": "", "stderr": "",
                "detail": f"the command did not finish within {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "status": "no-ssh-client", "rc": None,
                "stdout": "", "stderr": "",
                "detail": ("the `ssh` binary is not installed in this "
                           "container. track_scripts/setup-shell installs "
                           "openssh-client; if you are running by hand, "
                           "`apt install -y openssh-client`")}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    rc = proc.returncode

    # SSH's own failures and the remote command's failures both arrive as a
    # non-zero rc, and they need completely different remedies. 255 is ssh(1)'s
    # reserved code for "I could not establish the session at all".
    if rc == 255:
        first = stderr.splitlines()[0] if stderr else "no error output"
        return {"ok": False, "status": "ssh-failed", "rc": rc,
                "stdout": stdout, "stderr": stderr,
                "detail": (f"could not open an SSH session to {SSH_USER}@"
                           f"{address}: {first}. This is a CONNECTION fault, "
                           f"not a command fault — check the security group's "
                           f"port 22 rule (ssh_ingress_cidr must contain this "
                           f"container's egress address), that the VM is "
                           f"running, and that cloud-init has installed "
                           f"authorized_keys. diagnose_test_vm() reads sshd's "
                           f"state from the console without needing SSH.")}

    status = "Success" if rc == 0 else "Failed"
    detail = f"status={status} rc={rc}"
    if stderr:
        detail += f" stderr={stderr[:300]}"
    if not stdout and not stderr:
        detail += " (both streams empty)"

    return {"ok": rc == 0,
            "status": status, "rc": rc,
            "stdout": stdout, "stderr": stderr, "detail": detail}


def test_vm_can_run_commands(instance_id=None, attempts=2, timeout=150):
    """
    Can we execute anything at all on the VM, and is python3 there?

    Separate from the DNS question on purpose. These two are what Part 3
    genuinely requires — its check runs a Python DNS client over SSH — whereas
    whether a *public* name resolves at boot is a convenience, and not even the
    path Part 3 exercises. Conflating them meant a public-DNS quirk failed the
    whole track start.

    RETRIED, because the failure it guards against is a timing one: cloud-init
    installs authorized_keys a moment after sshd starts accepting connections,
    so a single early attempt turns "not ready yet" into "will never work".
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
        # Only a connection fault or a timeout is worth another go. Anything
        # else means the command reached the VM and genuinely failed, and
        # retrying will not change the answer.
        if result["status"] not in ("ssh-failed", "timeout"):
            break
        if attempt < attempts:
            info(f"    attempt {attempt}: {last} — retrying")

    return False, f"could not run a command on the VM — {last}"


def resolve_from_test_vm(fqdn, resolver=None, timeout=120, boot_wait=0):
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

    reachable, status = test_vm_reachable(instance_id, wait=boot_wait)
    if not reachable:
        return [], (f"The test VM ({instance_id}) is not reachable over SSH — "
                    f"{status}. Nothing can run on it, so this is an "
                    f"environment fault rather than a DNS problem. The usual "
                    f"causes are the security group's port 22 rule not "
                    f"covering this container's egress address, the private "
                    f"key missing from the container, or no route to the "
                    f"internet gateway.")

    payload = base64.b64encode(_DNS_PROBE.encode()).decode()
    command = (f"echo {payload} | base64 -d > /tmp/dnsprobe.py && "
               f"python3 /tmp/dnsprobe.py {fqdn} {resolver or ''}")

    result = run_on_test_vm(command, timeout=timeout, instance_id=instance_id)

    answers = [line.strip() for line in result["stdout"].splitlines()
               if line.strip()]
    if answers:
        return answers, result["stdout"]

    where = f"via {resolver}" if resolver else "via the VM's own resolver"
    detail = result["detail"]

    # THE MOST LIKELY FAILURE IN PART 3, AND THE LEAST OBVIOUS.
    #
    # A DHCP options set is applied to an instance when it takes or renews a
    # lease. Attaching one to the VPC does nothing to an instance that is
    # already running — it keeps the resolver it booted with until the lease
    # renews, which can be hours away. So the participant makes exactly the
    # right change, the console shows the VPC pointing at the NIOS-X host, and
    # the VM carries on asking AmazonProvidedDNS and answering NXDOMAIN.
    #
    # Without this, the evidence for that is one IP address buried in a
    # stderr string. `.2` in the VPC CIDR is AmazonProvidedDNS by AWS
    # convention, so seeing it in the answer path is a positive identification
    # rather than a guess.
    if not resolver:
        amazon_dns = _amazon_provided_dns()
        if amazon_dns and amazon_dns in detail:
            detail += (
                f"\n\n   The query went to {amazon_dns}, which is "
                f"AmazonProvidedDNS — not the Infoblox host. A DHCP options "
                f"set only takes effect when an instance takes or RENEWS a "
                f"lease, so attaching one does not move a VM that is already "
                f"running. The VPC configuration can be completely correct "
                f"and this still fails.\n"
                f"   Run `lab-renew-dns` to renew the lease in place, then "
                f"try again."
            )

    return [], (f"{fqdn} did not resolve from inside the VPC {where}. "
                f"{detail}")


def test_vm_resolver():
    """
    The nameserver the test VM is actually using right now.

    Read from the VM's own /etc/resolv.conf rather than inferred from the
    VPC's DHCP options, because the entire failure mode this exists for is the
    two disagreeing.
    """
    result = run_on_test_vm(
        "grep -m1 '^nameserver' /etc/resolv.conf | awk '{print $2}'",
        timeout=60)
    if not result["ok"]:
        return None, f"could not read the VM's resolver: {result['detail']}"
    value = (result.get("stdout") or "").strip()
    return (value or None), (value or "no nameserver line in /etc/resolv.conf")


# Renewal methods, tried in order until the resolver changes.
#
# AMAZON LINUX 2023 HAS NO dhclient. It uses NetworkManager's built-in DHCP
# client, so the `dhclient -r && dhclient` that every DHCP troubleshooting
# guide reaches for fails with "command not found" — which is why this tries
# NetworkManager first and keeps dhclient only as a fallback for other images.
#
# Each entry is (description, shell command). They run with sudo; the lab's
# ec2-user has passwordless sudo from the AMI's default cloud-init config.
_RENEW_METHODS = (
    ("nmcli device reapply",
     "IFACE=$(ip route show default | awk '{print $5}' | head -1); "
     "sudo nmcli device reapply \"$IFACE\""),
    ("NetworkManager restart",
     "sudo systemctl restart NetworkManager"),
    ("dhclient release and renew",
     "IFACE=$(ip route show default | awk '{print $5}' | head -1); "
     "sudo dhclient -r \"$IFACE\" && sudo dhclient \"$IFACE\""),
)


def renew_dhcp_lease(expect=None, settle=6):
    """
    Make the test VM pick up the VPC's current DHCP options.

    WHY NOT JUST REBOOT. A reboot certainly renews the lease, but it costs one
    to two minutes, drops SSH while it happens, and is a wildly disproportionate
    way to re-read one config value. Renewing in place takes seconds.

    SELF-VERIFYING, because none of these commands fails usefully. `nmcli
    device reapply` exits 0 whether or not it changed the resolver, so trusting
    the exit status would report success on a VM still pointed at
    AmazonProvidedDNS. The only trustworthy signal is /etc/resolv.conf before
    and after, which is what this compares.

    Returns (ok, detail). Never raises.
    """
    before, before_detail = test_vm_resolver()
    if before is None:
        return False, before_detail

    if expect and before == expect:
        return True, f"the VM is already using {before}"

    tried = []
    for description, command in _RENEW_METHODS:
        result = run_on_test_vm(command, timeout=120)
        tried.append(description)

        # Deliberately not checking result["ok"] — see the docstring. A method
        # that is absent or silently ineffective looks identical to one that
        # worked, so the resolver is re-read either way.
        time.sleep(settle)
        after, after_detail = test_vm_resolver()

        if after and after != before:
            if expect and after != expect:
                return False, (
                    f"the resolver changed from {before} to {after}, but "
                    f"{expect} was expected. The VPC's DHCP options may name a "
                    f"different address than the DNS host.")
            return True, (f"resolver is now {after} (was {before}), "
                          f"via {description}")

        if expect and after == expect:
            return True, f"resolver is now {after}, via {description}"

    after, _ = test_vm_resolver()
    return False, (
        f"the VM is still using {after or before} after trying: "
        f"{', '.join(tried)}. Either the VPC's DHCP options set does not name "
        f"the DNS host yet, or this image renews its lease some other way — "
        f"rebooting the instance always works.")


def _amazon_provided_dns():
    """
    The VPC's built-in resolver: the network base address plus two.

    Derived rather than hardcoded, because the lab's CIDR is configurable and
    a wrong guess here would produce a confidently misleading diagnosis.
    """
    cidr = outputs().get("vpc_cidr")
    if not cidr:
        return None
    try:
        import ipaddress
        return str(ipaddress.ip_network(cidr, strict=False).network_address + 2)
    except Exception:                                       # noqa: BLE001
        return None


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

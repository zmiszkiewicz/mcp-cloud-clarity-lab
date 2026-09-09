# --------------------------------------------------------------------------- #
# The cloud VPC Part 3 deploys DNS into.
#
# WHAT THIS FILE DOES AND DELIBERATELY DOES NOT DO
#
# It builds the VPC, two subnets, a test VM, and whichever scaffolding the
# chosen delivery mode needs. It does NOT create the DNS path itself — no VPN
# connection, no resolver endpoint, no forwarding rule, no DHCP option set
# override. Those are the participant's work in Part 3, done through the
# assistant.
#
# The split is on the "slow and boring versus fast and instructive" line. A
# Virtual Private Gateway takes several minutes to attach and teaches nothing;
# bringing a tunnel up against a real Infoblox point of presence takes seconds
# of API work and is the entire point. So the VGW is here and the VPN connection
# is not.
# --------------------------------------------------------------------------- #

locals {
  # Every name carries the participant id. Two people running this track in the
  # same AWS account is the normal case at an event, not an edge case.
  suffix   = substr(replace(var.participant_id, "/[^a-zA-Z0-9]/", ""), 0, 12)
  vpc_name = "${var.vpc_name}-${local.suffix}"
}

data "aws_availability_zones" "available" {
  state = "available"

  # Opt-in zones (Local Zones, Wavelength) are returned as available but cannot
  # host an ordinary subnet, and a Route 53 Resolver endpoint will not accept
  # them either. Restrict to the plain regional AZs.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}


# Amazon Linux 2023, for the two properties the probe depends on: sshd is
# configured and running at boot with the instance's key pair installed (how the
# check reaches in) and python3 is preinstalled (how it queries DNS, since
# nothing is installed on this VM at boot). Looked up rather than pinned so the
# lab does not rot when the AMI is rotated.
#
# The `al2023-ami-2023.*` prefix deliberately excludes `al2023-ami-minimal-*`,
# which ships neither.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

resource "aws_vpc" "lab" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.vpc_name }
}

resource "aws_subnet" "workload_a" {
  vpc_id            = aws_vpc.lab.id
  cidr_block        = var.workload_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]

  # Needed so the SSM agent can reach the public SSM endpoints. See the note
  # above the internet gateway for why this is not the private subnet it was.
  map_public_ip_on_launch = true

  tags = { Name = "${local.vpc_name}-workload-a" }
}

# A Route 53 Resolver endpoint requires two subnets in different availability
# zones, so the second one exists for `forwarder` mode. Created in both modes so
# switching LAB_C3_MODE never needs a re-apply.
resource "aws_subnet" "workload_b" {
  vpc_id            = aws_vpc.lab.id
  cidr_block        = var.workload_subnet_b_cidr
  availability_zone = data.aws_availability_zones.available.names[1]

  # `names[1]` past the end of the list is a Terraform crash with no useful
  # message. A precondition rather than a `check` block on purpose: a check only
  # WARNS, and this has to stop the apply — half a VPC is worse than none.
  lifecycle {
    precondition {
      condition = length(data.aws_availability_zones.available.names) >= 2
      error_message = format(
        "region %s reports %d usable availability zone(s). This lab needs two, because a Route 53 Resolver endpoint requires subnets in different AZs. Pick a different region in config.yml.",
        var.region,
        length(data.aws_availability_zones.available.names),
      )
    }
  }

  tags = { Name = "${local.vpc_name}-workload-b" }
}

resource "aws_route_table" "workload" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.vpc_name}-workload" }
}

resource "aws_route_table_association" "workload_a" {
  subnet_id      = aws_subnet.workload_a.id
  route_table_id = aws_route_table.workload.id
}

resource "aws_route_table_association" "workload_b" {
  subnet_id      = aws_subnet.workload_b.id
  route_table_id = aws_route_table.workload.id
}

# --------------------------------------------------------------------------- #
# The test VM, and how we reach it
#
# HOW THE CHECKS REACH THE TEST VM: SSH, NOT SSM
#
# Part 3's load-bearing assertion runs a DNS query ON this VM, from inside the
# VPC. That needs exactly one capability — execute a command, read stdout — and
# this lab gets it over SSH with a keypair Terraform generates per run.
#
# IT USED TO USE SSM RUN COMMAND, which is the better tool for the job: no key
# material, no inbound port. It was abandoned for a reason that has nothing to
# do with which is better. Systems Manager needs three IAM service prefixes —
# `ssm` for the heartbeat, `ssmmessages` for the channel Run Command is actually
# delivered over, and `ec2messages` for agent startup — and this Instruqt team's
# account can only be granted `ssm`:
#
#     [ERROR] Service ssmmessages is not available in your team
#
# One of three. The instance registers, reports PingStatus "Online", and every
# Run Command sits in Pending until it times out. That combination reads exactly
# like a network fault and is not one, which is how it consumed three rounds of
# debugging aimed at the subnet: first the private subnet and its three
# interface endpoints, then boot ordering against them, then the move to a
# public subnet and the public endpoints. None of them could have worked.
#
# SSH needs only the `ec2` prefix, which this team has. That is the entire
# argument for it. If Instruqt ever enables the other two prefixes, SSM is the
# better path and worth moving back to — the instance profile below is still
# attached, so that switch is a change to scripts/cloud_vpc.py alone.
#
# WHAT THIS COSTS. The VM previously had no inbound access at all. It now opens
# port 22 to `ssh_ingress_cidr`, which track_scripts/setup-shell narrows to the
# lab container's egress address. Password authentication is off (Amazon Linux
# 2023 ships it off and nothing here turns it on), the key is generated per
# track run and never leaves the container, and the instance is an ephemeral
# per-participant sandbox holding nothing. Worth stating plainly rather than
# leaving as a diff nobody reads.
#
# WHY THERE IS AN INTERNET GATEWAY HERE. Inherited from the SSM era, when the
# VM needed to reach the public SSM endpoints, and kept because SSH from the
# container now needs the same thing. Nothing this lab teaches depends on the
# test VM being private: Part 3 asks whether a workload in this VPC can resolve
# an internal name through Infoblox, and that question is identical either way.
# --------------------------------------------------------------------------- #

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.vpc_name}-igw" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.workload.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.lab.id
}

resource "aws_security_group" "test_vm" {
  name        = "${local.vpc_name}-test-vm"
  description = "Test workload. DNS and HTTPS out, SSH in from the lab container only."
  vpc_id      = aws_vpc.lab.id

  # The only way in. Key-only — Amazon Linux 2023 ships sshd with
  # PasswordAuthentication off and nothing in user_data turns it on.
  ingress {
    description = "SSH from the lab container, for the Part 3 DNS probe"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_ingress_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.vpc_name}-test-vm" }
}

# --------------------------------------------------------------------------- #
# The SSH keypair the checks use
#
# Generated per track run rather than taken from a secret. There is nothing to
# rotate, nothing to leak between participants, and no Instruqt secret to create
# before this track can be pushed. The private half is written to the container
# at `ssh_key_path` and never appears in a Terraform OUTPUT — outputs.tf is
# flattened wholesale into scripts/vpc_outputs.json, which the participant can
# read, so a key placed there would be sitting in their working directory.
#
# It IS in Terraform state, unavoidably. State lives only in the container, for
# the lifetime of one track run, alongside the key file itself.
# --------------------------------------------------------------------------- #

resource "tls_private_key" "test_vm" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "test_vm" {
  key_name   = "${local.vpc_name}-test-vm"
  public_key = tls_private_key.test_vm.public_key_openssh

  tags = { Name = "${local.vpc_name}-test-vm" }
}

resource "local_sensitive_file" "test_vm_key" {
  # `private_key_openssh`, not `private_key_pem`. ED25519 has no PEM
  # representation that OpenSSH will read, and `ssh -i` on a PEM-encoded ED25519
  # key fails with "invalid format" — which looks like a permissions problem and
  # is not.
  content              = tls_private_key.test_vm.private_key_openssh
  filename             = var.ssh_key_path
  file_permission      = "0600" # ssh refuses to use a key that is group-readable
  directory_permission = "0700"
}

resource "aws_iam_role" "test_vm" {
  name = "${local.vpc_name}-test-vm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# NO LONGER LOAD-BEARING, and kept deliberately. The checks reach the VM over
# SSH now, so nothing fails if this policy does nothing. It stays for two
# reasons: a workload instance with an SSM agent and an instance profile is what
# a real one looks like, which is the point of the VM; and if Instruqt enables
# the `ssmmessages` and `ec2messages` prefixes for this team, moving back to Run
# Command becomes a change to scripts/cloud_vpc.py with no Terraform work.
resource "aws_iam_role_policy_attachment" "test_vm_ssm" {
  role       = aws_iam_role.test_vm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "test_vm" {
  name = "${local.vpc_name}-test-vm"
  role = aws_iam_role.test_vm.name
}

resource "aws_instance" "test_vm" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.test_vm_instance_type
  subnet_id              = aws_subnet.workload_a.id
  vpc_security_group_ids = [aws_security_group.test_vm.id]
  iam_instance_profile   = aws_iam_instance_profile.test_vm.name
  key_name               = aws_key_pair.test_vm.key_name

  # NO PACKAGE INSTALLS HERE. Not because the subnet cannot reach the internet —
  # since the move to a public subnet it can — but because a boot that depends on
  # a package mirror is a boot that can fail for reasons this lab does not care
  # about. An earlier version installed bind-utils for `dig` and the DNS probe
  # then called a binary that was not always present.
  #
  # scripts/cloud_vpc.py queries DNS with a pure-stdlib Python client instead,
  # copied over SSH and run with the preinstalled python3, so the VM needs
  # nothing beyond what the AMI ships.
  #
  # THE DIAGNOSTIC BLOCK BELOW writes to /dev/console, which means
  # `aws ec2 get-console-output` can read it — needing only the `ec2` prefix and
  # nothing on the instance itself. It exists because the exec path and the only
  # instrument for debugging the exec path must not be the same thing: under SSM
  # they were, and four consecutive theories about why Run Command hung each
  # survived a track restart before being disproved. Under SSH the same trap is
  # there (a VM you cannot reach is a VM you cannot ask why), so the console
  # block now reports sshd first and the SSM agent second.
  #
  # ESCAPING: Terraform templates BOTH `${...}` and `%{...}` inside a heredoc,
  # so a bash variable must be written `$${...}` and a curl format string
  # `%%{http_code}`. An unescaped one of either is a plan-time error — and the
  # `%{` case is easy to miss, because it only appears in the curl line.
  # `${var.region}` below is a real interpolation and is deliberately unescaped.
  user_data = <<-EOT
    #!/bin/bash
    echo "techcorp ai workload test host" > /etc/motd
    REGION=${var.region}

    (
      for delay in 30 45 60 120 240; do
        sleep $${delay}
        {
          echo "=== LAB_VM_DIAG start (uptime $${SECONDS}s) ==="
          echo "-- sshd: THE PATH THE CHECKS USE --"
          systemctl is-active sshd 2>&1
          ss -lntp 2>/dev/null | grep -E ':22\s' || echo "  nothing listening on 22"
          echo "  authorized_keys: $(wc -l < /home/ec2-user/.ssh/authorized_keys 2>/dev/null || echo MISSING)"
          echo "-- ssm agent (not load-bearing; kept for realism) --"
          systemctl is-active amazon-ssm-agent 2>&1
          tail -n 10 /var/log/amazon/ssm/amazon-ssm-agent.log 2>&1
          echo "-- endpoint reachability --"
          for host in ssm ssmmessages ec2messages; do
            code=$(curl -s -o /dev/null -w '%%{http_code}' --max-time 5 \
                   "https://$${host}.$${REGION}.amazonaws.com/" 2>&1)
            echo "  $${host}: HTTP $${code}"
          done
          echo "=== LAB_VM_DIAG end ==="
        } > /dev/console 2>&1
      done
    ) &
  EOT

  # EVERYTHING THE VM NEEDS MUST EXIST BEFORE IT BOOTS.
  #
  # Less critical than it was — SSH has no registration step and no exponential
  # backoff, so a VM whose network settles late simply answers late. Kept
  # because it is still correct, because the SSM agent (still installed) does
  # back off, and because "the route exists but the subnet is not associated
  # with the table holding it" is a genuinely confusing state to debug.
  #
  # Terraform's IMPLICIT graph covers only what is referenced in an argument
  # above: the subnet, the security group, the key pair, the instance profile
  # and (through it) the role. These three are NOT referenced anywhere in this
  # resource, so without naming them here Terraform is free to create them
  # alongside the instance, or after it:
  #
  #   aws_route.internet
  #       No default route, so no return path for an inbound SSH session.
  #
  #   aws_route_table_association.workload_a
  #       Subtler, and the reason the route alone was not enough. A subnet with
  #       no explicit association uses the VPC's MAIN route table, which has no
  #       internet route. The route can therefore exist, the public IP can
  #       exist, and the instance still has no way off the VPC.
  #
  #   aws_iam_role_policy_attachment.test_vm_ssm
  #       The instance profile can be attached while the role behind it is still
  #       empty. The agent then gets AccessDenied on UpdateInstanceInformation
  #       and backs off. This one is the classic: the profile is referenced in
  #       an argument so Terraform orders it, the POLICY on the role is not.
  depends_on = [
    aws_route.internet,
    aws_route_table_association.workload_a,
    aws_iam_role_policy_attachment.test_vm_ssm,
  ]

  # The instance's metadata changes at boot as tags are applied and the AMI
  # lookup rotates. Without this a later apply wants to rebuild it.
  #
  # NOTE: this also means EDITING user_data ABOVE HAS NO EFFECT on a VPC that
  # already exists — including via `bash /opt/lab/build-vpc.sh`. To pick up a
  # change to the console diagnostics you must taint the instance or destroy and
  # re-apply. Harmless in a track run, which always starts from nothing.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = { Name = "${var.test_vm_name}-${local.suffix}" }
}

# --------------------------------------------------------------------------- #
# Mode: as-a-service
#
# A Virtual Private Gateway, attached and route-propagating, ready for the
# participant to attach a Site-to-Site VPN to the Infoblox point of presence.
# The VPN connection itself is NOT created here: its customer gateway needs the
# Cloud Service IPs that only exist once the participant has created the Service
# Deployment on the Infoblox side, which is exactly the work Part 3 is about.
# --------------------------------------------------------------------------- #

resource "aws_vpn_gateway" "lab" {
  count = var.c3_mode == "as-a-service" ? 1 : 0

  vpc_id          = aws_vpc.lab.id
  amazon_side_asn = 64512

  tags = { Name = "${local.vpc_name}-vgw" }
}

# NO ROUTE PROPAGATION HERE — that is Part 3's work, and it was also racy.
#
# `aws_vpn_gateway_route_propagation` failed the build immediately after the
# gateway finished creating. Enabling propagation needs the VPC attachment to
# be fully `attached`, and AWS reports the gateway as created slightly before
# that is consistently true, so the call intermittently fails.
#
# It should not have been here anyway. Part 3's assignment already tells the
# participant the assistant will create "the connection from the VPC to the
# Infoblox point of presence, AND THE ROUTING THAT CARRIES DNS TO IT" — that is
# this. Enabling propagation is one AWS API call the assistant can make, it is
# instructive, and it belongs on the participant's side of the line this file's
# header draws: slow and boring here, fast and instructive there.
#
# Removing it also deletes the race rather than papering over it with a sleep.

# A static route to the corporate space, so the route table shows intent while
# the participant is looking at it.
#
# THIS IS THE SAME DEPENDENCY THE PROPAGATION RACE WAS ABOUT, and an earlier
# version of this comment claimed otherwise. Adding a route that TARGETS a VGW
# does require the VPC attachment to be `attached` — it is the same condition,
# reached by a different call. What makes it survive in practice is that the
# provider's `aws_vpn_gateway` create waits for the attachment before returning,
# and this route depends on that resource, so by the time it runs the wait has
# already happened. `aws_vpn_gateway_route_propagation` had no such wait in
# front of it, which is why that one was the flaky call and this one is not.
#
# So: fine as written, but not for the reason previously given. If this ever
# does fail intermittently, the fix is a wait on attachment state, not a sleep.
resource "aws_route" "on_prem" {
  count = var.c3_mode == "as-a-service" ? 1 : 0

  route_table_id         = aws_route_table.workload.id
  destination_cidr_block = var.on_prem_cidr
  gateway_id             = aws_vpn_gateway.lab[0].id
}

# --------------------------------------------------------------------------- #
# Mode: forwarder
#
# A security group permitting DNS out of the resolver endpoint. The outbound
# endpoint and the forwarding rule are the participant's work.
# --------------------------------------------------------------------------- #

resource "aws_security_group" "resolver" {
  count = var.c3_mode == "forwarder" ? 1 : 0

  name        = "${local.vpc_name}-resolver"
  description = "Route 53 Resolver outbound endpoint. DNS in from the VPC, DNS out to Universal DDI."
  vpc_id      = aws_vpc.lab.id

  ingress {
    description = "DNS UDP from inside the VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.vpc_cidr]
  }

  ingress {
    description = "DNS TCP from inside the VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.vpc_name}-resolver" }
}

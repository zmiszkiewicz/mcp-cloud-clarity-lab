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


# Amazon Linux 2023, for two properties the probe depends on: the SSM agent is
# preinstalled (how the check reaches in) and so is python3 (how it queries
# DNS, since this subnet cannot install packages). Looked up rather than pinned
# so the lab does not rot when the AMI is rotated.
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
# WHY THERE IS AN INTERNET GATEWAY HERE
#
# This started as a private subnet with no internet and three SSM interface
# endpoints, because that is what a real workload subnet looks like. It cost
# three separate debugging cycles and never worked:
#
#   1. `dig` could not be installed, because there is no internet.
#   2. The VM booted before the endpoints existed, so the agent backed off.
#   3. With the ordering fixed, the agent still registered over the `ssm`
#      endpoint while Run Command stayed Pending forever — the `ssmmessages`
#      control channel never established.
#
# (3) IS NOW UNDERSTOOD, AND IT WAS NEVER THE NETWORK. `ssmmessages` is a
# separate IAM service prefix from `ssm`, and config.yml listed only `ssm` in
# the sandbox account's allowed services. So the heartbeat was permitted and the
# control channel was denied — which presents exactly as "registers Online,
# never runs anything", the symptom that sent three rounds of debugging at the
# subnet. Both prefixes are listed in config.yml now, along with `ec2messages`.
#
# The public subnet is kept regardless. The realism was not worth it and the
# reasoning below still holds — but note that it was a fix for a problem this
# file did not have, so if the VM ever fails to register again, check the IAM
# side FIRST.
#
# Nothing this lab teaches depends on the test VM being in a private subnet:
# Part 3 asks whether a workload in this VPC can resolve an internal name
# through Infoblox, and that question is identical either way. What it does
# depend on is being able to run one command on that VM, reliably, every time.
#
# So: an internet gateway, a public subnet, and SSM over the public endpoints —
# the configuration SSM works in by default. Three fewer resources, about a
# minute off the build, and no control-channel mystery.
#
# The VM still has NO inbound access. Its security group opens nothing, there is
# no SSH key, and the only way in is SSM Run Command.
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
  description = "Test workload. DNS out, HTTPS out for SSM, nothing in."
  vpc_id      = aws_vpc.lab.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.vpc_name}-test-vm" }
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

  # NO PACKAGE INSTALLS HERE. Not because the subnet cannot reach the internet —
  # since the move to a public subnet it can — but because a boot that depends on
  # a package mirror is a boot that can fail for reasons this lab does not care
  # about. An earlier version installed bind-utils for `dig` and the DNS probe
  # then called a binary that was not always present.
  #
  # scripts/cloud_vpc.py queries DNS with a pure-stdlib Python client instead,
  # run over SSM. Amazon Linux 2023 ships python3, so the VM needs nothing.
  #
  # THE DIAGNOSTIC BLOCK BELOW writes to /dev/console, which means
  # `aws ec2 get-console-output` can read it. That is the whole point: until now
  # SSM was both the thing under test and the only instrument, so every theory
  # about why Run Command hangs cost a track restart to disprove — four of them
  # did, and each was wrong. Console output needs nothing on the instance role,
  # no log group, no agent, and it works precisely when SSM does not.
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
          echo "=== LAB_SSM_DIAG start (uptime $${SECONDS}s) ==="
          echo "-- agent unit --"
          systemctl is-active amazon-ssm-agent 2>&1
          echo "-- agent log --"
          tail -n 25 /var/log/amazon/ssm/amazon-ssm-agent.log 2>&1
          echo "-- agent errors --"
          tail -n 15 /var/log/amazon/ssm/errors.log 2>&1
          echo "-- can the agent reach its endpoints? --"
          for host in ssm ssmmessages ec2messages; do
            code=$(curl -s -o /dev/null -w '%%{http_code}' --max-time 5 \
                   "https://$${host}.$${REGION}.amazonaws.com/" 2>&1)
            echo "  $${host}: HTTP $${code}"
          done
          echo "=== LAB_SSM_DIAG end ==="
        } > /dev/console 2>&1
      done
    ) &
  EOT

  # EVERYTHING THE AGENT NEEDS MUST EXIST BEFORE THE INSTANCE BOOTS.
  #
  # The SSM agent's retry is exponential and reaches ~15-minute intervals within
  # a few failures, while warm_vpc.py waits 300s. So anything missing at boot is
  # not "slow to converge", it is a failed track start — and an intermittent one,
  # because whether it is missing depends on how Terraform ordered a parallel
  # apply that run.
  #
  # Terraform's IMPLICIT graph covers only what is referenced in an argument
  # above: the subnet, the security group, the instance profile and (through it)
  # the role. These three are NOT referenced anywhere in this resource, so
  # without naming them here Terraform is free to create them alongside the
  # instance, or after it:
  #
  #   aws_route.internet
  #       No default route, so no path to the public SSM endpoints.
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

  # The instance registers with SSM at boot and its metadata changes as tags
  # are applied. Without this a later apply wants to rebuild it.
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

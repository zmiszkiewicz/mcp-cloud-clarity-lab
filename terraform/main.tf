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

  tags = { Name = "${local.vpc_name}-workload-a" }
}

# Route 53 Resolver endpoints require two subnets in different availability
# zones. Created in both modes so switching LAB_C3_MODE never needs a re-apply.
resource "aws_subnet" "workload_b" {
  vpc_id            = aws_vpc.lab.id
  cidr_block        = var.workload_subnet_b_cidr
  availability_zone = data.aws_availability_zones.available.names[1]

  tags = { Name = "${local.vpc_name}-workload-b" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.vpc_name}-private" }
}

resource "aws_route_table_association" "workload_a" {
  subnet_id      = aws_subnet.workload_a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "workload_b" {
  subnet_id      = aws_subnet.workload_b.id
  route_table_id = aws_route_table.private.id
}

# --------------------------------------------------------------------------- #
# The test VM
#
# No public IP, no internet gateway, no SSH. It reaches SSM — and therefore the
# check reaches it — through interface endpoints. That keeps the VPC realistic
# (a private workload subnet is what a customer actually has) and removes the
# security-group hole a bastion would need.
# --------------------------------------------------------------------------- #

resource "aws_security_group" "endpoints" {
  name        = "${local.vpc_name}-endpoints"
  description = "HTTPS from the VPC to the SSM interface endpoints"
  vpc_id      = aws_vpc.lab.id

  ingress {
    # No em dashes or fancy punctuation in a security group description: the AWS
    # API rejects the charset and the failure message does not say why.
    description = "HTTPS from inside the VPC"
    from_port   = 443
    to_port     = 443
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

  tags = { Name = "${local.vpc_name}-endpoints" }
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(["ssm", "ssmmessages", "ec2messages"])

  vpc_id              = aws_vpc.lab.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.workload_a.id, aws_subnet.workload_b.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.vpc_name}-${each.key}" }
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

  # NO PACKAGE INSTALLS HERE. This subnet has no internet gateway and no NAT,
  # which is the point — it is what a real private workload subnet looks like.
  # An earlier version installed bind-utils for `dig`; it could never have
  # worked, and the DNS probe then called a binary that was not present.
  #
  # scripts/cloud_vpc.py queries DNS with a pure-stdlib Python client instead,
  # run over SSM. Amazon Linux 2023 ships python3, so the VM needs nothing.
  user_data = <<-EOT
    #!/bin/bash
    echo "techcorp ai workload test host" > /etc/motd
  EOT

  # BOOT AFTER THE ENDPOINTS EXIST. Not a nicety — this was the bug.
  #
  # Terraform creates the instance in ~13s and the SSM interface endpoints in
  # ~55s, so by default the VM boots forty seconds before there is anything for
  # its agent to talk to. With no internet gateway there is no fallback: the
  # agent's first attempts fail and it enters backoff.
  #
  # It recovers enough to register — describe_instance_information reports
  # PingStatus Online — but the ssmmessages control channel, which is what
  # actually delivers Run Command, does not establish. Commands are accepted
  # and then sit in Pending forever, which is precisely the symptom:
  #
  #     SSM: Online
  #     exec: status=Pending rc=-1 (both streams empty)
  #
  # "Registered" and "can be commanded" are different states, and only the
  # second one matters here.
  depends_on = [aws_vpc_endpoint.ssm]

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
# the participant is looking at it. Not subject to the same race: adding a
# route to a gateway does not require the attachment to have settled.
resource "aws_route" "on_prem" {
  count = var.c3_mode == "as-a-service" ? 1 : 0

  route_table_id         = aws_route_table.private.id
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

###############################################################################
# The NIOS-X host — the DNS server that actually answers
###############################################################################
# An EC2 instance built from the privately shared Infoblox NIOS-X AMI. Its whole
# bootstrap is a join token: on first boot it calls csp.infoblox.com, registers
# against the tenant that issued the token, and appears under Infrastructure >
# Hosts. From that moment it is managed from the Portal, not from Terraform.
#
# WHY THIS EXISTS AT ALL
# ----------------------
# Part 3 first tried NIOS-X as a Service — an endpoint in an Infoblox point of
# presence, reached over IPsec. A sandbox tenant cannot do that; every service
# location was refused with "Service location <x> not supported", because
# placing an endpoint in a PoP is an entitlement a sandbox does not have.
#
# A host in our own VPC needs no PoP, no tunnel, and no access location. It also
# gives the tenant a real Universal DDI host, which is what LAB_AUTH_MODE=host
# has been waiting for.
#
# WHY IT STOPS AT "A REGISTERED HOST"
# -----------------------------------
# Turning on the DNS service is a CSP-side operation against the host's *pool*,
# and the pool does not exist until registration completes — minutes after
# `apply` returns. There is nothing here for Terraform to reference, so
# scripts/niosx_host.py does that half afterwards.
#
# EVERYTHING HERE IS CONDITIONAL on an AMI being known for this region. The
# image is shared privately rather than published, so there is no data source
# that can find it, and only eu-central-1 has a confirmed id today. Where none
# is known, count = 0 and the lab comes up without a host — Parts 1, 2 and 4 do
# not need one. See lab_config.NIOSX_AMI_BY_REGION.
###############################################################################

locals {
  # One switch for every resource in this file.
  niosx_enabled = var.niosx_ami_id != "" && var.infoblox_join_token != ""
  niosx_count   = local.niosx_enabled ? 1 : 0
}

###############################################################################
# Security group
###############################################################################
# DNS in from the VPC, everything out.
#
# Outbound is genuinely unrestricted and load bearing: the host needs 443 to
# csp.infoblox.com to register and to stay managed, and it needs to resolve
# recursively for anything it is not authoritative for. Narrowing egress here
# is what makes a host register and then go quiet an hour later.
###############################################################################

resource "aws_security_group" "niosx" {
  count = local.niosx_count

  name        = "${var.vpc_name}-${var.participant_id}-niosx"
  description = "NIOS-X host: DNS from the VPC, outbound to the CSP"
  vpc_id      = aws_vpc.lab.id

  ingress {
    description = "DNS over UDP from inside the VPC"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.vpc_cidr]
  }

  ingress {
    description = "DNS over TCP from inside the VPC, for zone transfers and large answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "The host must reach csp.infoblox.com and resolve recursively"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.vpc_name}-${var.participant_id}-niosx" }
}

###############################################################################
# Network interface
###############################################################################
# Static private IP, and it has to be static: the assignment prints this address
# for the participant, Part 3's check queries it, and the DHCP options set the
# assistant creates points at it. A dynamic address would mean three places
# discovering it separately.
###############################################################################

resource "aws_network_interface" "niosx" {
  count = local.niosx_count

  subnet_id       = aws_subnet.workload_a.id
  private_ips     = [var.niosx_host_ip]
  security_groups = [aws_security_group.niosx[0].id]

  tags = { Name = "${var.vpc_name}-${var.participant_id}-niosx-nic" }
}

###############################################################################
# The instance
###############################################################################

resource "aws_instance" "niosx" {
  count = local.niosx_count

  ami           = var.niosx_ami_id
  instance_type = var.niosx_instance_type

  network_interface {
    network_interface_id = aws_network_interface.niosx[0].id
    device_index         = 0
  }

  # The join token is the entire bootstrap. cloud-init hands it to the NIOS-X
  # host_setup module, which registers the host with the tenant that issued it.
  #
  # KEEP THIS HEREDOC EXACTLY AS IT IS. NIOS-X parses it as YAML and is
  # unforgiving about the shape — no extra keys, no reordering, and the
  # #cloud-config line must be first.
  user_data = <<-EOF
    #cloud-config
    host_setup:
      jointoken: "${var.infoblox_join_token}"
  EOF

  # IMDSv2 only. Nothing reads instance metadata, so requiring a token costs
  # nothing and keeps the host off the standard scan findings.
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # The host cannot register without a route to the internet, and an instance
  # that boots before the route exists fails its first call home and retries on
  # a long backoff. Same reason the test VM depends on these.
  depends_on = [
    aws_route.internet,
    aws_route_table_association.workload_a,
  ]

  tags = { Name = "${var.vpc_name}-${var.participant_id}-niosx" }
}

###############################################################################
# Elastic IP
###############################################################################
# The host needs outbound internet to reach csp.infoblox.com. The workload
# subnet routes through an internet gateway rather than a NAT gateway, and an
# IGW only carries traffic for instances that have a public address — so
# without this the host has no path to the CSP and never registers.
###############################################################################

resource "aws_eip" "niosx" {
  count  = local.niosx_count
  domain = "vpc"

  tags = { Name = "${var.vpc_name}-${var.participant_id}-niosx-eip" }
}

resource "aws_eip_association" "niosx" {
  count = local.niosx_count

  network_interface_id = aws_network_interface.niosx[0].id
  allocation_id        = aws_eip.niosx[0].id
  private_ip_address   = var.niosx_host_ip

  depends_on = [aws_instance.niosx]
}

###############################################################################
# DHCP options are NOT created here — deliberately
###############################################################################
# Pointing the VPC at the NIOS-X host is Part 3's whole exercise. The assistant
# does it through the AWS MCP server, with the participant approving the call,
# and `lab-dig` then proves it worked from inside the VPC.
#
# Doing it at boot would leave Part 3 with nothing to do and would make its
# verification meaningless: a check that passes before the challenge starts is
# not a check. The VPC therefore comes up on AmazonProvidedDNS, which resolves
# public names and knows nothing about svc.techcorp.internal — exactly the
# starting condition the assignment describes.
###############################################################################

# Every default here MUST match the matching constant in scripts/lab_config.py.
# The Python side is the source of truth that the assignment prose and the
# checks read; these defaults exist so `terraform plan` works standalone during
# development. setup-shell passes the real values as TF_VAR_* so the two cannot
# drift at runtime.

variable "region" {
  description = "AWS region for the lab VPC. Must be in config.yml's regions list."
  type        = string
  default     = "us-east-1"
}

variable "participant_id" {
  description = "INSTRUQT_PARTICIPANT_ID. Suffixes every name so two participants in one account never collide."
  type        = string
}

variable "track_slug" {
  description = "INSTRUQT_TRACK_SLUG. Tags every resource so a sweep can find orphans."
  type        = string
  default     = "infoblox-mcp-cloud-clarity"
}

variable "vpc_name" {
  description = "[VPC NAME] in the assignment prose. Matches lab_config.VPC_NAME."
  type        = string
  default     = "techcorp-ai-vpc"
}

variable "vpc_cidr" {
  description = "CIDR for the lab VPC. Must not overlap the on-prem 10.30.0.0/16 space."
  type        = string
  default     = "10.40.0.0/16"
}

variable "workload_subnet_cidr" {
  description = "Subnet the test VM and any resolver endpoints live in."
  type        = string
  default     = "10.40.1.0/24"
}

variable "workload_subnet_b_cidr" {
  description = "Second AZ. Route 53 Resolver endpoints require two subnets in different AZs."
  type        = string
  default     = "10.40.2.0/24"
}

variable "test_vm_name" {
  description = "Name tag of the test VM Part 3 step 3 digs from."
  type        = string
  default     = "techcorp-ai-test"
}

variable "test_vm_instance_type" {
  description = "Small is fine — this VM runs dig and nothing else."
  type        = string
  default     = "t3.micro"
}

variable "ssh_ingress_cidr" {
  description = <<-EOT
    Who may reach port 22 on the test VM.

    track_scripts/setup-shell sets this to the lab container's own egress
    address as a /32, discovered with `curl https://checkip.amazonaws.com`.

    THE DEFAULT IS DELIBERATELY WIDE. If that lookup fails — no egress, DNS not
    up yet, the service down — setup-shell falls back to this rather than
    guessing an address, because the failure modes are not symmetrical: a wide
    security group on an ephemeral single-participant sandbox VM that accepts
    key-only authentication is a small thing, and a security group that excludes
    the one host allowed to talk to it is a track that cannot start. Narrow it
    to a fixed egress range if the estate ever has one.
  EOT
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.ssh_ingress_cidr, 0))
    error_message = "ssh_ingress_cidr must be a CIDR block, e.g. 203.0.113.4/32."
  }
}

variable "ssh_key_path" {
  description = <<-EOT
    Where the generated private key is written in the lab container.

    Outside the repo on purpose. scripts/ is the participant's working directory
    and the whole of vpc_outputs.json is readable there; /opt/lab is not
    somewhere they are led. See the keypair note in main.tf.
  EOT
  type        = string
  default     = "/opt/lab/test_vm_key"
}

variable "c3_mode" {
  description = <<-EOT
    Which Part 3 delivery path to pre-stage.

      as-a-service  a Virtual Private Gateway attached to the VPC, ready for the
                    participant to bring up a Site-to-Site VPN to the Infoblox
                    point of presence. The VGW is the slow part (several
                    minutes) so it is created here rather than on the clock.

      forwarder     two Route 53 Resolver outbound-endpoint subnets and a
                    security group, ready for the participant to create the
                    endpoint and the forwarding rule.

    Must match LAB_C3_MODE in lab_config.py. See README.md "Part 3 delivery modes".
  EOT
  type        = string
  default     = "as-a-service"

  validation {
    condition     = contains(["as-a-service", "forwarder"], var.c3_mode)
    error_message = "c3_mode must be \"as-a-service\" or \"forwarder\"."
  }
}

variable "on_prem_cidr" {
  description = "The corporate space reachable over the tunnel. Matches lab_config.ADDRESS_BLOCK."
  type        = string
  default     = "10.30.0.0/16"
}

###############################################################################
# NIOS-X host
###############################################################################

variable "niosx_ami_id" {
  description = <<-EOT
    AMI of the privately shared Infoblox NIOS-X image — NOT the AWS Marketplace
    listing.

    EMPTY DISABLES THE HOST. Every resource in niosx.tf is counted on this, so
    a region with no known image builds the rest of the lab and skips the host
    rather than failing the apply. Parts 1, 2 and 4 do not need it.

    The image is shared privately with this organisation's accounts rather than
    published, so no data source can find it and there is no way to look one up
    at plan time. eu-central-1 is the only confirmed region: ami-08659b5070b66249d,
    the same image tech-summit-security-niosx, app-migration-niosx and
    nios-rpz-genai-block all hardcode.

    track_scripts/setup-shell passes this from lab_config.NIOSX_AMI_BY_REGION,
    which is the single source of truth. The default here is empty so a
    standalone `terraform plan` during development does not silently build a
    host against an image that does not exist in the developer's region.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.niosx_ami_id == "" || can(regex("^ami-[0-9a-f]{8,17}$", var.niosx_ami_id))
    error_message = "niosx_ami_id must be empty or look like ami-0123456789abcdef0."
  }
}

variable "infoblox_join_token" {
  description = <<-EOT
    The token that enrols the NIOS-X host into the CSP tenant.

    Minted by `python3 scripts/niosx_host.py --token` before terraform runs, and
    passed as TF_VAR_infoblox_join_token. It is the host's entire bootstrap: on
    first boot it calls csp.infoblox.com, registers against the tenant that
    issued the token, and appears under Infrastructure > Hosts.

    EMPTY DISABLES THE HOST, for the same reason as niosx_ami_id — an instance
    launched without a token boots, registers with nothing, and sits there
    costing money while looking healthy.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "niosx_instance_type" {
  description = "m5.large, matching every other NIOS-X lab in the estate. Smaller is untested."
  type        = string
  default     = "m5.large"
}

variable "niosx_host_ip" {
  description = <<-EOT
    Static private address of the NIOS-X host. Must be inside
    workload_subnet_cidr and clear of the four addresses AWS reserves at the
    bottom of every subnet.

    Matches lab_config.NIOSX_HOST_IP, which is what the assignment prose and
    Part 3's check both read.
  EOT
  type        = string
  default     = "10.40.1.53"
}

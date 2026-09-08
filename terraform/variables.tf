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

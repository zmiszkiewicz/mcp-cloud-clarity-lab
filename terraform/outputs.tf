# Consumed two ways:
#
#   * `terraform output -json | jq 'map_values(.value)'` is written to
#     scripts/vpc_outputs.json by track_scripts/setup-shell. scripts/cloud_vpc.py
#     reads that file, so the Part 3 check never shells out to terraform.
#   * `terraform output` directly, by a participant poking around, and by the
#     assistant when it needs an id to act on.
#
# Keep the key names in step with cloud_vpc.outputs() — they are the contract.

output "vpc_id" {
  description = "The VPC Part 3 deploys DNS into."
  value       = aws_vpc.lab.id
}

output "vpc_name" {
  description = "[VPC NAME] as it appears in the assignment prose and in the console."
  value       = local.vpc_name
}

output "vpc_cidr" {
  value = aws_vpc.lab.cidr_block
}

output "workload_subnet_id" {
  description = "Where the test VM lives, and where a resolver endpoint would go."
  value       = aws_subnet.workload_a.id
}

output "workload_subnet_ids" {
  description = "Both AZs. A Route 53 Resolver endpoint needs two."
  value       = [aws_subnet.workload_a.id, aws_subnet.workload_b.id]
}

output "route_table_id" {
  value = aws_route_table.workload.id
}

output "test_vm_instance_id" {
  description = "The VM the Part 3 check runs dig on, over SSM."
  value       = aws_instance.test_vm.id
}

output "test_vm_private_ip" {
  value = aws_instance.test_vm.private_ip
}

# --------------------------------------------------------------------------- #
# How the checks reach the VM. See "HOW THE CHECKS REACH THE TEST VM" in
# main.tf — SSH rather than SSM, because this Instruqt team can be granted the
# `ssm` service prefix but not `ssmmessages` or `ec2messages`.
#
# THE PRIVATE KEY IS NOT AN OUTPUT. Everything here is flattened into
# scripts/vpc_outputs.json, which sits in the participant's working directory.
# Terraform writes the key straight to `ssh_key_path` instead; only the path
# travels through here.
# --------------------------------------------------------------------------- #

output "test_vm_public_ip" {
  description = "Where the Part 3 probe SSHes to."
  value       = aws_instance.test_vm.public_ip
}

output "test_vm_ssh_user" {
  description = "Amazon Linux 2023's default login."
  value       = "ec2-user"
}

output "test_vm_ssh_key_path" {
  description = "Private key for the test VM, written by Terraform at apply."
  value       = local_sensitive_file.test_vm_key.filename
}

output "vpn_gateway_id" {
  description = "as-a-service mode. Empty in forwarder mode."
  value       = try(aws_vpn_gateway.lab[0].id, "")
}

output "amazon_side_asn" {
  description = "The ASN the participant enters as the Access Location ASN on the Infoblox side."
  value       = try(aws_vpn_gateway.lab[0].amazon_side_asn, null)
}

output "resolver_security_group_id" {
  description = "forwarder mode. Empty in as-a-service mode."
  value       = try(aws_security_group.resolver[0].id, "")
}

# Not produced by Terraform — the participant's work creates it. Declared here
# so vpc_outputs.json always has the key and cloud_vpc.dns_service_ip() can look
# for it without a KeyError. Populated by 03/check-shell when it can be read.
output "dns_service_ip" {
  description = "[DNS SERVICE IP]. Empty until the participant deploys DNS into the VPC."
  value       = ""
}

output "vpn_connection_id" {
  description = "Empty until the participant creates the tunnel to the Infoblox point of presence."
  value       = ""
}

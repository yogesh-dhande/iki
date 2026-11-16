output "vpc_id" {
  description = "The VPC's ID."
  value       = aws_vpc.this.id
}

output "cidr" {
  description = "The VPC's address range."
  value       = aws_vpc.this.cidr_block
}

output "zones" {
  description = "The availability zones it spans, in subnet order."
  value       = local.zones
}

output "public_subnet_ids" {
  description = "Subnets for internet-facing load balancers and the NAT gateways."
  value       = aws_subnet.public[*].id
}

output "public_subnet_cidrs" {
  description = "Their ranges: an ALB reaches pods from these, which the chart's NetworkPolicy admits."
  value       = aws_subnet.public[*].cidr_block
}

output "private_subnet_ids" {
  description = "Subnets for nodes, pods, internal load balancers and VM-tier machines."
  value       = aws_subnet.private[*].id
}

output "private_subnet_cidrs" {
  description = "Their ranges."
  value       = aws_subnet.private[*].cidr_block
}

output "database_subnet_ids" {
  description = "Subnets for the database, with no route out of the VPC."
  value       = aws_subnet.database[*].id
}

output "nat_public_ips" {
  description = "The addresses traffic from the VPC leaves by, for allowlists elsewhere."
  value       = aws_eip.nat[*].public_ip
}

output "s3_prefix_list_id" {
  description = "The S3 gateway endpoint's prefix list, for security groups that let pods or machines reach S3."
  value       = aws_vpc_endpoint.s3.prefix_list_id
}

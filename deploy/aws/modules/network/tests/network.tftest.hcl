# Runs against a mocked AWS provider: no account, no credentials. Each run checks a
# property ADR 0097 promises. Mocked IDs repeat across a resource's instances, so a check
# that compares IDs names them apart with override_resource first.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # S3's ranges in eu-west-1, as its gateway endpoint's prefix list holds them.
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = {
      entries = [
        { cidr = "52.218.0.0/17", description = "" },
        { cidr = "3.5.64.0/21", description = "" },
        { cidr = "52.92.40.0/21", description = "" },
      ]
    }
  }
  # The provider checks ARNs it's given, so mocked ones must look real.
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-west-1:123456789012:log-group:/aws/vpc/iki-test/flow-logs" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/iki-test-flow-logs" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
}

variables {
  name  = "iki-test"
  zones = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
}

run "spreads_over_three_zones_with_a_nat_gateway_in_each" {
  assert {
    condition     = length(distinct(aws_subnet.private[*].availability_zone)) == 3
    error_message = "Private subnets should be in three different zones."
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 3
    error_message = "Each zone should have its own NAT gateway."
  }
  assert {
    condition     = length(aws_route.private_nat) == 3
    error_message = "Each zone's private subnet should have its own route out."
  }
}

run "nothing_gets_a_public_address_by_default" {
  assert {
    condition     = alltrue([for s in concat(aws_subnet.public, aws_subnet.private, aws_subnet.database) : !s.map_public_ip_on_launch])
    error_message = "No subnet should hand out public addresses."
  }
}

run "the_database_subnets_have_no_way_out" {
  # Mocked IDs repeat across a resource's instances: name the tables apart.
  override_resource {
    target = aws_route_table.database
    values = { id = "rtb-database" }
  }
  override_resource {
    target = aws_route_table.private
    values = { id = "rtb-private" }
  }
  override_resource {
    target = aws_route_table.public
    values = { id = "rtb-public" }
  }
  assert {
    condition = alltrue([
      for r in concat([aws_route.public_internet], aws_route.private_nat) :
      r.route_table_id != aws_route_table.database.id
    ])
    error_message = "The database's route table should have no routes out of the VPC."
  }
  assert {
    condition     = !contains(aws_vpc_endpoint.s3.route_table_ids, aws_route_table.database.id)
    error_message = "The database's subnets shouldn't reach S3 either."
  }
}

run "subnets_never_overlap_and_fit_the_vpc" {
  variables {
    cidr  = "10.8.0.0/16"
    zones = ["eu-west-1a", "eu-west-1b", "eu-west-1c", "eu-west-1d"]
  }
  assert {
    condition = length(distinct(concat(
      aws_subnet.public[*].cidr_block, aws_subnet.private[*].cidr_block, aws_subnet.database[*].cidr_block
    ))) == 12
    error_message = "Every subnet should have its own range."
  }
  assert {
    condition = alltrue([
      for s in concat(aws_subnet.public, aws_subnet.private, aws_subnet.database) :
      cidrsubnet(s.cidr_block, 0, 0) == s.cidr_block && startswith(s.cidr_block, "10.8.")
    ])
    error_message = "Every subnet should be inside the VPC's range."
  }
}

run "one_nat_gateway_for_staging" {
  variables {
    single_nat_gateway = true
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 1
    error_message = "single_nat_gateway should make one NAT gateway."
  }
}

run "the_default_security_group_allows_nothing" {
  assert {
    condition     = length(aws_default_security_group.this.ingress) == 0 && length(aws_default_security_group.this.egress) == 0
    error_message = "The default security group should have no rules (CIS AWS 5.4)."
  }
}

run "flow_logs_record_everything_encrypted_with_a_rotated_key" {
  assert {
    condition     = aws_flow_log.this.traffic_type == "ALL"
    error_message = "Flow logs should record accepted and rejected traffic."
  }
  assert {
    condition     = aws_kms_key.logs.enable_key_rotation && aws_cloudwatch_log_group.flow_logs.kms_key_id == aws_kms_key.logs.arn
    error_message = "Flow logs should be encrypted with their own rotated key."
  }
}

run "aws_services_are_reached_inside_the_vpc" {
  assert {
    condition     = alltrue([for e in aws_vpc_endpoint.interface : e.private_dns_enabled])
    error_message = "Interface endpoints should take over the services' names, so clients use them unchanged."
  }
  assert {
    condition     = contains(keys(aws_vpc_endpoint.interface), "eks-auth")
    error_message = "EKS Pod Identity needs the eks-auth endpoint."
  }
}

run "pods_reach_s3_at_its_endpoints_ranges" {
  assert {
    condition     = output.s3_cidrs == tolist(["3.5.64.0/21", "52.218.0.0/17", "52.92.40.0/21"])
    error_message = "s3_cidrs should be the S3 gateway endpoint's prefix list's ranges."
  }
}

run "refuses_a_range_too_small_for_pods" {
  command = plan
  variables {
    cidr = "10.40.0.0/24"
  }
  expect_failures = [var.cidr]
}

run "refuses_one_zone_or_the_same_zone_twice" {
  command = plan
  variables {
    zones = ["eu-west-1a", "eu-west-1a"]
  }
  expect_failures = [var.zones]
}

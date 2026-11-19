# A VPC over several zones (ADR 0097). Nodes and the database sit in private subnets
# with no public address; only load balancers and NAT gateways are in public ones.

data "aws_region" "here" {}

data "aws_caller_identity" "here" {}

data "aws_partition" "here" {}

locals {
  zones = var.zones
  count = length(var.zones)
  # A /18 per zone for nodes and pods (the VPC CNI gives each pod an address), then a
  # /22 per zone for load balancers and NAT gateways, and one for the database, all in
  # the range's last quarter.
  private_cidrs  = [for i in range(local.count) : cidrsubnet(var.cidr, 2, i)]
  public_cidrs   = [for i in range(local.count) : cidrsubnet(var.cidr, 6, 48 + i)]
  database_cidrs = [for i in range(local.count) : cidrsubnet(var.cidr, 6, 52 + i)]
  nat_count      = var.single_nat_gateway ? 1 : local.count
  tags           = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = var.name })
}

# The default security group allows nothing, so nothing lands in it by accident (CIS
# AWS 5.4).
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-default-unused" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = var.name })
}

resource "aws_subnet" "public" {
  count             = local.count
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.public_cidrs[count.index]
  availability_zone = local.zones[count.index]
  # Load balancers get their addresses from AWS; nothing here gets one by default.
  map_public_ip_on_launch = false
  tags = merge(local.tags, {
    Name                     = "${var.name}-public-${local.zones[count.index]}"
    "iki.ikisci.org/tier"    = "public"
    "kubernetes.io/role/elb" = "1"
  })
}

resource "aws_subnet" "private" {
  count                   = local.count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.private_cidrs[count.index]
  availability_zone       = local.zones[count.index]
  map_public_ip_on_launch = false
  tags = merge(local.tags, {
    Name                              = "${var.name}-private-${local.zones[count.index]}"
    "iki.ikisci.org/tier"             = "private"
    "kubernetes.io/role/internal-elb" = "1"
  })
}

# The database's subnets have no route out of the VPC at all.
resource "aws_subnet" "database" {
  count                   = local.count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.database_cidrs[count.index]
  availability_zone       = local.zones[count.index]
  map_public_ip_on_launch = false
  tags = merge(local.tags, {
    Name                  = "${var.name}-database-${local.zones[count.index]}"
    "iki.ikisci.org/tier" = "database"
  })
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = merge(local.tags, { Name = "${var.name}-nat-${local.zones[count.index]}" })
}

resource "aws_nat_gateway" "this" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = merge(local.tags, { Name = "${var.name}-${local.zones[count.index]}" })
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = local.count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Each zone's private subnet goes out through its own zone's NAT gateway, so losing a
# zone doesn't cut the others off.
resource "aws_route_table" "private" {
  count  = local.count
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-private-${local.zones[count.index]}" })
}

resource "aws_route" "private_nat" {
  count                  = local.count
  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
}

resource "aws_route_table_association" "private" {
  count          = local.count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table" "database" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-database" })
}

resource "aws_route_table_association" "database" {
  count          = local.count
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}

# --- VPC endpoints -----------------------------------------------------------------------

# S3 through a gateway endpoint: free, and files never cross the NAT gateways.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.here.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = merge(local.tags, { Name = "${var.name}-s3" })
}

# The ranges S3 answers at through it. Pods' NetworkPolicy names these: a policy names
# addresses, not services. AWS adds ranges now and then, and each apply reads them again.
data "aws_ec2_managed_prefix_list" "s3" {
  id = aws_vpc_endpoint.s3.prefix_list_id
}

resource "aws_security_group" "endpoints" {
  count       = length(var.interface_endpoints) > 0 ? 1 : 0
  name        = "${var.name}-endpoints"
  description = "HTTPS from inside the VPC to AWS service endpoints"
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name}-endpoints" })
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  count             = length(var.interface_endpoints) > 0 ? 1 : 0
  security_group_id = aws_security_group.endpoints[0].id
  description       = "HTTPS from the VPC"
  cidr_ipv4         = var.cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(var.interface_endpoints)
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.here.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  tags                = merge(local.tags, { Name = "${var.name}-${each.key}" })
}

# --- Flow logs ---------------------------------------------------------------------------

resource "aws_kms_key" "logs" {
  description             = "${var.name}: VPC flow logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.logs_key.json
  tags                    = local.tags
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.name}-flow-logs"
  target_key_id = aws_kms_key.logs.key_id
}

data "aws_iam_policy_document" "logs_key" {
  #checkov:skip=CKV_AWS_109:A key policy's "*" is the key itself; this is AWS's default statement, which hands the key's control to the account's IAM.
  #checkov:skip=CKV_AWS_111:As above: the account's IAM policies decide who may write with it.
  #checkov:skip=CKV_AWS_356:As above: in a key policy, "*" can only mean this key.
  statement {
    sid       = "TheAccountManagesIt"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.here.partition}:iam::${data.aws_caller_identity.here.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogsUsesIt"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.here.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.here.partition}:logs:${data.aws_region.here.region}:${data.aws_caller_identity.here.account_id}:log-group:/aws/vpc/${var.name}/flow-logs"]
    }
  }
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/${var.name}/flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
  tags              = local.tags
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.here.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${var.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

resource "aws_flow_log" "this" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn             = aws_iam_role.flow_logs.arn
  max_aggregation_interval = 60
  tags                     = local.tags
}

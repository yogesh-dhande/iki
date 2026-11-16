variable "name" {
  description = "Names everything this module makes, such as iki-production."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name))
    error_message = "Use 3 to 32 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "cidr" {
  description = "The VPC's address range. A /16 leaves room for the pods' addresses: the VPC CNI gives each pod one from its node's subnet."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.cidr, 0)) && tonumber(split("/", var.cidr)[1]) <= 18
    error_message = "Give a CIDR block of /18 or larger, such as 10.40.0.0/16."
  }
}

variable "zones" {
  description = "The availability zones to spread over, by name, such as [\"eu-west-1a\", \"eu-west-1b\", \"eu-west-1c\"]. Named, not looked up, so a zone AWS adds later never moves a subnet. Three keep Multi-AZ RDS and EKS running through the loss of one."
  type        = list(string)

  validation {
    condition     = length(var.zones) >= 2 && length(var.zones) <= 4 && length(distinct(var.zones)) == length(var.zones)
    error_message = "Name 2 to 4 different zones: RDS and EKS need at least two."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for every zone, rather than one in each. Cheaper, but losing its zone cuts the others off from the internet: for staging, not production."
  type        = bool
  default     = false
}

variable "interface_endpoints" {
  description = "AWS services reached through interface endpoints in the VPC, so their traffic never leaves AWS's network. S3 always has a gateway endpoint. Empty: through the NAT gateways."
  type        = list(string)
  default     = ["ecr.api", "ecr.dkr", "sts", "eks-auth", "ec2", "ssm", "secretsmanager", "logs", "kms"]
}

variable "flow_log_retention_days" {
  description = "How long VPC flow logs are kept in CloudWatch."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Tags on everything this module makes."
  type        = map(string)
  default     = {}
}

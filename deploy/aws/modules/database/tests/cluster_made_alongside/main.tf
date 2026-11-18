# A root for database.tftest.hcl: the module given the cluster's security group while the
# cluster is made in the same apply, as deploy/aws/production does. A real first plan doesn't
# know the group's ID yet, and a mocked provider would, so a value unknown until apply
# (timestamp() is, in a plan) stands in for it.

provider "aws" {}

provider "aws" {
  alias = "dr"
}

resource "terraform_data" "cluster_security_group" {
  input = timestamp()
}

module "database" {
  source    = "../.."
  providers = { aws = aws, aws.dr = aws.dr }

  name                   = "iki-test"
  vpc_id                 = "vpc-0123"
  subnet_ids             = ["subnet-a", "subnet-b", "subnet-c"]
  client_security_groups = { cluster = terraform_data.cluster_security_group.output }
}

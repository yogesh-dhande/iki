# A root for access.tftest.hcl: the module given a private zone made in the same apply, as
# deploy/aws/production does. A real first plan doesn't know the zone's ARN yet, and a mocked
# provider would, so a value unknown until apply (timestamp() is, in a plan) stands in for
# the zone's.

resource "terraform_data" "zone" {
  input = timestamp()
}

module "access" {
  source = "../.."

  name         = "iki-test"
  cluster_name = "iki-test"
  cluster_arn  = "arn:aws:eks:eu-west-1:123456789012:cluster/iki-test"
  files = {
    bucket_arn  = "arn:aws:s3:::iki-test-files-123456789012"
    kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/files"
    prefix      = "prod/"
  }
  external_secrets = {
    secret_arns = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:iki-test/django-AbCdEf"]
    kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/secrets"
  }
  # Bare, not in a string template: OpenTofu knows a template is never null.
  external_dns = { zone_arn = terraform_data.zone.output }
}

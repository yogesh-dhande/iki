# public_host's name inside the VPC, for an internal deployment: what backend workers, run
# pods and the VM tier's machines send to (publicUrl). A private zone for that one name,
# associated with this VPC only, so everything else resolves as before. The ALB is the
# AWS Load Balancer Controller's, made from Iki's Ingress after this applies, so its record
# is ExternalDNS's (external_dns_values), not OpenTofu's.

resource "aws_route53_zone" "internal" {
  #checkov:skip=CKV2_AWS_38:DNSSEC signing is for public zones; Route 53 doesn't sign private ones.
  #checkov:skip=CKV2_AWS_39:A private zone's queries are logged by Route 53 Resolver's query logging for the VPC, if you want them, not by the zone.
  count         = var.internal && var.internal_dns ? 1 : 0
  name          = var.public_host
  comment       = "${var.name}: ${var.public_host} inside the VPC, at the internal ALB"
  force_destroy = true # its records are ExternalDNS's, which go with the deployment

  vpc {
    vpc_id = module.network.vpc_id
  }

  tags = merge(var.tags, { "iki.ikisci.org/deployment" = var.name })
}

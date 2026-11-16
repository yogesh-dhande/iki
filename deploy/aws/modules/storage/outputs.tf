output "bucket" {
  description = "The files bucket's name: the chart's files.bucket."
  value       = local.bucket
}

output "bucket_arn" {
  description = "The files bucket's ARN, for the pods' roles."
  value       = local.bucket_arn
}

output "region" {
  description = "Where the files bucket is: the chart's files.region."
  value       = local.region
}

output "kms_key_arn" {
  description = "The key files are encrypted with: the pods' roles need to use it."
  value       = local.kms_key_arn
}

output "dr_bucket" {
  description = "The copy in the DR region, to recover from."
  value       = aws_s3_bucket.dr.id
}

output "dr_region" {
  description = "The DR region."
  value       = data.aws_region.dr.region
}

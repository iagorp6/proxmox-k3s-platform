output "bucket_name" {
  value = aws_s3_bucket.backups.id
}

output "restic_repository" {
  value = "s3:s3.${var.region}.amazonaws.com/${aws_s3_bucket.backups.id}/zomboid"
}

output "restic_access_key_id" {
  value = aws_iam_access_key.restic.id
}

output "restic_secret_access_key" {
  value     = aws_iam_access_key.restic.secret
  sensitive = true
}

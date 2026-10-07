# ---------- Alerta de tamanho do bucket de backup ----------
# O tamanho é o que gera custo no S3; a métrica é diária, então avisa antes do budget.
resource "aws_sns_topic" "alerts" {
  name = "homelab-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.budget_email
}

resource "aws_cloudwatch_metric_alarm" "backup_bucket_size" {
  alarm_name          = "homelab-backup-bucket-size"
  alarm_description   = "Bucket de backup acima de ${var.backup_size_alert_gb} GB"
  namespace           = "AWS/S3"
  metric_name         = "BucketSizeBytes"
  statistic           = "Average"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.backup_size_alert_gb * 1024 * 1024 * 1024
  treat_missing_data  = "ignore"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    BucketName  = aws_s3_bucket.backups.id
    StorageType = "StandardStorage"
  }
}

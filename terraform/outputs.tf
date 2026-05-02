###############################################################################
# Cloud-Thrifty — Outputs
###############################################################################

output "waste_hunter_arn" {
  description = "ARN of the Waste Hunter Lambda"
  value       = aws_lambda_function.waste_hunter.arn
}

output "smart_scheduler_arn" {
  description = "ARN of the Smart Scheduler Lambda"
  value       = aws_lambda_function.smart_scheduler.arn
}

output "cost_anomaly_arn" {
  description = "ARN of the Cost Anomaly Detector Lambda"
  value       = aws_lambda_function.cost_anomaly.arn
}

output "report_bucket_name" {
  description = "S3 bucket where JSON waste reports are stored"
  value       = aws_s3_bucket.reports.bucket
}

output "report_bucket_arn" {
  description = "ARN of the S3 report bucket"
  value       = aws_s3_bucket.reports.arn
}

output "iam_role_arn" {
  description = "IAM role used by all Cloud-Thrifty Lambda functions"
  value       = aws_iam_role.cloud_thrifty.arn
}

output "deploy_summary" {
  description = "Quick-glance deployment summary"
  value = {
    project          = var.project_name
    region           = var.aws_region
    target_regions   = var.target_regions
    dry_run          = var.dry_run
    waste_scan_every = "6 hours"
    scheduler_stop   = "19:00 UTC Mon-Fri"
    scheduler_start  = "08:00 UTC Mon-Fri"
    anomaly_check    = "09:00 UTC daily"
  }
}

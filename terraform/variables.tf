###############################################################################
# Cloud-Thrifty — Input Variables
###############################################################################

variable "project_name" {
  description = "Prefix applied to all resource names"
  type        = string
  default     = "cloud-thrifty"
}

variable "aws_region" {
  description = "Primary AWS region to deploy Cloud-Thrifty infrastructure"
  type        = string
  default     = "us-east-1"
}

variable "target_regions" {
  description = "Comma-separated list of regions to scan for waste"
  type        = string
  default     = "us-east-1,us-west-2,eu-west-1"
}

variable "slack_webhook_url" {
  description = "Slack Incoming Webhook URL for notifications (store in AWS Secrets Manager in prod)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "discord_webhook_url" {
  description = "Discord Webhook URL (optional — uses Slack if empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "aws_account_alias" {
  description = "Human-readable account name shown in Slack alerts"
  type        = string
  default     = "my-aws-account"
}

variable "dry_run" {
  description = "When true, resources are flagged but NOT deleted/stopped"
  type        = bool
  default     = true
}

variable "idle_ebs_days" {
  description = "Days a volume must be unattached before it's flagged as waste"
  type        = number
  default     = 7
}

variable "idle_lb_days" {
  description = "Days a load balancer must have zero traffic before flagged"
  type        = number
  default     = 3
}

variable "scheduler_environments" {
  description = "Comma-separated Environment tag values the Smart Scheduler targets"
  type        = string
  default     = "dev,staging,test"
}

variable "default_instance_hourly_cost" {
  description = "Hourly cost (USD) used to estimate scheduler savings when instance type is unknown"
  type        = number
  default     = 0.096   # t3.medium on-demand
}

variable "anomaly_threshold_pct" {
  description = "Percentage increase in daily spend that triggers a cost anomaly alert"
  type        = number
  default     = 20
}

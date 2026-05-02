###############################################################################
# Cloud-Thrifty — Infrastructure as Code
# Deploys three Lambda functions + IAM roles + CloudWatch Event rules
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment to use remote state (recommended for teams)
  # backend "s3" {
  #   bucket = "my-tfstate-bucket"
  #   key    = "cloud-thrifty/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "cloud-thrifty"
      ManagedBy   = "terraform"
      Environment = "tools"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region"          "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.name
  lambda_runtime = "python3.12"
  lambda_layers  = []    # Add your own layer ARNs (e.g., boto3 updates)
}


###############################################################################
# S3 Bucket — Stores JSON reports for the dashboard
###############################################################################

resource "aws_s3_bucket" "reports" {
  bucket        = "${var.project_name}-reports-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "expire-old-reports"
    status = "Enabled"
    expiration { days = 90 }
  }
}


###############################################################################
# IAM — Lambda Execution Role
###############################################################################

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cloud_thrifty" {
  name               = "${var.project_name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "cloud_thrifty_policy" {
  # CloudWatch Logs
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:*"]
  }

  # EC2 read + stop/start/delete (waste hunter needs delete for non-dry-run)
  statement {
    effect  = "Allow"
    actions = [
      "ec2:DescribeVolumes",
      "ec2:DescribeAddresses",
      "ec2:DescribeInstances",
      "ec2:DescribeTags",
      "ec2:StopInstances",
      "ec2:StartInstances",
      "ec2:DeleteVolume",
      "ec2:ReleaseAddress",
    ]
    resources = ["*"]
  }

  # ELB / ALB
  statement {
    effect  = "Allow"
    actions = [
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeTags",
      "elasticloadbalancing:DeleteLoadBalancer",
    ]
    resources = ["*"]
  }

  # CloudWatch Metrics (for idle LB detection)
  statement {
    effect  = "Allow"
    actions = ["cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }

  # RDS (Smart Scheduler)
  statement {
    effect  = "Allow"
    actions = [
      "rds:DescribeDBInstances",
      "rds:ListTagsForResource",
      "rds:StopDBInstance",
      "rds:StartDBInstance",
    ]
    resources = ["*"]
  }

  # Cost Explorer (anomaly detection)
  statement {
    effect    = "Allow"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }

  # S3 (report writing)
  statement {
    effect  = "Allow"
    actions = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.reports.arn,
      "${aws_s3_bucket.reports.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "cloud_thrifty" {
  name   = "${var.project_name}-policy"
  role   = aws_iam_role.cloud_thrifty.id
  policy = data.aws_iam_policy_document.cloud_thrifty_policy.json
}


###############################################################################
# Lambda Packaging — zips the src/ directory
###############################################################################

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/cloud_thrifty.zip"
}


###############################################################################
# Lambda #1 — Waste Hunter (every 6 hours)
###############################################################################

resource "aws_lambda_function" "waste_hunter" {
  function_name    = "${var.project_name}-waste-hunter"
  role             = aws_iam_role.cloud_thrifty.arn
  runtime          = local.lambda_runtime
  handler          = "waste_hunter.handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 300
  memory_size      = 256
  layers           = local.lambda_layers

  environment {
    variables = {
      TARGET_REGIONS    = var.target_regions
      IDLE_EBS_DAYS     = tostring(var.idle_ebs_days)
      IDLE_LB_DAYS      = tostring(var.idle_lb_days)
      DRY_RUN           = tostring(var.dry_run)
      NOTIFY_SLACK      = "true"
      SLACK_WEBHOOK_URL = var.slack_webhook_url
      REPORT_BUCKET     = aws_s3_bucket.reports.bucket
      AWS_ACCOUNT_ALIAS = var.aws_account_alias
    }
  }
}

resource "aws_cloudwatch_event_rule" "waste_hunter" {
  name                = "${var.project_name}-waste-hunter-schedule"
  description         = "Trigger Cloud-Thrifty Waste Hunter every 6 hours"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "waste_hunter" {
  rule = aws_cloudwatch_event_rule.waste_hunter.name
  arn  = aws_lambda_function.waste_hunter.arn
}

resource "aws_lambda_permission" "waste_hunter" {
  statement_id  = "AllowEventBridgeWasteHunter"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.waste_hunter.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.waste_hunter.arn
}


###############################################################################
# Lambda #2a — Smart Scheduler STOP (7 PM weekdays)
###############################################################################

resource "aws_lambda_function" "smart_scheduler" {
  function_name    = "${var.project_name}-smart-scheduler"
  role             = aws_iam_role.cloud_thrifty.arn
  runtime          = local.lambda_runtime
  handler          = "smart_scheduler.handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 120
  memory_size      = 128
  layers           = local.lambda_layers

  environment {
    variables = {
      TARGET_REGIONS       = var.target_regions
      TARGET_ENVIRONMENTS  = var.scheduler_environments
      DRY_RUN              = tostring(var.dry_run)
      NOTIFY_SLACK         = "true"
      SLACK_WEBHOOK_URL    = var.slack_webhook_url
      DEFAULT_HOURLY_COST  = tostring(var.default_instance_hourly_cost)
      AWS_ACCOUNT_ALIAS    = var.aws_account_alias
    }
  }
}

# STOP rule — 7 PM UTC Mon-Fri
resource "aws_cloudwatch_event_rule" "scheduler_stop" {
  name                = "${var.project_name}-scheduler-stop"
  description         = "Stop dev/staging instances at 7 PM UTC on weekdays"
  schedule_expression = "cron(0 19 ? * MON-FRI *)"
}

resource "aws_cloudwatch_event_target" "scheduler_stop" {
  rule  = aws_cloudwatch_event_rule.scheduler_stop.name
  arn   = aws_lambda_function.smart_scheduler.arn
  input = jsonencode({ action = "stop" })
}

resource "aws_lambda_permission" "scheduler_stop" {
  statement_id  = "AllowEventBridgeStop"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.smart_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scheduler_stop.arn
}

# START rule — 8 AM UTC Mon-Fri
resource "aws_cloudwatch_event_rule" "scheduler_start" {
  name                = "${var.project_name}-scheduler-start"
  description         = "Start dev/staging instances at 8 AM UTC on weekdays"
  schedule_expression = "cron(0 8 ? * MON-FRI *)"
}

resource "aws_cloudwatch_event_target" "scheduler_start" {
  rule  = aws_cloudwatch_event_rule.scheduler_start.name
  arn   = aws_lambda_function.smart_scheduler.arn
  input = jsonencode({ action = "start" })
}

resource "aws_lambda_permission" "scheduler_start" {
  statement_id  = "AllowEventBridgeStart"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.smart_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.scheduler_start.arn
}


###############################################################################
# Lambda #3 — Cost Anomaly Detector (daily at 9 AM UTC)
###############################################################################

resource "aws_lambda_function" "cost_anomaly" {
  function_name    = "${var.project_name}-cost-anomaly"
  role             = aws_iam_role.cloud_thrifty.arn
  runtime          = local.lambda_runtime
  handler          = "notifier.anomaly_handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60
  memory_size      = 128
  layers           = local.lambda_layers

  environment {
    variables = {
      SLACK_WEBHOOK_URL  = var.slack_webhook_url
      ANOMALY_THRESHOLD_PCT = tostring(var.anomaly_threshold_pct)
      AWS_ACCOUNT_ALIAS  = var.aws_account_alias
    }
  }
}

resource "aws_cloudwatch_event_rule" "cost_anomaly" {
  name                = "${var.project_name}-cost-anomaly-daily"
  description         = "Daily cost anomaly check"
  schedule_expression = "cron(0 9 * * ? *)"
}

resource "aws_cloudwatch_event_target" "cost_anomaly" {
  rule = aws_cloudwatch_event_rule.cost_anomaly.name
  arn  = aws_lambda_function.cost_anomaly.arn
}

resource "aws_lambda_permission" "cost_anomaly" {
  statement_id  = "AllowEventBridgeCostAnomaly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_anomaly.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cost_anomaly.arn
}


###############################################################################
# CloudWatch Log Groups (explicit, so Terraform manages retention)
###############################################################################

resource "aws_cloudwatch_log_group" "waste_hunter" {
  name              = "/aws/lambda/${aws_lambda_function.waste_hunter.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "smart_scheduler" {
  name              = "/aws/lambda/${aws_lambda_function.smart_scheduler.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "cost_anomaly" {
  name              = "/aws/lambda/${aws_lambda_function.cost_anomaly.function_name}"
  retention_in_days = 30
}

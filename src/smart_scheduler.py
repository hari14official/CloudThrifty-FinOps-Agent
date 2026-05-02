"""
Cloud-Thrifty | Module 2: Smart Scheduler
Tag-based auto stop/start for Dev & Staging EC2 instances.
Triggered by two CloudWatch Events:
  - cron(0 19 ? * MON-FRI *)  → STOP  at 7 PM UTC on weekdays
  - cron(0  8 ? * MON-FRI *)  → START at 8 AM UTC on weekdays
Savings: ~65%+ on non-prod compute costs.
"""

import boto3
import json
import os
import logging
from datetime import datetime, timezone
from notifier import CloudThriftyNotifier

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─── Config ──────────────────────────────────────────────────────────────────
TARGET_ENVIRONMENTS = os.environ.get("TARGET_ENVIRONMENTS", "dev,staging,test").split(",")
TARGET_REGIONS      = os.environ.get("TARGET_REGIONS", "us-east-1").split(",")
DRY_RUN             = os.environ.get("DRY_RUN", "true").lower() == "true"
NOTIFY_SLACK        = os.environ.get("NOTIFY_SLACK", "true").lower() == "true"

# EC2 hourly cost estimate for savings calculation (override per instance type in env)
DEFAULT_HOURLY_COST = float(os.environ.get("DEFAULT_HOURLY_COST", "0.096"))  # t3.medium


# ──────────────────────────────────────────────────────────────────────────────

def _build_tag_filter() -> list[dict]:
    """Build EC2 filter for Environment tag matching target envs."""
    return [
        {
            "Name":   "tag:Environment",
            "Values": [e.strip() for e in TARGET_ENVIRONMENTS],
        },
        {
            "Name":   "instance-state-name",
            "Values": ["running", "stopped"],  # include stopped so START works
        },
    ]


def _get_instance_name(instance: dict) -> str:
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    return tags.get("Name", instance["InstanceId"])


def _should_skip(instance: dict) -> bool:
    """Return True if instance has an opt-out tag."""
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    return tags.get("scheduler:skip", "").lower() == "true"


def get_tagged_instances(ec2_client) -> list[dict]:
    """Return all instances matching the Environment tag filter."""
    instances = []
    paginator = ec2_client.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=_build_tag_filter()):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                if not _should_skip(inst):
                    instances.append(inst)
    return instances


# ──────────────────────────────────────────────────────────────────────────────
# STOP ACTION
# ──────────────────────────────────────────────────────────────────────────────

def stop_instances(ec2_client, region: str, notifier) -> dict:
    """Stop all running tagged instances. Returns summary dict."""
    instances = [i for i in get_tagged_instances(ec2_client) if i["State"]["Name"] == "running"]

    if not instances:
        logger.info(f"[{region}] No running tagged instances to stop.")
        return {"region": region, "action": "stop", "count": 0, "ids": []}

    ids   = [i["InstanceId"] for i in instances]
    names = [_get_instance_name(i) for i in instances]

    logger.info(f"[{region}] STOP: {ids}")

    if not DRY_RUN:
        ec2_client.stop_instances(InstanceIds=ids)

    # Estimate hours saved: weekday evenings (7 PM → 8 AM = 13h) + full weekends
    # Simplified: 13 hours/weeknight × 5 nights = 65 hours/week saved
    hours_saved_per_week = 13 * 5
    weekly_savings = len(ids) * DEFAULT_HOURLY_COST * hours_saved_per_week

    if notifier:
        notifier.send_scheduler_notification(
            action        = "stopped",
            instance_ids  = ids,
            instance_names= names,
            region        = region,
            weekly_savings= weekly_savings,
        )

    return {
        "region":   region,
        "action":   "stop",
        "count":    len(ids),
        "ids":      ids,
        "dry_run":  DRY_RUN,
        "weekly_savings_usd": round(weekly_savings, 2),
    }


# ──────────────────────────────────────────────────────────────────────────────
# START ACTION
# ──────────────────────────────────────────────────────────────────────────────

def start_instances(ec2_client, region: str, notifier) -> dict:
    """Start all stopped tagged instances."""
    instances = [i for i in get_tagged_instances(ec2_client) if i["State"]["Name"] == "stopped"]

    if not instances:
        logger.info(f"[{region}] No stopped tagged instances to start.")
        return {"region": region, "action": "start", "count": 0, "ids": []}

    ids   = [i["InstanceId"] for i in instances]
    names = [_get_instance_name(i) for i in instances]

    logger.info(f"[{region}] START: {ids}")

    if not DRY_RUN:
        ec2_client.start_instances(InstanceIds=ids)

    if notifier:
        notifier.send_scheduler_notification(
            action         = "started",
            instance_ids   = ids,
            instance_names = names,
            region         = region,
        )

    return {
        "region":  region,
        "action":  "start",
        "count":   len(ids),
        "ids":     ids,
        "dry_run": DRY_RUN,
    }


# ──────────────────────────────────────────────────────────────────────────────
# RDS SUPPORT (bonus: extend scheduler to cover RDS clusters)
# ──────────────────────────────────────────────────────────────────────────────

def _handle_rds(action: str, region: str):
    """Stop/start RDS instances tagged for dev environments."""
    rds = boto3.client("rds", region_name=region)

    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            db_id    = db["DBInstanceIdentifier"]
            tags_raw = rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"]
            tags     = {t["Key"]: t["Value"] for t in tags_raw}

            env = tags.get("Environment", "").lower()
            if env not in TARGET_ENVIRONMENTS:
                continue
            if tags.get("scheduler:skip", "").lower() == "true":
                continue

            state = db["DBInstanceStatus"]
            logger.info(f"[RDS][{region}] {action.upper()} {db_id} (state={state})")

            if DRY_RUN:
                continue

            try:
                if action == "stop" and state == "available":
                    rds.stop_db_instance(DBInstanceIdentifier=db_id)
                elif action == "start" and state == "stopped":
                    rds.start_db_instance(DBInstanceIdentifier=db_id)
            except rds.exceptions.InvalidDBInstanceStateFault as e:
                logger.warning(f"Cannot {action} {db_id}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# LAMBDA HANDLER
# ──────────────────────────────────────────────────────────────────────────────

def handler(event, context):
    """
    Expected event payload:
      { "action": "stop" }   or   { "action": "start" }
    CloudWatch Events rule passes this via the Input field.
    """
    action   = event.get("action", "stop").lower()
    notifier = CloudThriftyNotifier() if NOTIFY_SLACK else None
    results  = []

    for region in TARGET_REGIONS:
        region = region.strip()
        ec2    = boto3.client("ec2", region_name=region)

        if action == "stop":
            results.append(stop_instances(ec2, region, notifier))
        elif action == "start":
            results.append(start_instances(ec2, region, notifier))
        else:
            raise ValueError(f"Unknown action: {action}")

        # Also handle RDS
        _handle_rds(action, region)

    total_stopped = sum(r["count"] for r in results if r["action"] == "stop")
    total_started = sum(r["count"] for r in results if r["action"] == "start")
    total_savings = sum(r.get("weekly_savings_usd", 0) for r in results)

    logger.info(
        f"Scheduler complete | action={action} | "
        f"instances_affected={total_stopped or total_started} | "
        f"est_weekly_savings=${total_savings:.2f}"
    )

    return {
        "statusCode":        200,
        "action":            action,
        "regions_processed": len(TARGET_REGIONS),
        "results":           results,
        "weekly_savings":    round(total_savings, 2),
        "dry_run":           DRY_RUN,
    }

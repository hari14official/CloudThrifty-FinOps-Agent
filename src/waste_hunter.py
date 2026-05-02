"""
Cloud-Thrifty | Module 1: Waste Hunter
Scans for zombie resources: unattached EBS volumes, idle load balancers,
and orphaned Elastic IPs. Runs every 6 hours via CloudWatch Events.
"""

import boto3
import json
import os
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from notifier import CloudThriftyNotifier

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cost estimates (USD/month) — update to match your region's pricing
PRICING = {
    "ebs_gp2_per_gb":  0.10,
    "ebs_gp3_per_gb":  0.08,
    "ebs_io1_per_gb":  0.125,
    "elastic_ip":      0.005,   # per hour when unassociated
    "alb_per_hour":    0.008,
    "nlb_per_hour":    0.006,
}

IDLE_EBS_DAYS    = int(os.environ.get("IDLE_EBS_DAYS", 7))
IDLE_LB_DAYS     = int(os.environ.get("IDLE_LB_DAYS", 3))
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() == "true"
NOTIFY_SLACK     = os.environ.get("NOTIFY_SLACK", "true").lower() == "true"


@dataclass
class WasteItem:
    resource_id:   str
    resource_type: str
    region:        str
    monthly_cost:  float
    idle_days:     int
    details:       dict
    action:        str = "pending_deletion"


# ──────────────────────────────────────────────────────────────────────────────
# EBS VOLUMES
# ──────────────────────────────────────────────────────────────────────────────

def scan_unattached_ebs(ec2_client, region: str) -> list[WasteItem]:
    """Find EBS volumes in 'available' state (not attached to any instance)."""
    waste = []
    paginator = ec2_client.get_paginator("describe_volumes")

    for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
        for vol in page["Volumes"]:
            # Skip volumes explicitly tagged keep:true
            tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
            if tags.get("keep", "").lower() == "true":
                logger.info(f"Skipping kept volume {vol['VolumeId']}")
                continue

            create_time = vol["CreateTime"].replace(tzinfo=timezone.utc)
            idle_days   = (datetime.now(timezone.utc) - create_time).days

            if idle_days < IDLE_EBS_DAYS:
                continue

            size_gb      = vol["Size"]
            vol_type     = vol.get("VolumeType", "gp2")
            price_key    = f"ebs_{vol_type}_per_gb" if f"ebs_{vol_type}_per_gb" in PRICING else "ebs_gp2_per_gb"
            monthly_cost = size_gb * PRICING[price_key]

            waste.append(WasteItem(
                resource_id   = vol["VolumeId"],
                resource_type = "EBS Volume",
                region        = region,
                monthly_cost  = monthly_cost,
                idle_days     = idle_days,
                details       = {
                    "size_gb":      size_gb,
                    "volume_type":  vol_type,
                    "snapshot_id":  vol.get("SnapshotId", "none"),
                    "name":         tags.get("Name", "unnamed"),
                },
            ))

    logger.info(f"[EBS] Found {len(waste)} unattached volumes in {region}")
    return waste


# ──────────────────────────────────────────────────────────────────────────────
# ELASTIC IPs
# ──────────────────────────────────────────────────────────────────────────────

def scan_orphaned_eips(ec2_client, region: str) -> list[WasteItem]:
    """Find Elastic IPs not associated with any running instance or ENI."""
    waste = []
    response = ec2_client.describe_addresses(
        Filters=[{"Name": "domain", "Values": ["vpc"]}]
    )

    for addr in response["Addresses"]:
        # Associated = currently in use
        if addr.get("AssociationId"):
            continue

        tags         = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
        if tags.get("keep", "").lower() == "true":
            continue

        # EIPs don't have a creation timestamp via API — treat all unassociated as idle
        monthly_cost = PRICING["elastic_ip"] * 24 * 30  # ~$3.60/mo per IP

        waste.append(WasteItem(
            resource_id   = addr["AllocationId"],
            resource_type = "Elastic IP",
            region        = region,
            monthly_cost  = monthly_cost,
            idle_days     = -1,   # unknown
            details       = {
                "public_ip":   addr["PublicIp"],
                "name":        tags.get("Name", "unnamed"),
            },
        ))

    logger.info(f"[EIP] Found {len(waste)} orphaned Elastic IPs in {region}")
    return waste


# ──────────────────────────────────────────────────────────────────────────────
# LOAD BALANCERS
# ──────────────────────────────────────────────────────────────────────────────

def _get_lb_request_count(cw_client, lb_arn: str, lb_type: str, days: int) -> float:
    """Return total RequestCount for a load balancer over the last N days."""
    dim_name  = "LoadBalancer"
    metric    = "RequestCount"
    namespace = "AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB"

    # Extract the short ARN suffix that CloudWatch uses as the dimension value
    # e.g. app/my-alb/50dc6c495c0c9188  →  app/my-alb/50dc6c495c0c9188
    lb_dim = "/".join(lb_arn.split("/")[-3:]) if lb_type == "application" else "/".join(lb_arn.split("/")[-3:])

    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0) - __import__("datetime").timedelta(days=days)

    resp = cw_client.get_metric_statistics(
        Namespace  = namespace,
        MetricName = metric,
        Dimensions = [{"Name": dim_name, "Value": lb_dim}],
        StartTime  = start,
        EndTime    = now,
        Period     = 86400,
        Statistics = ["Sum"],
    )
    return sum(dp["Sum"] for dp in resp.get("Datapoints", []))


def scan_idle_load_balancers(elb_client, cw_client, region: str) -> list[WasteItem]:
    """Find ALBs/NLBs with zero requests over the past IDLE_LB_DAYS days."""
    waste    = []
    response = elb_client.describe_load_balancers()

    for lb in response["LoadBalancers"]:
        lb_arn  = lb["LoadBalancerArn"]
        lb_type = lb["Type"]  # application | network | gateway

        if lb_type not in ("application", "network"):
            continue

        tags_resp = elb_client.describe_tags(ResourceArns=[lb_arn])
        tags      = {}
        for td in tags_resp["TagDescriptions"]:
            tags = {t["Key"]: t["Value"] for t in td["Tags"]}

        if tags.get("keep", "").lower() == "true":
            continue

        requests = _get_lb_request_count(cw_client, lb_arn, lb_type, IDLE_LB_DAYS)
        if requests > 0:
            continue

        price_key    = "alb_per_hour" if lb_type == "application" else "nlb_per_hour"
        monthly_cost = PRICING[price_key] * 24 * 30

        waste.append(WasteItem(
            resource_id   = lb_arn.split("/")[-2],   # short name
            resource_type = f"{lb_type.upper()} Load Balancer",
            region        = region,
            monthly_cost  = monthly_cost,
            idle_days     = IDLE_LB_DAYS,
            details       = {
                "arn":        lb_arn,
                "dns_name":   lb["DNSName"],
                "state":      lb["State"]["Code"],
                "name":       lb["LoadBalancerName"],
                "az_count":   len(lb.get("AvailabilityZones", [])),
            },
        ))

    logger.info(f"[LB] Found {len(waste)} idle load balancers in {region}")
    return waste


# ──────────────────────────────────────────────────────────────────────────────
# LAMBDA HANDLER
# ──────────────────────────────────────────────────────────────────────────────

def handler(event, context):
    regions = os.environ.get("TARGET_REGIONS", "us-east-1").split(",")
    notifier = CloudThriftyNotifier() if NOTIFY_SLACK else None
    all_waste: list[WasteItem] = []

    for region in regions:
        region = region.strip()
        logger.info(f"Scanning region: {region}")

        ec2  = boto3.client("ec2",                region_name=region)
        elb  = boto3.client("elbv2",              region_name=region)
        cw   = boto3.client("cloudwatch",         region_name=region)

        all_waste += scan_unattached_ebs(ec2, region)
        all_waste += scan_orphaned_eips(ec2, region)
        all_waste += scan_idle_load_balancers(elb, cw, region)

    total_savings = sum(w.monthly_cost for w in all_waste)
    logger.info(f"Total waste found: {len(all_waste)} items | Est. savings: ${total_savings:.2f}/mo")

    # Notify & optionally clean up
    for item in all_waste:
        if notifier:
            notifier.send_waste_alert(item)

        if not DRY_RUN:
            _delete_resource(item)

    # Persist findings to S3 for the dashboard
    _write_report(all_waste, total_savings)

    return {
        "statusCode":    200,
        "items_found":   len(all_waste),
        "monthly_waste": round(total_savings, 2),
        "dry_run":       DRY_RUN,
    }


def _delete_resource(item: WasteItem):
    """Actually delete/release a confirmed zombie resource."""
    ec2 = boto3.client("ec2", region_name=item.region)

    if item.resource_type == "EBS Volume":
        logger.info(f"Deleting EBS volume {item.resource_id}")
        ec2.delete_volume(VolumeId=item.resource_id)

    elif item.resource_type == "Elastic IP":
        logger.info(f"Releasing Elastic IP {item.resource_id}")
        ec2.release_address(AllocationId=item.resource_id)

    elif "Load Balancer" in item.resource_type:
        elb = boto3.client("elbv2", region_name=item.region)
        logger.info(f"Deleting load balancer {item.details['name']}")
        elb.delete_load_balancer(LoadBalancerArn=item.details["arn"])


def _write_report(waste: list[WasteItem], total: float):
    """Write a JSON report to S3 for dashboard consumption."""
    bucket = os.environ.get("REPORT_BUCKET")
    if not bucket:
        return

    s3 = boto3.client("s3")
    report = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "total_monthly": round(total, 2),
        "item_count":    len(waste),
        "items":         [asdict(w) for w in waste],
    }
    s3.put_object(
        Bucket      = bucket,
        Key         = f"reports/waste-hunter/{datetime.now().strftime('%Y/%m/%d/%H')}.json",
        Body        = json.dumps(report, default=str),
        ContentType = "application/json",
    )

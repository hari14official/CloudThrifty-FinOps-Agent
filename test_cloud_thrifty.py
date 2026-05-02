"""
Cloud-Thrifty — Unit Tests
Uses moto to mock AWS services. No real AWS credentials required.
Run: pytest tests/ -v
"""

import json
import os
import pytest
import boto3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# ── Set dummy AWS credentials before any boto3 import ────────────────────────
os.environ.setdefault("AWS_DEFAULT_REGION",        "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",         "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY",     "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN",        "testing")
os.environ.setdefault("AWS_SESSION_TOKEN",         "testing")
os.environ.setdefault("SLACK_WEBHOOK_URL",         "")   # disabled in tests
os.environ.setdefault("DRY_RUN",                   "true")
os.environ.setdefault("NOTIFY_SLACK",              "false")
os.environ.setdefault("TARGET_REGIONS",            "us-east-1")

from moto import mock_aws

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))


###############################################################################
# WASTE HUNTER TESTS
###############################################################################

class TestWasteHunter:

    @mock_aws
    def test_scan_unattached_ebs_finds_old_volumes(self):
        """Volumes unattached for >= IDLE_EBS_DAYS should be flagged."""
        os.environ["IDLE_EBS_DAYS"] = "7"
        from waste_hunter import scan_unattached_ebs

        ec2 = boto3.client("ec2", region_name="us-east-1")

        # Create an unattached volume (moto doesn't honour CreateTime easily,
        # so we test the available-state filter logic here)
        vol = ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=100,
            VolumeType="gp2",
        )
        vol_id = vol["VolumeId"]

        # moto returns volumes in 'available' state by default
        result = scan_unattached_ebs(ec2, "us-east-1")

        # At minimum the volume should appear in the scan
        ids = [w.resource_id for w in result]
        assert vol_id in ids

    @mock_aws
    def test_scan_unattached_ebs_skips_kept_volumes(self):
        """Volumes tagged keep:true must be ignored."""
        os.environ["IDLE_EBS_DAYS"] = "0"   # flag immediately
        from waste_hunter import scan_unattached_ebs

        ec2 = boto3.client("ec2", region_name="us-east-1")
        vol = ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=50,
            VolumeType="gp3",
            TagSpecifications=[{
                "ResourceType": "volume",
                "Tags": [{"Key": "keep", "Value": "true"}],
            }],
        )

        result = scan_unattached_ebs(ec2, "us-east-1")
        ids    = [w.resource_id for w in result]
        assert vol["VolumeId"] not in ids

    @mock_aws
    def test_scan_orphaned_eips(self):
        """Unassociated Elastic IPs should be returned as waste."""
        from waste_hunter import scan_orphaned_eips

        ec2  = boto3.client("ec2", region_name="us-east-1")
        addr = ec2.allocate_address(Domain="vpc")

        result = scan_orphaned_eips(ec2, "us-east-1")
        ids    = [w.resource_id for w in result]
        assert addr["AllocationId"] in ids

    @mock_aws
    def test_eip_monthly_cost_is_positive(self):
        """Each orphaned EIP should have a non-zero monthly_cost estimate."""
        from waste_hunter import scan_orphaned_eips

        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.allocate_address(Domain="vpc")

        result = scan_orphaned_eips(ec2, "us-east-1")
        for item in result:
            assert item.monthly_cost > 0

    @mock_aws
    def test_ebs_cost_calculation(self):
        """EBS cost should be size_gb × price_per_gb."""
        os.environ["IDLE_EBS_DAYS"] = "0"
        from waste_hunter import scan_unattached_ebs, PRICING

        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.create_volume(AvailabilityZone="us-east-1a", Size=200, VolumeType="gp2")

        result = scan_unattached_ebs(ec2, "us-east-1")
        assert any(abs(w.monthly_cost - 200 * PRICING["ebs_gp2_per_gb"]) < 0.01 for w in result)


###############################################################################
# SMART SCHEDULER TESTS
###############################################################################

class TestSmartScheduler:

    @mock_aws
    def test_stop_running_tagged_instances(self):
        """Tagged running instances should be stopped."""
        from smart_scheduler import stop_instances

        ec2 = boto3.client("ec2", region_name="us-east-1")

        # Launch a dummy instance with the dev tag
        ec2.run_instances(
            ImageId      = "ami-12345678",
            MinCount     = 1,
            MaxCount     = 1,
            InstanceType = "t3.medium",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Environment", "Value": "dev"},
                    {"Key": "Name",        "Value": "test-server"},
                ],
            }],
        )

        result = stop_instances(ec2, "us-east-1", notifier=None)
        assert result["count"] == 1
        assert result["action"] == "stop"
        assert result["dry_run"] is True  # DRY_RUN=true in env

    @mock_aws
    def test_start_stopped_tagged_instances(self):
        """Tagged stopped instances should be started."""
        from smart_scheduler import start_instances

        ec2 = boto3.client("ec2", region_name="us-east-1")

        # moto instances are running by default; stop first
        resp = ec2.run_instances(
            ImageId      = "ami-12345678",
            MinCount     = 1,
            MaxCount     = 1,
            InstanceType = "t3.medium",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [{"Key": "Environment", "Value": "staging"}],
            }],
        )
        instance_id = resp["Instances"][0]["InstanceId"]
        ec2.stop_instances(InstanceIds=[instance_id])

        result = start_instances(ec2, "us-east-1", notifier=None)
        assert result["count"] == 1
        assert result["action"] == "start"

    @mock_aws
    def test_skip_tagged_instances_excluded(self):
        """Instances with scheduler:skip=true must not be touched."""
        from smart_scheduler import stop_instances

        ec2 = boto3.client("ec2", region_name="us-east-1")
        ec2.run_instances(
            ImageId      = "ami-12345678",
            MinCount     = 1,
            MaxCount     = 1,
            InstanceType = "t3.medium",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Environment",   "Value": "dev"},
                    {"Key": "scheduler:skip","Value": "true"},
                ],
            }],
        )

        result = stop_instances(ec2, "us-east-1", notifier=None)
        assert result["count"] == 0

    @mock_aws
    def test_no_instances_returns_zero(self):
        """Scheduler should handle regions with no tagged instances gracefully."""
        from smart_scheduler import stop_instances
        ec2    = boto3.client("ec2", region_name="us-east-1")
        result = stop_instances(ec2, "us-east-1", notifier=None)
        assert result["count"] == 0


###############################################################################
# NOTIFIER TESTS
###############################################################################

class TestNotifier:

    def test_notifier_builds_waste_alert_payload(self):
        """_slack_waste_alert should produce a valid Slack Block Kit dict."""
        from notifier import _slack_waste_alert
        from waste_hunter import WasteItem

        item = WasteItem(
            resource_id   = "vol-0abc123",
            resource_type = "EBS Volume",
            region        = "us-east-1",
            monthly_cost  = 25.50,
            idle_days     = 14,
            details       = {"size_gb": 255, "volume_type": "gp2", "name": "db-backup"},
        )
        payload = _slack_waste_alert(item)

        assert "blocks"   in payload
        assert len(payload["blocks"]) > 0
        assert any("vol-0abc123" in str(b) for b in payload["blocks"])

    def test_notifier_builds_anomaly_alert(self):
        """_slack_cost_anomaly_alert should include the percentage increase."""
        from notifier import _slack_cost_anomaly_alert

        payload  = _slack_cost_anomaly_alert(
            today_spend    = 240.0,
            yesterday_spend= 180.0,
            pct_increase   = 33.3,
            top_services   = [{"service": "Amazon EC2", "amount": 120.0}],
        )
        text_combined = json.dumps(payload)
        assert "33.3" in text_combined
        assert "Amazon EC2" in text_combined

    def test_notifier_skips_posting_when_no_webhook(self):
        """CloudThriftyNotifier should not crash if no webhook URL is set."""
        os.environ["SLACK_WEBHOOK_URL"]   = ""
        os.environ["DISCORD_WEBHOOK_URL"] = ""
        from notifier import CloudThriftyNotifier

        n = CloudThriftyNotifier()
        # Should complete without raising
        n.send({"text": "test"})

    @mock_aws
    def test_anomaly_handler_no_baseline(self):
        """anomaly_handler should handle $0 yesterday gracefully."""
        from notifier import anomaly_handler

        # moto's CE returns empty results → yesterday_cost == 0
        result = anomaly_handler({}, None)
        # Either no baseline or no anomaly — both valid outcomes
        assert result["statusCode"] == 200


###############################################################################
# INTEGRATION — Lambda handler smoke tests
###############################################################################

class TestLambdaHandlers:

    @mock_aws
    def test_waste_hunter_handler_returns_200(self):
        """waste_hunter.handler should complete and return statusCode 200."""
        os.environ["REPORT_BUCKET"] = ""   # disable S3 write
        from waste_hunter import handler

        result = handler({}, None)
        assert result["statusCode"]  == 200
        assert "items_found"         in result
        assert "monthly_waste"       in result

    @mock_aws
    def test_smart_scheduler_handler_stop(self):
        """smart_scheduler.handler with action=stop should return 200."""
        from smart_scheduler import handler

        result = handler({"action": "stop"}, None)
        assert result["statusCode"] == 200
        assert result["action"]     == "stop"

    @mock_aws
    def test_smart_scheduler_handler_start(self):
        """smart_scheduler.handler with action=start should return 200."""
        from smart_scheduler import handler

        result = handler({"action": "start"}, None)
        assert result["statusCode"] == 200
        assert result["action"]     == "start"

    @mock_aws
    def test_smart_scheduler_invalid_action_raises(self):
        """Unknown action should raise ValueError."""
        from smart_scheduler import handler

        with pytest.raises(ValueError):
            handler({"action": "nuke"}, None)

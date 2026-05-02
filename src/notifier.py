"""
Cloud-Thrifty | Module 3: Real-Time Notifier
Handles:
  - Slack/Discord webhook notifications for waste alerts
  - Scheduler start/stop notifications
  - Cost anomaly detection (>20% daily spend spike)
"""

import boto3
import json
import os
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK_URL", "")
ALERT_CHANNEL      = os.environ.get("ALERT_CHANNEL", "#devops-alerts")
ANOMALY_THRESHOLD  = float(os.environ.get("ANOMALY_THRESHOLD_PCT", "20"))  # %
AWS_ACCOUNT_ALIAS  = os.environ.get("AWS_ACCOUNT_ALIAS", "my-aws-account")


# ──────────────────────────────────────────────────────────────────────────────
# SLACK MESSAGE BUILDERS
# ──────────────────────────────────────────────────────────────────────────────

def _slack_waste_alert(item) -> dict:
    """Build a rich Slack Block Kit message for a zombie resource."""
    idle_text = f"{item.idle_days} days" if item.idle_days >= 0 else "unknown duration"
    return {
        "username": "Cloud-Thrifty 🔍",
        "channel":  ALERT_CHANNEL,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⚠️ Zombie Resource Detected"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Resource Type:*\n{item.resource_type}"},
                    {"type": "mrkdwn", "text": f"*Resource ID:*\n`{item.resource_id}`"},
                    {"type": "mrkdwn", "text": f"*Region:*\n{item.region}"},
                    {"type": "mrkdwn", "text": f"*Idle For:*\n{idle_text}"},
                    {"type": "mrkdwn", "text": f"*Est. Monthly Waste:*\n*${item.monthly_cost:.2f}*"},
                    {"type": "mrkdwn", "text": f"*Account:*\n{AWS_ACCOUNT_ALIAS}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🗑️ This resource will be *deleted in 24 hours* unless you add the tag "
                        f"`keep:true` to `{item.resource_id}`."
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Cloud-Thrifty · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                    }
                ],
            },
        ],
    }


def _slack_scheduler_notification(
    action: str,
    instance_ids: list[str],
    instance_names: list[str],
    region: str,
    weekly_savings: float = 0,
) -> dict:
    """Build a Slack message for scheduled start/stop events."""
    emoji  = "🔴" if action == "stopped" else "🟢"
    action_label = "stopped" if action == "stopped" else "started"

    instance_list = "\n".join(
        f"• `{iid}` ({name})" for iid, name in zip(instance_ids, instance_names)
    )
    savings_line = (
        f"\n💰 *Est. weekly savings:* ${weekly_savings:.2f}"
        if action == "stopped" and weekly_savings > 0
        else ""
    )

    return {
        "username": "Cloud-Thrifty ⏰",
        "channel":  ALERT_CHANNEL,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{emoji} *Smart Scheduler:* {len(instance_ids)} Dev/Staging instance(s) "
                        f"*{action_label}* in `{region}`{savings_line}"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": instance_list or "_No instances_"},
            },
        ],
    }


def _slack_cost_anomaly_alert(
    today_spend: float,
    yesterday_spend: float,
    pct_increase: float,
    top_services: list[dict],
) -> dict:
    """Build a high-priority Slack alert for cost spikes."""
    service_lines = "\n".join(
        f"• *{s['service']}:* ${s['amount']:.2f}" for s in top_services[:5]
    )
    return {
        "username": "Cloud-Thrifty 🚨",
        "channel":  ALERT_CHANNEL,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 Cost Anomaly Detected!",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Yesterday's Spend:*\n${yesterday_spend:.2f}"},
                    {"type": "mrkdwn", "text": f"*Today's Spend:*\n${today_spend:.2f}"},
                    {"type": "mrkdwn", "text": f"*Increase:*\n⬆️ {pct_increase:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Account:*\n{AWS_ACCOUNT_ALIAS}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Top spending services today:*\n{service_lines}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "👉 Check the <https://console.aws.amazon.com/cost-management/home|AWS Cost Explorer> for details.",
                },
            },
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# NOTIFIER CLASS
# ──────────────────────────────────────────────────────────────────────────────

class CloudThriftyNotifier:
    def __init__(self):
        self.slack_url   = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.discord_url = DISCORD_WEBHOOK

        if not self.slack_url and not self.discord_url:
            logger.warning("No webhook URL configured. Notifications disabled.")

    def _post_webhook(self, url: str, payload: dict):
        """POST a JSON payload to a webhook URL."""
        if url.endswith("/slack"):
            url = url[:-6]

        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data    = data,
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
            method  = "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Webhook response: {resp.status}")
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode()
            except Exception:
                error_body = "Could not read error body"
            logger.error(f"Webhook HTTP error {e.code}: {error_body}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")

    def _to_discord(self, slack_payload: dict) -> dict:
        """Convert a Slack Block Kit payload to a Discord embed (best-effort)."""
        text_parts = []
        for block in slack_payload.get("blocks", []):
            if block.get("type") == "section":
                t = block.get("text", {})
                if t.get("text"):
                    text_parts.append(t["text"])
                for field in block.get("fields", []):
                    text_parts.append(field.get("text", ""))
            elif block.get("type") == "header":
                text_parts.insert(0, f"**{block['text']['text']}**")

        return {"content": "\n".join(text_parts)[:2000]}

    def send(self, payload: dict):
        if self.slack_url:
            discord_payload = self._to_discord(payload)
            self._post_webhook(self.slack_url, discord_payload)
        if self.discord_url:
            self._post_webhook(self.discord_url, self._to_discord(payload))

    def send_waste_alert(self, item):
        self.send(_slack_waste_alert(item))

    def send_scheduler_notification(
        self,
        action: str,
        instance_ids: list,
        instance_names: list,
        region: str,
        weekly_savings: float = 0,
    ):
        self.send(_slack_scheduler_notification(
            action, instance_ids, instance_names, region, weekly_savings
        ))

    def send_cost_anomaly_alert(self, today: float, yesterday: float, pct: float, services: list):
        self.send(_slack_cost_anomaly_alert(today, yesterday, pct, services))


# ──────────────────────────────────────────────────────────────────────────────
# COST ANOMALY DETECTION (runs as its own Lambda handler)
# ──────────────────────────────────────────────────────────────────────────────

def get_daily_costs(ce_client, date_str: str) -> tuple[float, list[dict]]:
    """
    Return (total_cost, top_services) for a given date.
    date_str format: 'YYYY-MM-DD'
    """
    next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    resp = ce_client.get_cost_and_usage(
        TimePeriod = {"Start": date_str, "End": next_day},
        Granularity= "DAILY",
        Metrics    = ["UnblendedCost"],
        GroupBy    = [{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    total    = 0.0
    services = []

    for result in resp.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            service = group["Keys"][0]
            amount  = float(group["Metrics"]["UnblendedCost"]["Amount"])
            total  += amount
            if amount > 0.01:
                services.append({"service": service, "amount": amount})

    services.sort(key=lambda x: x["amount"], reverse=True)
    return round(total, 4), services


def anomaly_handler(event, context):
    """Lambda handler for cost anomaly detection. Run daily."""
    ce       = boto3.client("ce", region_name="us-east-1")
    notifier = CloudThriftyNotifier()

    today_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    today_cost,     today_services     = get_daily_costs(ce, today_str)
    yesterday_cost, _                  = get_daily_costs(ce, yesterday_str)

    if yesterday_cost == 0:
        logger.warning("Yesterday's cost is $0, skipping anomaly check.")
        return {"statusCode": 200, "message": "no baseline"}

    pct_change = ((today_cost - yesterday_cost) / yesterday_cost) * 100

    logger.info(
        f"Cost check | yesterday=${yesterday_cost:.2f} today=${today_cost:.2f} "
        f"change={pct_change:.1f}%"
    )

    if pct_change > ANOMALY_THRESHOLD:
        logger.warning(f"ANOMALY: {pct_change:.1f}% increase detected!")
        notifier.send_cost_anomaly_alert(today_cost, yesterday_cost, pct_change, today_services)
        return {
            "statusCode":    200,
            "anomaly":       True,
            "pct_increase":  round(pct_change, 2),
            "today_cost":    today_cost,
            "yesterday_cost":yesterday_cost,
        }

    return {
        "statusCode":   200,
        "anomaly":      False,
        "pct_change":   round(pct_change, 2),
    }

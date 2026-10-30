#!/usr/bin/env python3
"""
Launch91 GA4 Impact & Change Intelligence Report -> Slack
Uses google-analytics-mcp (ga4_mcp) and slack_sdk.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project directory
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

os.environ.setdefault("GA4_PROPERTY_ID", "539310486")

from ga4_mcp import coordinator
from ga4_mcp.tools.reporting import get_ga4_data
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def resolve_slack_credentials() -> tuple[str, str]:
    """Resolve Slack token and channel from .env or ~/.slack/credentials.json."""
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel = os.getenv("SLACK_CHANNEL_ID", "").strip() or "C0CDFCJ6G93"  # default #l91-analytics

    if not token or token.startswith("xoxb-your-"):
        slack_creds_path = Path.home() / ".slack" / "credentials.json"
        if slack_creds_path.exists():
            try:
                data = json.loads(slack_creds_path.read_text(encoding="utf-8"))
                for team_data in data.values():
                    if isinstance(team_data, dict) and team_data.get("token"):
                        token = team_data["token"]
                        break
            except Exception:
                pass
    return token, channel


def pct_change(curr: float, prev: float) -> str:
    if prev == 0:
        return "🟢 ↑ +100% (new)" if curr > 0 else "⚪ → 0.0%"
    diff = ((curr - prev) / prev) * 100.0
    sign = "+" if diff > 0 else ""
    arrow = "🟢 ↑" if diff > 5 else ("🔴 ↓" if diff < -5 else "⚪ →")
    return f"{arrow} {sign}{diff:.1f}%"


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s}s"


async def fetch_period_data(start_date: str, end_date: str):
    """Fetch core metrics, pages, channels, and device split for a given date range."""
    overview = await get_ga4_data(
        dimensions=["date"],
        metrics=["totalUsers", "newUsers", "sessions", "averageSessionDuration", "screenPageViews"],
        date_range_start=start_date,
        date_range_end=end_date,
        intent="overview metrics and daily trend",
    )
    pages = await get_ga4_data(
        dimensions=["pagePath", "pageTitle"],
        metrics=["screenPageViews", "totalUsers", "averageSessionDuration"],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=15,
        intent="trending pages",
    )
    channels = await get_ga4_data(
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "totalUsers", "newUsers", "averageSessionDuration"],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=10,
        intent="top acquisition channels",
    )
    devices = await get_ga4_data(
        dimensions=["deviceCategory"],
        metrics=["totalUsers", "sessions"],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=10,
        intent="traffic split by device",
    )
    return {
        "overview": overview,
        "pages": pages,
        "channels": channels,
        "devices": devices,
    }


def extract_totals(report: dict) -> dict:
    """Extract aggregated totals from a ga4_mcp report payload."""
    if "totals" in report and report["totals"]:
        return {k: float(v) for k, v in report["totals"].items()}
    rows = report.get("data", [])
    if not rows:
        return {
            "totalUsers": 0.0,
            "newUsers": 0.0,
            "sessions": 0.0,
            "averageSessionDuration": 0.0,
            "screenPageViews": 0.0,
        }
    if len(rows) == 1:
        r = rows[0]
        return {
            "totalUsers": float(r.get("totalUsers", 0)),
            "newUsers": float(r.get("newUsers", 0)),
            "sessions": float(r.get("sessions", 0)),
            "averageSessionDuration": float(r.get("averageSessionDuration", 0)),
            "screenPageViews": float(r.get("screenPageViews", 0)),
        }
    tot_sessions = sum(float(r.get("sessions", 0)) for r in rows)
    weighted_dur = sum(float(r.get("averageSessionDuration", 0)) * float(r.get("sessions", 0)) for r in rows)
    return {
        "totalUsers": sum(float(r.get("totalUsers", 0)) for r in rows),
        "newUsers": sum(float(r.get("newUsers", 0)) for r in rows),
        "sessions": tot_sessions,
        "averageSessionDuration": (weighted_dur / tot_sessions) if tot_sessions else 0.0,
        "screenPageViews": sum(float(r.get("screenPageViews", 0)) for r in rows),
    }


def detect_anomalies(daily_rows: list[dict]) -> list[str]:
    """Detect daily spikes or drops (>= 1.8 std dev from mean) across recent days."""
    if len(daily_rows) < 4:
        return ["Insufficient daily history for statistical anomaly detection."]

    sorted_rows = sorted(daily_rows, key=lambda r: r.get("date", ""))
    anomalies = []
    for metric, label in [("totalUsers", "Total Users"), ("newUsers", "New Users"), ("screenPageViews", "Page Views")]:
        vals = [float(r.get(metric, 0)) for r in sorted_rows]
        mean = sum(vals) / len(vals)
        variance = sum((x - mean) ** 2 for x in vals) / len(vals)
        std = math.sqrt(variance)
        if std == 0:
            continue
        for r in sorted_rows[-3:]:
            val = float(r.get(metric, 0))
            z = (val - mean) / std
            dt = r.get("date", "")
            dt_fmt = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}" if len(dt) == 8 else dt
            if z >= 1.8:
                anomalies.append(f"🚀 *Spike* on `{dt_fmt}`: *{label}* reached *{int(val):,}* ({z:+.1f}σ vs {int(mean):,} baseline)")
            elif z <= -1.8:
                anomalies.append(f"⚠️ *Drop* on `{dt_fmt}`: *{label}* fell to *{int(val):,}* ({z:+.1f}σ vs {int(mean):,} baseline)")

    if not anomalies:
        return ["✅ No major statistical anomalies (±1.8σ) detected in recent daily traffic."]
    return anomalies


def build_report_text(mode: str, curr_data: dict, prev_data: dict, baseline_daily: dict) -> str:
    title = "📊 *Launch91 — Daily GA4 Impact Intelligence*" if mode == "daily" else "📈 *Launch91 — Weekly GA4 Impact & Change Intelligence*"
    period_label = "Yesterday vs Prior Day" if mode == "daily" else "Last 7 Days vs Previous 7 Days (WoW)"

    curr_tot = extract_totals(curr_data["overview"])
    prev_tot = extract_totals(prev_data["overview"])

    c_users = curr_tot["totalUsers"]
    p_users = prev_tot["totalUsers"]
    c_new = curr_tot["newUsers"]
    p_new = prev_tot["newUsers"]
    c_dur = curr_tot["averageSessionDuration"]
    p_dur = prev_tot["averageSessionDuration"]
    c_views = curr_tot["screenPageViews"]
    p_views = prev_tot["screenPageViews"]

    lines = [
        f"{title}",
        f"_Property ID: {os.getenv('GA4_PROPERTY_ID')} | Comparison: {period_label}_",
        "",
        "*1. Core Impact Metrics*",
        f"• *Total Users:* *{int(c_users):,}* (prev: {int(p_users):,}) — {pct_change(c_users, p_users)}",
        f"• *New Users:* *{int(c_new):,}* (prev: {int(p_new):,}) — {pct_change(c_new, p_new)}",
        f"• *Avg Session Duration:* *{fmt_duration(c_dur)}* (prev: {fmt_duration(p_dur)}) — {pct_change(c_dur, p_dur)}",
        f"• *Page Views:* *{int(c_views):,}* (prev: {int(p_views):,}) — {pct_change(c_views, p_views)}",
        "",
    ]

    ret_users = max(0.0, c_users - c_new)
    new_pct = (c_new / c_users * 100.0) if c_users else 0.0
    ret_pct = (ret_users / c_users * 100.0) if c_users else 0.0
    lines.append("*2. Traffic Split*")
    lines.append(f"• *User Type:* New `{new_pct:.1f}%` ({int(c_new):,}) | Returning `{ret_pct:.1f}%` ({int(ret_users):,})")

    dev_rows = curr_data["devices"].get("data", [])
    if dev_rows:
        dev_parts = []
        tot_dev_u = sum(float(r.get("totalUsers", 0)) for r in dev_rows) or 1.0
        for r in dev_rows[:4]:
            d_name = r.get("deviceCategory", "unknown").capitalize()
            d_u = float(r.get("totalUsers", 0))
            dev_parts.append(f"{d_name} `{d_u / tot_dev_u * 100:.1f}%` ({int(d_u):,})")
        lines.append(f"• *Devices:* {' | '.join(dev_parts)}")
    lines.append("")

    lines.append("*3. Top Channels (Acquisition & Shifts)*")
    prev_ch_map = {
        r.get("sessionDefaultChannelGroup", ""): float(r.get("totalUsers", 0))
        for r in prev_data["channels"].get("data", [])
    }
    ch_rows = curr_data["channels"].get("data", [])
    if ch_rows:
        for r in ch_rows[:5]:
            ch = r.get("sessionDefaultChannelGroup", "(other)")
            u = float(r.get("totalUsers", 0))
            nu = float(r.get("newUsers", 0))
            dur = float(r.get("averageSessionDuration", 0))
            pu = prev_ch_map.get(ch, 0.0)
            lines.append(f"• *{ch}:* *{int(u):,}* users ({int(nu):,} new, avg {fmt_duration(dur)}) — {pct_change(u, pu)}")
    else:
        lines.append("• _No channel data for period._")
    lines.append("")

    lines.append("*4. Trending Pages*")
    prev_pg_map = {
        r.get("pagePath", ""): float(r.get("screenPageViews", 0))
        for r in prev_data["pages"].get("data", [])
    }
    pg_rows = curr_data["pages"].get("data", [])
    if pg_rows:
        for r in pg_rows[:5]:
            path = r.get("pagePath", "/")
            views = float(r.get("screenPageViews", 0))
            u = float(r.get("totalUsers", 0))
            p_views_pg = prev_pg_map.get(path, 0.0)
            lines.append(f"• `{path}` — *{int(views):,}* views ({int(u):,} users) — {pct_change(views, p_views_pg)}")
    else:
        lines.append("• _No page data for period._")
    lines.append("")

    lines.append("*5. Anomalies & Spikes (14-Day Baseline)*")
    anomaly_lines = detect_anomalies(baseline_daily.get("data", []))
    for a in anomaly_lines:
        lines.append(f"• {a}")

    return "\n".join(lines)


async def run_report(mode: str, dry_run: bool = False):
    ok, cat, detail = coordinator.reinitialize()
    if not ok:
        print(f"[GA4 SETUP REQUIRED] ({cat}): {detail}", file=sys.stderr)
        if coordinator.SERVER_INIT_ERROR:
            print(coordinator.SERVER_INIT_ERROR, file=sys.stderr)
        sys.exit(2)

    if mode == "daily":
        curr = await fetch_period_data("yesterday", "yesterday")
        prev = await fetch_period_data("2daysAgo", "2daysAgo")
    else:
        curr = await fetch_period_data("7daysAgo", "yesterday")
        prev = await fetch_period_data("14daysAgo", "8daysAgo")

    baseline_daily = await get_ga4_data(
        dimensions=["date"],
        metrics=["totalUsers", "newUsers", "screenPageViews", "averageSessionDuration"],
        date_range_start="14daysAgo",
        date_range_end="yesterday",
        intent="14-day daily anomaly detection baseline",
    )

    if "error" in curr["overview"]:
        print(f"[GA4 ERROR] {curr['overview']['error']}", file=sys.stderr)
        sys.exit(2)

    report_text = build_report_text(mode, curr, prev, baseline_daily)
    print(report_text)

    if dry_run:
        print("\n[DRY RUN] Skipping Slack send.")
        return

    slack_token, slack_channel = resolve_slack_credentials()
    if not slack_token or not slack_channel:
        print("\n[SLACK SETUP REQUIRED] Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in .env or ~/.slack/credentials.json.", file=sys.stderr)
        sys.exit(3)

    client = WebClient(token=slack_token)
    try:
        resp = client.chat_postMessage(
            channel=slack_channel,
            text=report_text,
            mrkdwn=True,
        )
        print(f"\n[SUCCESS] Sent to Slack channel {slack_channel} (ts: {resp.get('ts')})")
    except SlackApiError as e:
        print(f"\n[SLACK API ERROR] {e.response['error']}", file=sys.stderr)
        sys.exit(4)


def main():
    parser = argparse.ArgumentParser(description="Launch91 GA4 Impact & Change Intelligence Report")
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily", help="Report mode: daily or weekly")
    parser.add_argument("--dry-run", action="store_true", help="Print report without sending to Slack")
    args = parser.parse_args()
    asyncio.run(run_report(args.mode, args.dry_run))


if __name__ == "__main__":
    main()

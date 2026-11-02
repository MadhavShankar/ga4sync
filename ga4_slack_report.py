#!/usr/bin/env python3
"""
Launch91 GA4 Impact & Change Intelligence Report -> Slack
Uses google-analytics-mcp (ga4_mcp) and slack_sdk.
Includes an Analyst Briefing Engine that explains what changed, why it happened,
and concrete improvements for the founder.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
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


def pct_diff_val(curr: float, prev: float) -> float:
    if prev == 0:
        return 100.0 if curr > 0 else 0.0
    return ((curr - prev) / prev) * 100.0


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s}s" if m > 0 else f"{s}s"


def clean_section_name(path: str) -> str:
    """Extract editorial section/category from pagePath."""
    if not path or path == "/":
        return "Homepage"
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "Homepage"
    first = parts[0].lower()
    mapping = {
        "startups": "Startups",
        "deep-tech-research": "Deep Tech & Research",
        "funding": "Funding",
        "technology": "Technology",
        "policy": "Policy",
        "market-insights": "Market Insights",
    }
    if first in mapping:
        return mapping[first]
    if len(parts) == 1:
        return "Static / Landing Pages"
    return first.replace("-", " ").title()


def is_article_path(path: str) -> bool:
    """Return True if path looks like an individual story/article slug."""
    if not path or path == "/":
        return False
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return False
    non_article_prefixes = {"category", "tag", "author", "page", "search", "wp-admin", "wp-content", "feed"}
    return parts[0].lower() not in non_article_prefixes


async def fetch_period_data(start_date: str, end_date: str):
    """Fetch core metrics, pages, channels, and device split for a given date range."""
    overview = await get_ga4_data(
        dimensions=["date"],
        metrics=[
            "totalUsers",
            "newUsers",
            "sessions",
            "averageSessionDuration",
            "engagementRate",
            "screenPageViews",
        ],
        date_range_start=start_date,
        date_range_end=end_date,
        intent="overview metrics and daily trend",
    )
    pages = await get_ga4_data(
        dimensions=["pagePath", "pageTitle"],
        metrics=[
            "screenPageViews",
            "totalUsers",
            "sessions",
            "averageSessionDuration",
            "engagementRate",
        ],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=25,
        intent="trending pages and content engagement",
    )
    channels = await get_ga4_data(
        dimensions=["sessionDefaultChannelGroup"],
        metrics=[
            "sessions",
            "totalUsers",
            "newUsers",
            "averageSessionDuration",
            "engagementRate",
            "screenPageViews",
        ],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=10,
        intent="top acquisition channels and quality",
    )
    devices = await get_ga4_data(
        dimensions=["deviceCategory"],
        metrics=[
            "totalUsers",
            "sessions",
            "averageSessionDuration",
            "engagementRate",
            "screenPageViews",
        ],
        date_range_start=start_date,
        date_range_end=end_date,
        limit=10,
        intent="traffic and engagement split by device",
    )
    return {
        "overview": overview,
        "pages": pages,
        "channels": channels,
        "devices": devices,
    }


def extract_totals(report: dict) -> dict:
    """Extract aggregated totals from a ga4_mcp report payload."""
    rows = report.get("data", [])
    if "totals" in report and report["totals"]:
        t = {k: float(v) for k, v in report["totals"].items()}
        if "engagementRate" not in t and rows:
            tot_sess = sum(float(r.get("sessions", 0)) for r in rows)
            weighted_eng = sum(float(r.get("engagementRate", 0)) * float(r.get("sessions", 0)) for r in rows)
            t["engagementRate"] = (weighted_eng / tot_sess) if tot_sess else 0.0
        return {
            "totalUsers": t.get("totalUsers", 0.0),
            "newUsers": t.get("newUsers", 0.0),
            "sessions": t.get("sessions", 0.0),
            "averageSessionDuration": t.get("averageSessionDuration", 0.0),
            "engagementRate": t.get("engagementRate", 0.0),
            "screenPageViews": t.get("screenPageViews", 0.0),
        }

    if not rows:
        return {
            "totalUsers": 0.0,
            "newUsers": 0.0,
            "sessions": 0.0,
            "averageSessionDuration": 0.0,
            "engagementRate": 0.0,
            "screenPageViews": 0.0,
        }
    tot_sessions = sum(float(r.get("sessions", 0)) for r in rows)
    weighted_dur = sum(float(r.get("averageSessionDuration", 0)) * float(r.get("sessions", 0)) for r in rows)
    weighted_eng = sum(float(r.get("engagementRate", 0)) * float(r.get("sessions", 0)) for r in rows)
    return {
        "totalUsers": sum(float(r.get("totalUsers", 0)) for r in rows),
        "newUsers": sum(float(r.get("newUsers", 0)) for r in rows),
        "sessions": tot_sessions,
        "averageSessionDuration": (weighted_dur / tot_sessions) if tot_sessions else 0.0,
        "engagementRate": (weighted_eng / tot_sessions) if tot_sessions else 0.0,
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
        return ["✅ Traffic stayed within normal 14-day range (±1.8σ) over the last 3 days."]
    return anomalies


def aggregate_pages(rows: list[dict]) -> dict[str, dict]:
    """Aggregate page rows by pagePath (in case pageTitle had minor variations)."""
    agg = {}
    for r in rows:
        path = r.get("pagePath", "/")
        views = float(r.get("screenPageViews", 0))
        users = float(r.get("totalUsers", 0))
        sessions = float(r.get("sessions", 0))
        dur = float(r.get("averageSessionDuration", 0))
        eng = float(r.get("engagementRate", 0))
        title = r.get("pageTitle", "")
        if path not in agg:
            agg[path] = {
                "pagePath": path,
                "pageTitle": title,
                "views": views,
                "users": users,
                "sessions": sessions,
                "dur_weighted": dur * max(sessions, 1.0),
                "eng_weighted": eng * max(sessions, 1.0),
                "weight": max(sessions, 1.0),
            }
        else:
            agg[path]["views"] += views
            agg[path]["users"] += users
            agg[path]["sessions"] += sessions
            w = max(sessions, 1.0)
            agg[path]["dur_weighted"] += dur * w
            agg[path]["eng_weighted"] += eng * w
            agg[path]["weight"] += w
    for item in agg.values():
        w = item["weight"] or 1.0
        item["avg_dur"] = item["dur_weighted"] / w
        item["eng_rate"] = item["eng_weighted"] / w
    return agg


def generate_analyst_briefing(mode: str, curr_data: dict, prev_data: dict, baseline_daily: dict) -> tuple[list[str], list[str]]:
    """
    Act as a growth & editorial analyst briefing the founder:
    Returns (what_changed_bullets, what_to_improve_bullets).
    """
    period_word = "yesterday" if mode == "daily" else "this week"
    prev_word = "the day before" if mode == "daily" else "last week"

    curr_tot = extract_totals(curr_data["overview"])
    prev_tot = extract_totals(prev_data["overview"])

    c_users, p_users = curr_tot["totalUsers"], prev_tot["totalUsers"]
    c_views, p_views = curr_tot["screenPageViews"], prev_tot["screenPageViews"]
    c_dur, p_dur = curr_tot["averageSessionDuration"], prev_tot["averageSessionDuration"]
    c_eng, p_eng = curr_tot["engagementRate"] * 100.0, prev_tot["engagementRate"] * 100.0
    c_sess, p_sess = curr_tot["sessions"], prev_tot["sessions"]
    c_new = curr_tot["newUsers"]

    user_diff = c_users - p_users
    user_pct = pct_diff_val(c_users, p_users)
    view_diff = c_views - p_views
    view_pct = pct_diff_val(c_views, p_views)
    dur_diff = c_dur - p_dur
    pages_per_sess = (c_views / c_sess) if c_sess > 0 else 0.0
    prev_pages_per_sess = (p_views / p_sess) if p_sess > 0 else 0.0

    # --- Channel attribution ---
    curr_ch = {r.get("sessionDefaultChannelGroup", "(other)"): r for r in curr_data["channels"].get("data", [])}
    prev_ch = {r.get("sessionDefaultChannelGroup", "(other)"): r for r in prev_data["channels"].get("data", [])}
    all_channels = set(curr_ch.keys()) | set(prev_ch.keys())

    ch_deltas = []
    for ch in all_channels:
        cu = float(curr_ch.get(ch, {}).get("totalUsers", 0))
        pu = float(prev_ch.get(ch, {}).get("totalUsers", 0))
        cdur = float(curr_ch.get(ch, {}).get("averageSessionDuration", 0))
        ceng = float(curr_ch.get(ch, {}).get("engagementRate", 0)) * 100.0
        ch_deltas.append({
            "channel": ch,
            "curr_u": cu,
            "prev_u": pu,
            "diff": cu - pu,
            "pct": pct_diff_val(cu, pu),
            "dur": cdur,
            "eng": ceng,
        })

    ch_deltas_sorted = sorted(ch_deltas, key=lambda x: x["diff"], reverse=True)
    positive_chs = [c for c in ch_deltas_sorted if c["diff"] > 0]
    negative_chs = sorted([c for c in ch_deltas if c["diff"] < 0], key=lambda x: x["diff"])

    # Prefer named acquisition channels over 'Unassigned' as primary narrative lead, while noting Unassigned if present
    named_pos = [c for c in positive_chs if c["channel"].lower() != "unassigned"]
    top_gain_ch = named_pos[0] if named_pos else (positive_chs[0] if positive_chs else None)
    second_gain_ch = named_pos[1] if len(named_pos) > 1 else None
    top_drop_ch = negative_chs[0] if negative_chs else None

    # --- Page & Editorial Section analysis ---
    curr_pages = aggregate_pages(curr_data["pages"].get("data", []))
    prev_pages = aggregate_pages(prev_data["pages"].get("data", []))
    all_paths = set(curr_pages.keys()) | set(prev_pages.keys())

    page_deltas = []
    for path in all_paths:
        cp = curr_pages.get(path, {"views": 0.0, "users": 0.0, "avg_dur": 0.0, "eng_rate": 0.0, "pageTitle": ""})
        pp = prev_pages.get(path, {"views": 0.0, "users": 0.0, "avg_dur": 0.0, "eng_rate": 0.0, "pageTitle": ""})
        page_deltas.append({
            "path": path,
            "title": cp.get("pageTitle") or pp.get("pageTitle") or path,
            "curr_v": cp["views"],
            "prev_v": pp["views"],
            "diff_v": cp["views"] - pp["views"],
            "curr_u": cp["users"],
            "dur": cp["avg_dur"],
            "eng": cp["eng_rate"] * 100.0,
            "is_article": is_article_path(path),
            "section": clean_section_name(path),
        })

    articles_curr = sorted([p for p in page_deltas if p["is_article"] and p["curr_v"] > 0], key=lambda x: x["curr_v"], reverse=True)
    risers = sorted([p for p in page_deltas if p["is_article"] and p["diff_v"] > 0], key=lambda x: x["diff_v"], reverse=True)
    fallers = sorted([p for p in page_deltas if p["is_article"] and p["diff_v"] < 0], key=lambda x: x["diff_v"])

    # Section totals
    section_views = defaultdict(float)
    prev_section_views = defaultdict(float)
    for p in page_deltas:
        if p["is_article"]:
            section_views[p["section"]] += p["curr_v"]
            prev_section_views[p["section"]] += p["prev_v"]

    what_changed = []

    # 1. Headline Narrative: Why did traffic move?
    if c_users == 0 and p_users == 0:
        what_changed.append("• *Traffic Flat at Zero:* No active sessions recorded in either period.")
    else:
        direction = "grew" if user_diff > 0 else ("pulled back" if user_diff < 0 else "held steady")
        driver_parts = []
        if user_diff > 0 and top_gain_ch:
            gain_str = f"*{top_gain_ch['channel']}* (`+{int(top_gain_ch['diff'])}` users, `{int(top_gain_ch['curr_u'])}` total)"
            if second_gain_ch and second_gain_ch["diff"] >= 2:
                gain_str += f" and *{second_gain_ch['channel']}* (`+{int(second_gain_ch['diff'])}` users)"
            driver_parts.append(f"led by {gain_str}")
            if top_drop_ch:
                driver_parts.append(f"partially offset by softer *{top_drop_ch['channel']}* (`{int(top_drop_ch['diff'])}` users)")
        elif user_diff < 0 and top_drop_ch:
            driver_parts.append(f"primarily due to a cooldown in *{top_drop_ch['channel']}* (`{int(top_drop_ch['diff'])}` users, from `{int(top_drop_ch['prev_u'])}` → `{int(top_drop_ch['curr_u'])}`)")
            if top_gain_ch:
                driver_parts.append(f"while *{top_gain_ch['channel']}* added `+{int(top_gain_ch['diff'])}` users")
        driver_clause = f" — {', '.join(driver_parts)}" if driver_parts else ""
        what_changed.append(
            f"• *The Headline:* Audience {direction} *{user_pct:+.1f}%* {period_word} (`{int(c_users):,}` vs `{int(p_users):,}` users; `{int(c_views):,}` vs `{int(p_views):,}` pageviews){driver_clause}."
        )

    # 2. Content & Story Movers (What people actually read vs what faded)
    if articles_curr:
        top_art = articles_curr[0]
        top_share = (top_art["curr_v"] / c_views * 100.0) if c_views > 0 else 0.0
        mover_notes = [
            f"Top story `{top_art['path']}` generated *{int(top_art['curr_v'])} views* (`{top_share:.0f}%` of all site views, `{fmt_duration(top_art['dur'])}` avg read time)"
        ]
        if risers and risers[0]["path"] != top_art["path"] and risers[0]["diff_v"] >= 2:
            r0 = risers[0]
            mover_notes.append(f"`{r0['path']}` surged `+{int(r0['diff_v'])}` views (`{int(r0['curr_v'])}` total)")
        if fallers and abs(fallers[0]["diff_v"]) >= 3:
            f0 = fallers[0]
            mover_notes.append(f"previous driver `{f0['path']}` cooled off (`{int(f0['diff_v'])}` views, from `{int(f0['prev_v'])}` → `{int(f0['curr_v'])}`)")
        what_changed.append(f"• *Story Momentum:* {'; '.join(mover_notes)}.")

    # 3. Editorial Beat / Category Shift
    if section_views:
        top_sec, top_sec_v = max(section_views.items(), key=lambda x: x[1])
        prev_sec_v = prev_section_views.get(top_sec, 0.0)
        sec_share = (top_sec_v / c_views * 100.0) if c_views > 0 else 0.0
        sec_delta_str = f"up from `{int(prev_sec_v)}` views {prev_word}" if top_sec_v > prev_sec_v else f"vs `{int(prev_sec_v)}` views {prev_word}"
        what_changed.append(
            f"• *Winning Beat:* *{top_sec}* was your strongest editorial category with *{int(top_sec_v)} article views* (`{sec_share:.0f}%` of total traffic, {sec_delta_str})."
        )

    # 4. Reader Quality & Attention Shift
    eng_delta = c_eng - p_eng
    quality_dir = "improved" if dur_diff > 5 else ("dipped" if dur_diff < -5 else "stayed consistent")
    what_changed.append(
        f"• *Attention & Depth:* Reader time {quality_dir} from `{fmt_duration(p_dur)}` → *{fmt_duration(c_dur)}* (`{dur_diff:+.0f}s`), "
        f"with engagement rate at *{c_eng:.1f}%* (`{eng_delta:+.1f} pts`) and *{pages_per_sess:.2f} pages/session* (prev `{prev_pages_per_sess:.2f}`)."
    )

    # =========================================================================
    # WHAT CAN BE IMPROVED (Concrete, Data-Backed Founder Action Items)
    # =========================================================================
    improvements = []

    # 1. Identify "Click-and-Bail" articles (getting views, but <15s read time or low engagement)
    min_views_for_audit = 2 if mode == "daily" else 5
    bailout_articles = [
        p for p in articles_curr
        if p["curr_v"] >= min_views_for_audit and p["dur"] < 15.0
    ]
    deep_read_articles = [
        p for p in articles_curr
        if p["curr_v"] >= min_views_for_audit and p["dur"] >= 45.0
    ]

    if bailout_articles:
        bail_list = ", ".join(
            f"`{p['path']}` (`{int(p['curr_v'])}` views, only `{fmt_duration(p['dur'])}` read time)"
            for p in bailout_articles[:2]
        )
        improvements.append(
            f"• *Fix Headline-to-Intro Drop-Off:* Readers clicked {bail_list} but left in under 15 seconds. "
            f"*Action:* Add a 3-bullet 'Key Takeaways' box right below the headline and tighten the opening paragraph so social clickers don't bounce immediately."
        )

    # 2. Amplify "High-Retention / Deep Read" winners
    if deep_read_articles:
        best_deep = sorted(deep_read_articles, key=lambda x: x["dur"], reverse=True)[0]
        improvements.append(
            f"• *Double Down on High-Attention Stories:* `{best_deep['path']}` held readers for *{fmt_duration(best_deep['dur'])}* (`{best_deep['eng']:.0f}%` engaged). "
            f"*Action:* Repurpose this piece into a founder LinkedIn carousel / X thread and pin it in the 'Trending' slot on the homepage."
        )

    # 3. Single-page session / Recirculation bottleneck
    if pages_per_sess < 1.45 and c_sess > 0:
        potential_extra_views = max(1, int(round(c_sess * 0.35)))
        improvements.append(
            f"• *Plug the Recirculation Leak (`{pages_per_sess:.2f}` pages/session):* Almost every visitor leaves after reading 1 page. "
            f"*Action:* Insert an inline *'Related on Launch91'* callout block mid-article (around paragraph 4, reachable within `{fmt_duration(min(40.0, max(20.0, c_dur * 0.6)))}`) — lifting recirculation by just 0.35 pages/session adds *+{potential_extra_views} pageviews* from existing traffic."
        )

    # 4. Channel Quality, SEO & "Dark Social" UTM Blindness
    direct_ch = next((c for c in ch_deltas if c["channel"] == "Direct"), None)
    social_ch = next((c for c in ch_deltas if "Social" in c["channel"]), None)
    search_ch = next((c for c in ch_deltas if "Search" in c["channel"]), None)

    direct_share = (direct_ch["curr_u"] / c_users * 100.0) if (direct_ch and c_users > 0) else 0.0
    search_share = (search_ch["curr_u"] / c_users * 100.0) if (search_ch and c_users > 0) else 0.0

    if direct_share >= 45.0:
        social_note = (
            f" Notably, *Organic Social* readers stay longer (`{fmt_duration(social_ch['dur'])}`, `{social_ch['eng']:.0f}%` engaged) than Direct (`{fmt_duration(direct_ch['dur'])}`, `{direct_ch['eng']:.0f}%` engaged)."
            if social_ch and social_ch["curr_u"] > 0 and social_ch["dur"] > direct_ch["dur"]
            else ""
        )
        improvements.append(
            f"• *Fix 'Dark Social' Attribution (`{direct_share:.0f}%` Direct Traffic):* `{int(direct_ch['curr_u'])}` of `{int(c_users)}` users are bucketed as `Direct` — most of this is untracked WhatsApp, LinkedIn, or Slack shares.{social_note} "
            f"*Action:* Append `?utm_source=linkedin&utm_medium=social` (or `whatsapp`) to every link you and the team share so you can see which distribution channel actually converts."
        )

    if search_share < 15.0 and c_users > 0:
        search_u = int(search_ch["curr_u"]) if search_ch else 0
        search_dur_note = f" (and they read for `{fmt_duration(search_ch['dur'])}`)" if search_ch and search_ch["dur"] > 30 else ""
        improvements.append(
            f"• *Capture Search Intent (`{search_share:.1f}%` Organic Search):* Only `{search_u}` users came from Google Search{search_dur_note}. "
            f"*Action:* Put exact company names, funding amounts, and sector keywords at the front of `<title>` tags and H1s (e.g., `'Company Name Raises $XM Series A: ...'`) to capture Google News & search traffic after social spikes fade."
        )

    # 5. Returning Reader Habit & Concentration Risk
    ret_users = max(0.0, c_users - c_new)
    ret_pct = (ret_users / c_users * 100.0) if c_users > 0 else 0.0
    if ret_pct < 15.0 and c_users >= 10:
        improvements.append(
            f"• *Convert One-Time Visitors (`{ret_pct:.1f}%` Returning):* `{100 - ret_pct:.1f}%` of visitors are first-timers who haven't formed a return habit yet. "
            f"*Action:* Add a lightweight 'Join Launch91 WhatsApp / Weekly Brief' banner right after the 3rd paragraph on top stories to own distribution independent of social algorithms."
        )

    return what_changed, improvements[:4]


def build_report_text(mode: str, curr_data: dict, prev_data: dict, baseline_daily: dict) -> str:
    title = "📊 *Launch91 — Daily GA4 Founder & Analyst Briefing*" if mode == "daily" else "📈 *Launch91 — Weekly GA4 Founder & Analyst Briefing*"
    period_label = "Yesterday vs Prior Day" if mode == "daily" else "Last 7 Days vs Previous 7 Days (WoW)"

    curr_tot = extract_totals(curr_data["overview"])
    prev_tot = extract_totals(prev_data["overview"])

    c_users = curr_tot["totalUsers"]
    p_users = prev_tot["totalUsers"]
    c_new = curr_tot["newUsers"]
    p_new = prev_tot["newUsers"]
    c_dur = curr_tot["averageSessionDuration"]
    p_dur = prev_tot["averageSessionDuration"]
    c_eng = curr_tot["engagementRate"] * 100.0
    p_eng = prev_tot["engagementRate"] * 100.0
    c_views = curr_tot["screenPageViews"]
    p_views = prev_tot["screenPageViews"]

    what_changed, improvements = generate_analyst_briefing(mode, curr_data, prev_data, baseline_daily)

    lines = [
        f"{title}",
        f"_Property ID: {os.getenv('GA4_PROPERTY_ID')} | Comparison: {period_label}_",
        "",
        "🧠 *1. Analyst Briefing: What Happened & Why*",
        *what_changed,
        "",
        "🎯 *2. What Can Be Improved (Founder Action Items)*",
        *improvements,
        "",
        "📈 *3. Core Impact Metrics*",
        f"• *Total Users:* *{int(c_users):,}* (prev: {int(p_users):,}) — {pct_change(c_users, p_users)}",
        f"• *New Users:* *{int(c_new):,}* (prev: {int(p_new):,}) — {pct_change(c_new, p_new)}",
        f"• *Page Views:* *{int(c_views):,}* (prev: {int(p_views):,}) — {pct_change(c_views, p_views)}",
        f"• *Avg Read Duration:* *{fmt_duration(c_dur)}* (prev: {fmt_duration(p_dur)}) — {pct_change(c_dur, p_dur)}",
        f"• *Engagement Rate:* *{c_eng:.1f}%* (prev: {p_eng:.1f}%) — {pct_change(c_eng, p_eng)}",
        "",
    ]

    ret_users = max(0.0, c_users - c_new)
    new_pct = (c_new / c_users * 100.0) if c_users else 0.0
    ret_pct = (ret_users / c_users * 100.0) if c_users else 0.0
    lines.append("🧭 *4. Audience & Channel Shifts*")
    lines.append(f"• *Reader Mix:* New `{new_pct:.1f}%` ({int(c_new):,}) | Returning `{ret_pct:.1f}%` ({int(ret_users):,})")

    dev_rows = curr_data["devices"].get("data", [])
    if dev_rows:
        dev_parts = []
        tot_dev_u = sum(float(r.get("totalUsers", 0)) for r in dev_rows) or 1.0
        for r in dev_rows[:3]:
            d_name = r.get("deviceCategory", "unknown").capitalize()
            d_u = float(r.get("totalUsers", 0))
            d_dur = float(r.get("averageSessionDuration", 0))
            dev_parts.append(f"{d_name} `{d_u / tot_dev_u * 100:.0f}%` ({int(d_u):,}u, avg `{fmt_duration(d_dur)}`)")
        lines.append(f"• *Devices:* {' | '.join(dev_parts)}")

    prev_ch_map = {
        r.get("sessionDefaultChannelGroup", ""): float(r.get("totalUsers", 0))
        for r in prev_data["channels"].get("data", [])
    }
    ch_rows = curr_data["channels"].get("data", [])
    if ch_rows:
        for r in ch_rows[:5]:
            ch = r.get("sessionDefaultChannelGroup", "(other)")
            u = float(r.get("totalUsers", 0))
            dur = float(r.get("averageSessionDuration", 0))
            eng = float(r.get("engagementRate", 0)) * 100.0
            pu = prev_ch_map.get(ch, 0.0)
            lines.append(f"• *{ch}:* *{int(u):,}* users (prev {int(pu):,}, `{fmt_duration(dur)}` avg, `{eng:.0f}%` engaged) — {pct_change(u, pu)}")
    lines.append("")

    lines.append("🔥 *5. Top Pages & Reader Retention*")
    curr_pg_agg = aggregate_pages(curr_data["pages"].get("data", []))
    prev_pg_agg = aggregate_pages(prev_data["pages"].get("data", []))
    sorted_pg = sorted(curr_pg_agg.values(), key=lambda x: x["views"], reverse=True)
    if sorted_pg:
        for p in sorted_pg[:6]:
            path = p["pagePath"]
            views = p["views"]
            u = p["users"]
            dur = p["avg_dur"]
            eng = p["eng_rate"] * 100.0
            p_views_pg = prev_pg_agg.get(path, {}).get("views", 0.0)
            lines.append(
                f"• `{path}` — *{int(views):,}* views ({int(u):,}u | `{fmt_duration(dur)}` read | `{eng:.0f}%` eng) — {pct_change(views, p_views_pg)}"
            )
    else:
        lines.append("• _No page data for period._")
    lines.append("")

    lines.append("📡 *6. 14-Day Anomaly Radar*")
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

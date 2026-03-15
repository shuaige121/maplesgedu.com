#!/usr/bin/env python3
"""Fetch XHS ad data and generate dashboard JSON + thumbnails.

Runs from cron on 192.168.7.13 every hour.
Outputs: ads/data.json, ads/history.json, ads/thumbs/<note_id>.jpg
"""

import json
import subprocess
import sys
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

# --- Config ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADS_DIR = PROJECT_ROOT / "ads"
THUMBS_DIR = ADS_DIR / "thumbs"
DATA_FILE = ADS_DIR / "data.json"
HISTORY_FILE = ADS_DIR / "history.json"

ADVERTISER_ID = 7463472
WORKER_BASE = "https://api.maplesgedu.com/xhs/api"

# D5: Max retries for API calls
MAX_RETRIES = 2
RETRY_DELAY = 3  # seconds


def worker_call(endpoint: str, payload: dict) -> dict:
    """Call XHS API via Cloudflare Worker proxy. D5: retry on failure."""
    import urllib.request
    import urllib.error

    url = f"{WORKER_BASE}/{endpoint}"
    body = json.dumps({"advertiser_id": ADVERTISER_ID, **payload}).encode()

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-Advertiser-Id": str(ADVERTISER_ID),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                # D5: Check for token expiry and trigger refresh
                if result.get("code") in (40004, 40006, "TOKEN_EXPIRED"):
                    print(f"Token expired on {endpoint}, refreshing...", file=sys.stderr)
                    _refresh_token()
                    continue
                return result
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            # D5: Handle 401/403 as token issues
            if e.code in (401, 403) and attempt < MAX_RETRIES - 1:
                print(f"HTTP {e.code} on {endpoint}, refreshing token (attempt {attempt + 1})...",
                      file=sys.stderr)
                _refresh_token()
                continue
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                if attempt < MAX_RETRIES - 1:
                    print(f"HTTP {e.code} from {endpoint}, retrying in {RETRY_DELAY}s...",
                          file=sys.stderr)
                    time.sleep(RETRY_DELAY)
                    continue
                print(f"HTTP {e.code} from {endpoint}: {body_text[:200]}", file=sys.stderr)
                return {"ok": False, "msg": f"HTTP {e.code}"}
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"Request failed for {endpoint}: {e}, retrying...", file=sys.stderr)
                time.sleep(RETRY_DELAY)
                continue
            print(f"Request failed for {endpoint}: {e}", file=sys.stderr)
            return {"ok": False, "msg": str(e)}

    return {"ok": False, "msg": "max retries exceeded"}


def _refresh_token() -> bool:
    """D5: Call the worker's token refresh endpoint."""
    import urllib.request
    try:
        url = f"{WORKER_BASE}/token.refresh"
        body = json.dumps({"advertiser_id": ADVERTISER_ID}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-Advertiser-Id": str(ADVERTISER_ID),
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("Token refreshed successfully", file=sys.stderr)
                return True
            print(f"Token refresh failed: {result.get('msg')}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Token refresh error: {e}", file=sys.stderr)
        return False


def fetch_note_report(start: str, end: str, time_unit: str = "SUMMARY") -> list[dict]:
    result = worker_call("report.offline.note", {
        "start_date": start, "end_date": end,
        "time_unit": time_unit, "page_num": 1, "page_size": 100,
    })
    if not result.get("ok"):
        print(f"Note report error: {result.get('msg')}", file=sys.stderr)
        return []
    return (result.get("data") or {}).get("data_list", [])


def fetch_daily_note_report(start: str, end: str) -> list[dict]:
    """Fetch note report with DAY granularity for trend charts."""
    result = worker_call("report.offline.note", {
        "start_date": start, "end_date": end,
        "time_unit": "DAY", "page_num": 1, "page_size": 500,
    })
    if not result.get("ok"):
        return []
    return (result.get("data") or {}).get("data_list", [])


def fetch_campaign_report(start: str, end: str, time_unit: str = "DAY") -> list[dict]:
    result = worker_call("report.offline.campaign", {
        "start_date": start, "end_date": end,
        "time_unit": time_unit, "page_num": 1, "page_size": 100,
    })
    if not result.get("ok"):
        return []
    return (result.get("data") or {}).get("data_list", [])


def fetch_realtime_creative() -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    result = worker_call("report.realtime.creative", {
        "start_date": today, "end_date": today,
        "page_num": 1, "page_size": 100,
    })
    creativity_dtos = result.get("creativity_dtos", [])
    if not creativity_dtos:
        creativity_dtos = (result.get("data") or {}).get("creativity_dtos", [])
    if not creativity_dtos:
        return []

    results = []
    for c in creativity_dtos:
        base = c.get("base_creativity_dto", {})
        d = c.get("data", {})
        if int(d.get("impression", 0)) == 0:
            continue
        results.append({
            "note_id": base.get("note_id", ""),
            "creativity_name": base.get("creativity_name", ""),
            **d,
        })
    return results


def fetch_realtime_account() -> dict:
    """Fetch today's account-level realtime summary."""
    today = datetime.now().strftime("%Y-%m-%d")
    result = worker_call("report.realtime.account", {
        "start_date": today, "end_date": today,
    })
    return result.get("total_data") or result.get("data", {}).get("total_data", {}) or {}


def fetch_account_budget() -> dict:
    result = worker_call("account.budget", {})
    return result.get("data") or {}


def fetch_note_covers() -> dict[str, str]:
    covers: dict[str, str] = {}
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    result = worker_call("report.offline.creative", {
        "start_date": year_ago, "end_date": yesterday,
        "time_unit": "SUMMARY", "page_num": 1, "page_size": 100,
    })
    for r in (result.get("data") or {}).get("data_list", []):
        nid = r.get("note_id", "")
        img = r.get("creativity_image", "")
        if nid and img:
            covers[nid] = img
    return covers


def fetch_note_titles() -> dict[str, str]:
    """Fetch real note titles from note.list API."""
    titles: dict[str, str] = {}
    for note_type in [1, 2]:  # 1=图文 2=视频
        for page in range(1, 10):
            result = worker_call("note.list", {
                "note_type": note_type,
                "page_index": page,
                "page_size": 100,
            })
            notes = (result.get("data") or {}).get("notes", [])
            if not notes:
                break
            for n in notes:
                nid = n.get("note_id", "")
                title = n.get("title", "")
                if nid and title:
                    titles[nid] = title
    return titles


def download_cover(note_id: str, image_url: str) -> bool:
    import urllib.request

    thumb_path = THUMBS_DIR / f"{note_id}.jpg"
    if thumb_path.exists():
        age_hours = (datetime.now().timestamp() - thumb_path.stat().st_mtime) / 3600
        if age_hours < 24:
            return True
    try:
        req = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.xiaohongshu.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            thumb_path.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"Download failed for {note_id}: {e}", file=sys.stderr)
        return False


def summarize_notes(rows: list[dict], titles: dict[str, str] | None = None) -> list[dict]:
    titles = titles or {}
    by_note: dict[str, dict] = {}
    for r in rows:
        nid = r.get("note_id", "")
        if not nid:
            continue
        if nid not in by_note:
            # 优先用真实标题，fallback 到 creativity_name
            name = titles.get(nid) or r.get("creativity_name", r.get("campaign_name", ""))
            by_note[nid] = {
                "note_id": nid,
                "name": name,
                "impression": 0, "click": 0, "fee": 0.0,
                "message_consult": 0, "initiative_message": 0,
                "msg_leads_num": 0, "interaction": 0,
                "like": 0, "collect": 0, "comment": 0, "share": 0, "follow": 0,
            }
        s = by_note[nid]
        for k in ["impression", "click", "message_consult", "initiative_message",
                   "msg_leads_num", "interaction", "like", "collect", "comment", "share", "follow"]:
            s[k] += int(r.get(k, 0))
        s["fee"] += float(r.get("fee", 0))
    for s in by_note.values():
        s["ctr"] = f"{s['click'] / s['impression'] * 100:.2f}%" if s["impression"] > 0 else "0%"
        s["cpc"] = round(s["fee"] / s["click"], 2) if s["click"] > 0 else 0
        s["cpl"] = round(s["fee"] / s["msg_leads_num"], 0) if s["msg_leads_num"] > 0 else 0
        s["msg_cost"] = round(s["fee"] / s["message_consult"], 0) if s["message_consult"] > 0 else 0
        s["fee"] = round(s["fee"], 2)
    result = list(by_note.values())
    result.sort(key=lambda n: n["fee"], reverse=True)
    return result


def enrich_notes_with_metrics(notes: list[dict], daily_trend: list[dict], range_key: str) -> list[dict]:
    """D1+D4: Add computed metrics to each note in a range.

    - fee_pct: percentage of total fee for this range
    - msg_to_lead_rate: message_consult -> msg_leads_num conversion rate
    - fee_change_7d: 7-day moving average fee change (for 'all' range with daily data)
    - efficiency_score: normalized composite score (0-100)
    """
    if not notes:
        return notes

    total_fee = sum(n["fee"] for n in notes) or 1  # avoid division by zero

    # D4: Collect values for min-max normalization
    ctrs = []
    inv_cpcs = []
    inv_cpls = []
    msg_rates = []

    for n in notes:
        ctr_val = n["click"] / n["impression"] * 100 if n["impression"] > 0 else 0
        ctrs.append(ctr_val)
        inv_cpcs.append(1.0 / n["cpc"] if n["cpc"] > 0 else 0)
        inv_cpls.append(1.0 / n["cpl"] if n["cpl"] > 0 else 0)
        msg_rate = n["msg_leads_num"] / n["message_consult"] * 100 if n["message_consult"] > 0 else 0
        msg_rates.append(msg_rate)

    def normalize_list(vals: list[float]) -> list[float]:
        mn, mx = min(vals), max(vals)
        rng = mx - mn
        if rng == 0:
            return [50.0] * len(vals)
        return [(v - mn) / rng * 100 for v in vals]

    norm_ctrs = normalize_list(ctrs)
    norm_inv_cpcs = normalize_list(inv_cpcs)
    norm_inv_cpls = normalize_list(inv_cpls)
    norm_msg_rates = normalize_list(msg_rates)

    # D1: Compute 7-day moving average from daily_trend for fee_change_7d
    fee_change_7d_str = ""
    if len(daily_trend) >= 2:
        recent_7 = daily_trend[-7:] if len(daily_trend) >= 7 else daily_trend
        prev_7 = daily_trend[-14:-7] if len(daily_trend) >= 14 else daily_trend[:len(daily_trend)//2] if len(daily_trend) >= 2 else []
        if recent_7 and prev_7:
            avg_recent = sum(d["fee"] for d in recent_7) / len(recent_7)
            avg_prev = sum(d["fee"] for d in prev_7) / len(prev_7)
            if avg_prev > 0:
                pct_change = (avg_recent - avg_prev) / avg_prev * 100
                fee_change_7d_str = f"{pct_change:+.1f}%"

    for i, n in enumerate(notes):
        # D1: Fee percentage
        n["fee_pct"] = round(n["fee"] / total_fee * 100, 1)

        # D1: Message to lead conversion rate
        if n["message_consult"] > 0:
            rate = n["msg_leads_num"] / n["message_consult"] * 100
            n["msg_to_lead_rate"] = f"{rate:.1f}%"
        else:
            n["msg_to_lead_rate"] = "N/A"

        # D1: 7-day fee change (same for all notes in a range, based on daily trend)
        n["fee_change_7d"] = fee_change_7d_str if fee_change_7d_str else "N/A"

        # D4: Efficiency score
        w1, w2, w3, w4 = 0.25, 0.25, 0.3, 0.2
        score = (w1 * norm_ctrs[i] + w2 * norm_inv_cpcs[i] +
                 w3 * norm_inv_cpls[i] + w4 * norm_msg_rates[i])
        n["efficiency_score"] = round(score, 1)

    return notes


def compute_daily_trend_with_ma(daily_trend: list[dict]) -> list[dict]:
    """D1: Add 7-day moving averages and day-over-day changes to daily trend."""
    for i, d in enumerate(daily_trend):
        # Day-over-day fee change
        if i > 0 and daily_trend[i - 1]["fee"] > 0:
            pct = (d["fee"] - daily_trend[i - 1]["fee"]) / daily_trend[i - 1]["fee"] * 100
            d["fee_dod_change"] = f"{pct:+.1f}%"
        else:
            d["fee_dod_change"] = "N/A"

        # 7-day moving averages
        window = daily_trend[max(0, i - 6):i + 1]
        d["fee_ma7"] = round(sum(x["fee"] for x in window) / len(window), 2)
        d["ctr_ma7"] = round(sum(x["ctr"] for x in window) / len(window), 2)
        d["cpc_ma7"] = round(sum(x["cpc"] for x in window) / len(window), 2)

    return daily_trend


def build_hourly_trend() -> list[dict]:
    """D3: Build hourly trend from history.json for today."""
    if not HISTORY_FILE.exists():
        return []
    try:
        history = json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_entries = [h for h in history if h.get("timestamp", "").startswith(today_str)]

    if not today_entries:
        return []

    hourly: list[dict] = []
    for entry in today_entries:
        ts = entry.get("timestamp", "")
        try:
            hour = ts[11:16]  # "HH:MM"
        except (IndexError, TypeError):
            continue
        hourly.append({
            "hour": hour,
            "fee": entry.get("fee", 0),
            "impression": entry.get("impression", 0),
            "click": entry.get("click", 0),
            "message_consult": entry.get("message_consult", 0),
            "msg_leads_num": entry.get("msg_leads_num", 0),
        })

    return hourly


def build_daily_trend(rows: list[dict]) -> list[dict]:
    """Build daily trend data from DAY-granularity report."""
    by_day: dict[str, dict] = {}
    for r in rows:
        day = r.get("time", "")
        if not day:
            continue
        if day not in by_day:
            by_day[day] = {"date": day, "fee": 0.0, "impression": 0, "click": 0,
                           "message_consult": 0, "msg_leads_num": 0}
        d = by_day[day]
        d["fee"] += float(r.get("fee", 0))
        d["impression"] += int(r.get("impression", 0))
        d["click"] += int(r.get("click", 0))
        d["message_consult"] += int(r.get("message_consult", 0))
        d["msg_leads_num"] += int(r.get("msg_leads_num", 0))
    for d in by_day.values():
        d["fee"] = round(d["fee"], 2)
        d["ctr"] = round(d["click"] / d["impression"] * 100, 2) if d["impression"] > 0 else 0
        d["cpc"] = round(d["fee"] / d["click"], 2) if d["click"] > 0 else 0
    result = sorted(by_day.values(), key=lambda x: x["date"])
    return result


def append_hourly_snapshot(account_realtime: dict) -> None:
    """Append hourly snapshot to history.json for time-series charts."""
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            history = []

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "fee": float(account_realtime.get("fee", 0)),
        "impression": int(account_realtime.get("impression", 0)),
        "click": int(account_realtime.get("click", 0)),
        "message_consult": int(account_realtime.get("message_consult", 0)),
        "msg_leads_num": int(account_realtime.get("msg_leads_num", 0)),
    }
    history.append(snapshot)

    # Keep last 30 days of hourly data (720 entries)
    if len(history) > 720:
        history = history[-720:]

    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False))


def generate_ai_commentary(account: dict, daily_trend: list[dict], notes_all: list[dict]) -> str:
    """D2: Generate AI commentary via OpenClaw gateway (localhost:18789)."""
    import urllib.request
    import urllib.error

    # Build a concise data summary for the AI
    total_fee_all = sum(n["fee"] for n in notes_all)
    total_leads_all = sum(n["msg_leads_num"] for n in notes_all)
    total_msg_all = sum(n["message_consult"] for n in notes_all)

    # Recent trend info
    trend_summary = ""
    if len(daily_trend) >= 2:
        last = daily_trend[-1]
        prev = daily_trend[-2]
        trend_summary = (
            f"最近两天: {prev['date']} 消费¥{prev['fee']} CTR={prev['ctr']}% | "
            f"{last['date']} 消费¥{last['fee']} CTR={last['ctr']}%"
        )
    if len(daily_trend) >= 7:
        recent_7_fee = sum(d["fee"] for d in daily_trend[-7:])
        trend_summary += f" | 近7天总消费¥{recent_7_fee:.0f}"

    # Top 3 notes
    top_notes = notes_all[:3]
    top_info = ""
    for n in top_notes:
        top_info += (
            f"  - {n['note_id'][:8]}... 消费¥{n['fee']} CTR={n['ctr']} "
            f"CPC=¥{n['cpc']} 留资={n['msg_leads_num']}\n"
        )

    lead_rate_line = ""
    if total_msg_all > 0:
        lead_rate_line = f"整体私信转留资率: {total_leads_all / total_msg_all * 100:.1f}%\n"

    prompt = (
        f"你是小红书聚光平台广告优化师。请根据以下数据给出简短的中文分析评语（100-200字），"
        f"包含：当前表现总结、异常提醒、优化建议。不要用markdown格式。\n\n"
        f"账户: UF-SG枫叶留学 (留学机构)\n"
        f"今日: 消费¥{account.get('today_spend', 0):.2f} "
        f"曝光={account.get('today_impression', 0)} "
        f"点击={account.get('today_click', 0)} "
        f"私信={account.get('today_message', 0)} "
        f"留资={account.get('today_leads', 0)}\n"
        f"余额: ¥{account.get('balance', 0)}\n"
        f"历史总计: 消费¥{total_fee_all:.0f} 私信={total_msg_all} 留资={total_leads_all}\n"
        f"{lead_rate_line}"
        f"{trend_summary}\n"
        f"消费TOP3笔记:\n{top_info}"
    )

    # Try Claude CLI first (cheapest, fastest)
    try:
        cli_result = subprocess.run(
            ["claude", "--print", "--model", "sonnet", "-p", prompt],
            capture_output=True, text=True, timeout=30,
        )
        if cli_result.returncode == 0 and cli_result.stdout.strip():
            return cli_result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"Claude CLI unavailable: {e}", file=sys.stderr)

    # Fallback: OpenClaw gateway
    try:
        api_body = json.dumps({
            "model": "sonnet",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.7,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:18789/v1/chat/completions",
            data=api_body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content.strip()
    except Exception as e:
        print(f"AI commentary generation failed: {e}", file=sys.stderr)

    # Fallback: generate a basic summary without AI
    return _fallback_commentary(account, daily_trend, total_fee_all, total_msg_all, total_leads_all)


def _fallback_commentary(account: dict, daily_trend: list[dict],
                         total_fee: float, total_msg: int, total_leads: int) -> str:
    """Generate a basic commentary when AI is unavailable."""
    parts = []
    spend = account.get("today_spend", 0)
    parts.append(f"今日消费¥{spend:.2f}")
    if account.get("today_click", 0) > 0 and account.get("today_impression", 0) > 0:
        ctr = account["today_click"] / account["today_impression"] * 100
        parts.append(f"CTR={ctr:.2f}%")
    if total_msg > 0:
        rate = total_leads / total_msg * 100
        parts.append(f"整体私信转留资率{rate:.1f}%")
    if len(daily_trend) >= 2:
        prev_fee = daily_trend[-2]["fee"]
        last_fee = daily_trend[-1]["fee"]
        if prev_fee > 0:
            chg = (last_fee - prev_fee) / prev_fee * 100
            parts.append(f"消费环比{chg:+.1f}%")
    return "，".join(parts) + "。(AI评语生成失败，显示基础数据摘要)"


def build_dashboard_data() -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")

    print("Fetching realtime account summary...")
    account_rt = fetch_realtime_account()

    print("Fetching today's realtime creatives...")
    today_data = fetch_realtime_creative()

    print("Fetching account budget...")
    budget = fetch_account_budget()

    print("Fetching 7-day note report...")
    week_data = fetch_note_report(week_ago, yesterday)

    print("Fetching 30-day note report...")
    month_data = fetch_note_report(month_ago, yesterday)

    print("Fetching all-time note report...")
    all_data = fetch_note_report(year_ago, yesterday)

    print("Fetching 30-day daily trend...")
    daily_rows = fetch_daily_note_report(month_ago, yesterday)

    print("Fetching note covers...")
    covers = fetch_note_covers()

    print("Fetching note titles...")
    titles = fetch_note_titles()

    # Collect all unique note IDs
    all_note_ids = set()
    for rows in [today_data, week_data, month_data, all_data]:
        for r in rows:
            nid = r.get("note_id", "")
            if nid:
                all_note_ids.add(nid)

    # Download covers
    print(f"Downloading covers for {len(all_note_ids)} notes ({len(covers)} have images)...")
    has_thumb = {}
    for nid in all_note_ids:
        if nid in covers:
            has_thumb[nid] = download_cover(nid, covers[nid])
        else:
            has_thumb[nid] = False

    # Append hourly snapshot
    append_hourly_snapshot(account_rt)

    # Build daily trend with D1 enhancements (MA7, DoD change)
    daily_trend = build_daily_trend(daily_rows)
    daily_trend = compute_daily_trend_with_ma(daily_trend)

    # Summarize notes per range
    notes_today = summarize_notes(today_data, titles)
    notes_week = summarize_notes(week_data, titles)
    notes_month = summarize_notes(month_data, titles)
    notes_all = summarize_notes(all_data, titles)

    # D1 + D4: Enrich notes with computed metrics and efficiency scores
    notes_today = enrich_notes_with_metrics(notes_today, daily_trend, "today")
    notes_week = enrich_notes_with_metrics(notes_week, daily_trend, "week")
    notes_month = enrich_notes_with_metrics(notes_month, daily_trend, "month")
    notes_all = enrich_notes_with_metrics(notes_all, daily_trend, "all")

    # Account summary
    account_summary = {
        "balance": budget.get("total_balance") or budget.get("available_balance") or 0,
        "daily_budget": budget.get("day_budget") or 0,
        "today_spend": float(account_rt.get("fee", 0)),
        "today_impression": int(account_rt.get("impression", 0)),
        "today_click": int(account_rt.get("click", 0)),
        "today_message": int(account_rt.get("message_consult", 0)),
        "today_leads": int(account_rt.get("msg_leads_num", 0)),
        "today_ctr": account_rt.get("ctr", "0%"),
        "today_cpc": float(account_rt.get("acp", 0)),
    }

    # D3: Build hourly trend from history.json
    print("Building hourly trend from history...")
    hourly_trend = build_hourly_trend()

    # D2: Generate AI commentary
    print("Generating AI commentary...")
    ai_commentary = generate_ai_commentary(account_summary, daily_trend, notes_all)
    print(f"AI commentary: {ai_commentary[:80]}...")

    return {
        "updated_at": now.isoformat(),
        "advertiser": {"id": ADVERTISER_ID, "name": "UF-SG枫叶留学"},
        "account": account_summary,
        "ai_commentary": ai_commentary,
        "hourly_trend": hourly_trend,
        "daily_trend": daily_trend,
        "ranges": {
            "today": {"label": "今日", "start": today, "end": today,
                      "notes": notes_today},
            "week": {"label": "近7天", "start": week_ago, "end": today,
                     "notes": notes_week},
            "month": {"label": "近30天", "start": month_ago, "end": today,
                      "notes": notes_month},
            "all": {"label": "全部", "start": year_ago, "end": today,
                    "notes": notes_all},
        },
        "thumbnails": {nid: f"thumbs/{nid}.jpg" for nid, ok in has_thumb.items() if ok},
        "note_titles": {nid: t for nid, t in titles.items() if nid in all_note_ids},
    }


def git_push():
    os.chdir(PROJECT_ROOT)
    subprocess.run(["git", "add", "ads/"], check=False)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
    if result.returncode == 0:
        print("No changes to commit")
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "commit", "-m", f"ads: auto-update {timestamp}"],
                   check=False, capture_output=True)
    subprocess.run(["git", "push"], check=False, capture_output=True)
    print("Pushed to GitHub")


def main():
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {DATA_FILE}")
    if "--push" in sys.argv:
        git_push()


if __name__ == "__main__":
    main()

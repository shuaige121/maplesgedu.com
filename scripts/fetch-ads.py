#!/usr/bin/env python3
"""Fetch XHS ad data and generate dashboard JSON + thumbnails.

Runs from cron on 192.168.7.13 every hour.
Outputs: ads/data.json, ads/history.json, ads/thumbs/<note_id>.jpg
"""

import json
import subprocess
import sys
import os
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


def worker_call(endpoint: str, payload: dict) -> dict:
    """Call XHS API via Cloudflare Worker proxy."""
    import urllib.request
    import urllib.error

    url = f"{WORKER_BASE}/{endpoint}"
    body = json.dumps({"advertiser_id": ADVERTISER_ID, **payload}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Advertiser-Id": str(ADVERTISER_ID),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            print(f"HTTP {e.code} from {endpoint}: {body_text[:200]}", file=sys.stderr)
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        print(f"Request failed for {endpoint}: {e}", file=sys.stderr)
        return {"ok": False, "msg": str(e)}


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


def summarize_notes(rows: list[dict]) -> list[dict]:
    by_note: dict[str, dict] = {}
    for r in rows:
        nid = r.get("note_id", "")
        if not nid:
            continue
        if nid not in by_note:
            by_note[nid] = {
                "note_id": nid,
                "name": r.get("creativity_name", r.get("campaign_name", "")),
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

    return {
        "updated_at": now.isoformat(),
        "advertiser": {"id": ADVERTISER_ID, "name": "UF-SG枫叶留学"},
        "account": account_summary,
        "daily_trend": build_daily_trend(daily_rows),
        "ranges": {
            "today": {"label": "今日", "start": today, "end": today,
                      "notes": summarize_notes(today_data)},
            "week": {"label": "近7天", "start": week_ago, "end": today,
                     "notes": summarize_notes(week_data)},
            "month": {"label": "近30天", "start": month_ago, "end": today,
                      "notes": summarize_notes(month_data)},
            "all": {"label": "全部", "start": year_ago, "end": today,
                    "notes": summarize_notes(all_data)},
        },
        "thumbnails": {nid: f"thumbs/{nid}.jpg" for nid, ok in has_thumb.items() if ok},
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

#!/usr/bin/env python3
"""Fetch XHS ad data and generate dashboard JSON + thumbnails.

Runs from cron on 192.168.7.13 every hour.
Outputs: ads/data.json, ads/thumbs/<note_id>.png
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

XHS_CLI = "uv run xhs-ad-console"
XHS_CLI_DIR = Path.home() / "xhs-ad-console"
ADVERTISER_ID = 7463472
WORKER_BASE = "https://api.maplesgedu.com/xhs/api"


def run_api(args: str) -> dict:
    """Run xhs-ad-console api command and return parsed JSON."""
    cmd = f"cd {XHS_CLI_DIR} && {XHS_CLI} api {args} --json"
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"ERROR: {cmd}\n{result.stderr}", file=sys.stderr)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"JSON parse error: {result.stdout[:200]}", file=sys.stderr)
        return {}


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
    """Fetch note-level offline report via Worker proxy."""
    result = worker_call("report.offline.note", {
        "start_date": start,
        "end_date": end,
        "time_unit": time_unit,
        "page_num": 1,
        "page_size": 100,
    })
    if not result.get("ok"):
        print(f"API error: {result.get('msg')}", file=sys.stderr)
        return []
    return (result.get("data") or {}).get("data_list", [])


def fetch_realtime_creative() -> list[dict]:
    """Fetch today's realtime creative data via Worker proxy."""
    today = datetime.now().strftime("%Y-%m-%d")
    result = worker_call("report.realtime.creative", {
        "start_date": today,
        "end_date": today,
        "page_num": 1,
        "page_size": 100,
    })

    # Worker now returns top-level fields: creativity_dtos, page, total_data
    creativity_dtos = result.get("creativity_dtos", [])
    if not creativity_dtos:
        # Fallback: check inside data
        creativity_dtos = (result.get("data") or {}).get("creativity_dtos", [])
    if not creativity_dtos:
        print(f"Realtime API: {result.get('msg', 'no creativity data')}", file=sys.stderr)
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


def fetch_note_covers() -> dict[str, str]:
    """Fetch cover images from creative offline report (creativity_image field)."""
    covers: dict[str, str] = {}
    # 用全时间范围拉创意报表获取封面图
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    result = worker_call("report.offline.creative", {
        "start_date": year_ago,
        "end_date": yesterday,
        "time_unit": "SUMMARY",
        "page_num": 1,
        "page_size": 100,
    })
    rows = (result.get("data") or {}).get("data_list", [])
    for r in rows:
        nid = r.get("note_id", "")
        img = r.get("creativity_image", "")
        if nid and img:
            covers[nid] = img
    return covers


def download_cover(note_id: str, image_url: str) -> bool:
    """Download cover image as thumbnail."""
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


def build_dashboard_data() -> dict:
    """Build the complete dashboard JSON."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")

    print("Fetching today's realtime data...")
    today_data = fetch_realtime_creative()

    # 离线报表 end_date 不能是今天，只有 T-1 数据
    print("Fetching 7-day note report...")
    week_data = fetch_note_report(week_ago, yesterday)

    print("Fetching 30-day note report...")
    month_data = fetch_note_report(month_ago, yesterday)

    print("Fetching all-time note report...")
    all_data = fetch_note_report(year_ago, yesterday)

    print("Fetching note covers...")
    covers = fetch_note_covers()

    # Collect all unique note IDs
    all_note_ids = set()
    for rows in [today_data, week_data, month_data, all_data]:
        for r in rows:
            nid = r.get("note_id", "")
            if nid:
                all_note_ids.add(nid)

    # Build per-note summary
    def summarize(rows: list[dict]) -> dict[str, dict]:
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
            s["impression"] += int(r.get("impression", 0))
            s["click"] += int(r.get("click", 0))
            s["fee"] += float(r.get("fee", 0))
            s["message_consult"] += int(r.get("message_consult", 0))
            s["initiative_message"] += int(r.get("initiative_message", 0))
            s["msg_leads_num"] += int(r.get("msg_leads_num", 0))
            s["interaction"] += int(r.get("interaction", 0))
            s["like"] += int(r.get("like", 0))
            s["collect"] += int(r.get("collect", 0))
            s["comment"] += int(r.get("comment", 0))
            s["share"] += int(r.get("share", 0))
            s["follow"] += int(r.get("follow", 0))
        # Add computed fields
        for s in by_note.values():
            s["ctr"] = f"{s['click'] / s['impression'] * 100:.2f}%" if s["impression"] > 0 else "0%"
            s["cpc"] = round(s["fee"] / s["click"], 2) if s["click"] > 0 else 0
            s["fee"] = round(s["fee"], 2)
        return by_note

    # Download cover images from creativity_image
    print(f"Downloading covers for {len(all_note_ids)} notes ({len(covers)} have images)...")
    has_thumb = {}
    for nid in all_note_ids:
        if nid in covers:
            has_thumb[nid] = download_cover(nid, covers[nid])
        else:
            has_thumb[nid] = False

    return {
        "updated_at": now.isoformat(),
        "advertiser": {"id": ADVERTISER_ID, "name": "UF-SG枫叶留学"},
        "ranges": {
            "today": {
                "label": "今日",
                "start": today,
                "end": today,
                "notes": list(summarize(today_data).values()),
            },
            "week": {
                "label": "近7天",
                "start": week_ago,
                "end": today,
                "notes": list(summarize(week_data).values()),
            },
            "month": {
                "label": "近30天",
                "start": month_ago,
                "end": today,
                "notes": list(summarize(month_data).values()),
            },
            "all": {
                "label": "全部",
                "start": year_ago,
                "end": today,
                "notes": list(summarize(all_data).values()),
            },
        },
        "thumbnails": {nid: f"thumbs/{nid}.jpg" for nid, ok in has_thumb.items() if ok},
    }


def git_push():
    """Commit and push changes."""
    os.chdir(PROJECT_ROOT)
    subprocess.run(["git", "add", "ads/"], check=False)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], capture_output=True
    )
    if result.returncode == 0:
        print("No changes to commit")
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(
        ["git", "commit", "-m", f"ads: auto-update {timestamp}"],
        check=False, capture_output=True,
    )
    subprocess.run(["git", "push"], check=False, capture_output=True)
    print("Pushed to GitHub")


def main():
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data()

    # Sort notes by fee descending in each range
    for rng in data["ranges"].values():
        rng["notes"].sort(key=lambda n: n["fee"], reverse=True)

    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {DATA_FILE}")

    if "--push" in sys.argv:
        git_push()


if __name__ == "__main__":
    main()

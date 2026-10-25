"""Daily Instagram Reel publisher for the One Block series (GitHub Actions).

Runs hourly; publishes each day's reel at its planned moment:
  - Oct 25, 2026 at 16:30 UTC (10:00 PM IST)
  - Oct 26-31, 2026 at 13:30 UTC (7:00 PM IST)

Safe to run often: skips when not yet due, skips if the same caption was
already published (idempotent), exits cleanly on non-scheduled days.

Env:
  IG_TOKEN   - Instagram long-lived access token
  IG_USER_ID - Instagram business user id
  DRYRUN     - "true" stops after container creation (no publish)
  FORCE_DATE - optional "YYYY-MM-DD" override (manual reruns)
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.instagram.com/v21.0"
IG_USER_ID = os.environ["IG_USER_ID"]
TOKEN = os.environ["IG_TOKEN"]
DRYRUN = os.environ.get("DRYRUN", "false").lower() == "true"
FORCE_DATE = os.environ.get("FORCE_DATE", "").strip()

RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/clips"

# date -> (file, caption day, publish time UTC as HH:MM)
SCHEDULE = {
    "2026-10-25": ("day1.mp4", "DAY 1", "16:30"),
    "2026-10-26": ("day2.mp4", "DAY 2", "13:30"),
    "2026-10-27": ("day3.mp4", "DAY 3", "13:30"),
    "2026-10-28": ("day4.mp4", "DAY 4", "13:30"),
    "2026-10-29": ("day5.mp4", "DAY 5", "13:30"),
    "2026-10-30": ("day6.mp4", "DAY 6", "13:30"),
    "2026-10-31": ("day7.mp4", "DAY 7", "13:30"),
}

CAPTIONS = json.load(open("captions.json", encoding="utf-8"))


def main() -> int:
    now = datetime.now(timezone.utc)
    today = FORCE_DATE or now.strftime("%Y-%m-%d")
    entry = SCHEDULE.get(today)
    if not entry:
        print(f"No reel scheduled for {today}; nothing to do.")
        return 0
    clip_file, day, hhmm = entry
    publish_at = datetime.strptime(f"{today} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    if now < publish_at:
        print(f"{day} is due at {hhmm} UTC (in {(publish_at - now)}); not yet.")
        return 0
    if now > publish_at + timedelta(hours=6):
        print(f"{day} window missed (due {publish_at}, now {now}). Use FORCE_DATE to rerun.")
        return 1

    caption = CAPTIONS[day]

    # Idempotency: skip if this exact caption was already published.
    feed = requests.get(
        f"{GRAPH}/{IG_USER_ID}/media",
        params={"fields": "caption", "limit": 10, "access_token": TOKEN},
        timeout=30,
    )
    feed.raise_for_status()
    for item in feed.json().get("data", []):
        if (item.get("caption") or "").strip() == caption.strip():
            print(f"{day} already published (matching caption found); skipping.")
            return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    video_url = f"{RAW_BASE.format(repo=repo)}/{clip_file}"
    print(f"{day}: publishing {clip_file} (due {publish_at.isoformat()}, now {now.isoformat()})")
    print(f"video_url: {video_url}")

    # 1. Create the media container; Instagram fetches the video from the URL.
    r = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": TOKEN,
        },
        timeout=60,
    )
    print("container:", r.status_code, r.text[:300])
    r.raise_for_status()
    creation_id = r.json()["id"]

    if DRYRUN:
        print(f"DRYRUN: container {creation_id} created; not publishing.")
        return 0

    # 2. Wait for Instagram to finish processing the video.
    for attempt in range(60):
        time.sleep(15)
        s = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code,status", "access_token": TOKEN},
            timeout=30,
        )
        status = s.json().get("status_code", "")
        print(f"  [{attempt}] status: {status}")
        if status == "FINISHED":
            break
        if status == "EXPIRED":
            print("Container expired; aborting.")
            return 1
    else:
        print("Processing timed out; the hourly rerun will retry via caption check.")
        return 1

    # 3. Publish.
    p = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": TOKEN},
        timeout=60,
    )
    print("publish:", p.status_code, p.text[:300])
    p.raise_for_status()
    print("PUBLISHED:", p.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())

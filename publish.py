"""Instagram reel publisher for two campaigns (GitHub Actions).

Runs every 10 min; publishes each scheduled reel at its planned moment.
  - One Block series: Oct 25-31, 2026 (7 PM IST slots)
  - IShowSpeed x Expedia: Oct 25-29, 2026 at 21:00 UTC
    (US/CA/UK/AU/NZ evening-optimized: 4-5 PM ET, 9 PM UK, 8 AM AU)

Idempotent: skips when not due, already published (caption match), or the
6h window has lapsed. Multiple reels can share one date.
"""
import base64
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
STATE_FILE = "state.json"

# One Block series (7 PM IST slots)
BLOCK = [
    ("2026-10-25", "16:30", "day1.mp4", "DAY 1"),
    ("2026-10-26", "13:30", "day2.mp4", "DAY 2"),
    ("2026-10-27", "13:30", "day3.mp4", "DAY 3"),
    ("2026-10-28", "13:30", "day4.mp4", "DAY 4"),
    ("2026-10-29", "13:30", "day5.mp4", "DAY 5"),
    ("2026-10-30", "13:30", "day6.mp4", "DAY 6"),
    ("2026-10-31", "13:30", "day7.mp4", "DAY 7"),
]
# IShowSpeed x Expedia (21:00 UTC = 4-5 PM ET / 9 PM UK / 8 AM AU)
SPEED = [
    ("2026-10-25", "21:00", "speed1.mp4", "SPEED 1"),
    ("2026-10-26", "21:00", "speed2.mp4", "SPEED 2"),
    ("2026-10-27", "21:00", "speed3.mp4", "SPEED 3"),
    ("2026-10-28", "21:00", "speed4.mp4", "SPEED 4"),
    ("2026-10-29", "21:00", "speed5.mp4", "SPEED 5"),
]


# MrBeast Minecraft (15:00 UTC = 8:30 PM IST / 11 AM ET / 3 PM UK)
MC = [
    ("2026-10-27", "15:00", "mc1.mp4", "MC 1"),
    ("2026-10-28", "15:00", "mc2.mp4", "MC 2"),
    ("2026-10-29", "20:00", "mc3.mp4", "MC 3"),
    ("2026-10-30", "15:00", "mc4.mp4", "MC 4"),
    ("2026-10-31", "20:00", "mc5.mp4", "MC 5"),
]


# MrBeast - Escape 100 Cops (Nov 1-5, 2026)
COPS = [
    ("2026-11-01", "15:00", "cops1.mp4", "COPS 1"),
    ("2026-11-02", "15:00", "cops2.mp4", "COPS 2"),
    ("2026-11-03", "20:00", "cops3.mp4", "COPS 3"),
    ("2026-11-04", "15:00", "cops4.mp4", "COPS 4"),
    ("2026-11-05", "20:00", "cops5.mp4", "COPS 5"),
]

SCHEDULE = [
    {"date": d, "time": t, "file": f, "caption": c}
    for d, t, f, c in BLOCK + SPEED + MC + COPS
]

CAPTIONS = json.load(open("captions.json", encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"published": []}


def save_state(state: dict, repo: str) -> None:
    """Commit state.json back to the repo (marks the reel as done)."""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if not gh_token:
        print("No GITHUB_TOKEN; state not persisted (reruns may duplicate).")
        return
    api = f"https://api.github.com/repos/{repo}/contents/{STATE_FILE}"
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    sha = None
    existing = requests.get(api, headers=headers, timeout=30)
    if existing.status_code == 200:
        sha = existing.json().get("sha")
    body = {
        "message": f"state: publish {state['last']}",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode()
        ).decode(),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(api, headers=headers, json=body, timeout=30)
    print("state commit:", r.status_code)


def main() -> int:
    now = datetime.now(timezone.utc)
    today = FORCE_DATE or now.strftime("%Y-%m-%d")
    todays = [e for e in SCHEDULE if e["date"] == today]
    if not todays:
        print(f"No reels scheduled for {today}; nothing to do.")
        return 0

    failures = 0
    state = load_state()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    for entry in todays:
        file_name = entry["file"]
        day = entry["caption"]
        caption = CAPTIONS[day]
        publish_at = datetime.strptime(
            f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)

        if now < publish_at:
            print(f"{day} due at {entry['time']} UTC (in {publish_at - now}); not yet.")
            continue
        if now > publish_at + timedelta(hours=6):
            print(f"{day} window missed (due {publish_at}). Use FORCE_DATE to rerun.")
            failures += 1
            continue

        state_key = f"{entry['date']}|{day}"
        if state_key in state.get("published", []):
            print(f"{day}: already published (state); skipping.")
            continue
            continue

        video_url = f"{RAW_BASE.format(repo=repo)}/{file_name}"
        print(f"{day}: publishing {file_name} (due {publish_at.isoformat()}, now {now.isoformat()})")
        print(f"video_url: {video_url}")

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
            continue

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
                print("Container expired; aborting this reel.")
                failures += 1
                break
        else:
            print("Processing timed out; the 10-min rerun will retry.")
            failures += 1
            continue

        p = requests.post(
            f"{GRAPH}/{IG_USER_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": TOKEN},
            timeout=60,
        )
        print("publish:", p.status_code, p.text[:200])
        p.raise_for_status()
        print(f"PUBLISHED {day}:", p.json())
        state["published"].append(state_key)
        state["last"] = state_key
        save_state(state, repo)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

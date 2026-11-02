"""Instagram reel publisher for @thehighlight_____ (GitHub Actions).

Runs every 10 min; publishes each scheduled reel at its planned moment.

Content: paid clipping campaigns from Content Rewards. One campaign clip per
day during warm-up, never more than two per day after that.

Idempotent: skips when not due, already published (state), or the 6h window
has lapsed. Multiple reels can share one date.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# System-user (SUAT) token: publishes through the Facebook host, not
# graph.instagram.com. Verified against @thehighlight_____ on 2026-11-01.
GRAPH = "https://graph.facebook.com/v23.0"
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_TOKEN", "")
DRYRUN = os.environ.get("DRYRUN", "false").lower() == "true"
FORCE_DATE = os.environ.get("FORCE_DATE", "").strip()

RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/clips-highlight"
COVER_BASE = "https://raw.githubusercontent.com/{repo}/main/covers-highlight"
STATE_FILE = "state_highlight.json"

# Paid campaign clips (ClipFarm x Solved with Mark Manson). One per day at
# 17:00 UTC = 12:00 ET / 9:00 PT / 17:00 UK, timed for the campaign's
# US/UK/CA/AU view requirement.
HIGHLIGHT = [
    ("2026-11-01", "17:00", "mm01.mp4", "MM 01"),
    ("2026-11-02", "17:00", "mm02.mp4", "MM 02"),
    ("2026-11-03", "17:00", "mm03.mp4", "MM 03"),
    ("2026-11-04", "17:00", "mm04.mp4", "MM 04"),
    ("2026-11-05", "17:00", "mm05.mp4", "MM 05"),
]

# ClipFarm x FOMO (TOKEN2049 Singapore). Two per day at 13:00 and 21:00 UTC,
# which keeps every FOMO reel 4h clear of the 17:00 Manson slot on this same
# account, and 8h clear of each other. 13:00 UTC hits the US morning and the UK
# afternoon; 21:00 UTC hits the US afternoon and the Australian morning.
FOMO = [
    ("2026-11-02", "13:00", "fo01.mp4", "FO 01"),
    ("2026-11-02", "21:00", "fo02.mp4", "FO 02"),
    ("2026-11-03", "13:00", "fo03.mp4", "FO 03"),
    ("2026-11-03", "21:00", "fo04.mp4", "FO 04"),
    ("2026-11-04", "13:00", "fo05.mp4", "FO 05"),
    ("2026-11-04", "21:00", "fo06.mp4", "FO 06"),
    ("2026-11-05", "13:00", "fo07.mp4", "FO 07"),
]

# ClipFarm x SEGA Virtua Fighter Crossroads (Lionel Rafale trailer). Two per day
# at 13:00 and 21:00 UTC, starting once the Manson and FOMO slates clear on
# Nov 5: 13:00 hits the US morning and the UK afternoon, 21:00 the US afternoon,
# which is what the 35% combined USA+Canada audience rule needs. Each sv caption
# is the verbatim brief caption - tools/build_sega_caption.py writes them from
# the .docx so none of it is retyped - and each file carries the official logo
# burned in, because the brief requires it on every post.
SEGA = [
    ("2026-11-06", "13:00", "sv01.mp4", "sv01"),
    ("2026-11-06", "21:00", "sv02.mp4", "sv02"),
    ("2026-11-07", "13:00", "sv03.mp4", "sv03"),
    ("2026-11-07", "21:00", "sv04.mp4", "sv04"),
    ("2026-11-08", "13:00", "sv05.mp4", "sv05"),
]

SCHEDULE = [
    {"date": d, "time": t, "file": f, "caption": c}
    for d, t, f, c in (*HIGHLIGHT, *FOMO, *SEGA)
]

CAPTIONS = json.load(open("captions_highlight.json", encoding="utf-8"))


def cover_payload(repo: str, file_name: str) -> dict:
    """Use the committed 9:16 cover for this reel when one exists."""
    stem = os.path.splitext(file_name)[0]
    if not os.path.exists(os.path.join("covers-highlight", f"{stem}.jpg")):
        return {}
    return {"cover_url": f"{COVER_BASE.format(repo=repo)}/{stem}.jpg"}


def fetch_permalink(media_id: str) -> str:
    """The public post URL - the thing a Content Rewards submission asks for.

    Never raises. A reel that already published must not lose its slot, and its
    link can be filled in by the next run.
    """
    if not media_id:
        return ""
    try:
        r = requests.get(
            f"{GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": TOKEN},
            timeout=30,
        )
        return r.json().get("permalink", "")
    except (requests.RequestException, ValueError) as exc:
        print(f"  permalink lookup failed for {media_id}: {exc}")
        return ""


def backfill_links(state: dict) -> bool:
    """Record links for reels published before links were kept."""
    links = state.setdefault("links", {})
    media = state.get("media", {})
    missing = [k for k in state.get("published", []) if k not in links and media.get(k)]
    added = 0
    for key in missing[:6]:
        link = fetch_permalink(media[key])
        if link:
            links[key] = link
            added += 1
            print(f"link backfilled {key}: {link}")
    return bool(added)


def load_state() -> dict:
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"published": []}


def save_state(state: dict, repo: str) -> None:
    """Commit state file back to the repo (marks the reel as done)."""
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
        "message": f"state: {state.get('last', 'links backfilled')}",
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode()
        ).decode(),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(api, headers=headers, json=body, timeout=30)
    print("state commit:", r.status_code)


def main() -> int:
    if not TOKEN or not IG_USER_ID:
        # Not a failure: this workflow is scheduled before its secrets exist,
        # and a non-zero exit would mail the owner every 10 minutes.
        print("IG_TOKEN_HIGHLIGHT / IG_USER_ID_HIGHLIGHT secrets are not set; skipping.")
        return 0

    now = datetime.now(timezone.utc)
    today = FORCE_DATE or now.strftime("%Y-%m-%d")
    todays = [e for e in SCHEDULE if e["date"] == today]

    failures = 0
    state = load_state()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        if backfill_links(state):
            save_state(state, repo)
    except Exception as exc:
        # Bookkeeping only. A link that will not resolve must not cost a slot
        # that is due in the next few minutes.
        print(f"link backfill skipped: {exc}")

    if not todays:
        print(f"No reels scheduled for {today}; nothing to do.")
        return 0
    for entry in todays:
        file_name = entry["file"]
        day = entry["caption"]
        caption = CAPTIONS[day]
        publish_at = datetime.strptime(
            f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)

        state_key = f"{entry['date']}|{day}"
        if state_key in state.get("published", []):
            print(f"{day}: already published (state); skipping.")
            continue
        if now < publish_at:
            print(f"{day} due at {entry['time']} UTC (in {publish_at - now}); not yet.")
            continue
        if now > publish_at + timedelta(hours=6):
            print(f"{day} window missed (due {publish_at}). Use FORCE_DATE to rerun.")
            failures += 1
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
                **cover_payload(repo, file_name),
            },
            timeout=60,
        )
        print("container:", r.status_code, r.text[:300])
        if r.status_code >= 400 and "cover_url" in r.text:
            # A rejected cover must not cost us the slot; publish without it.
            print("cover rejected; retrying container without cover_url")
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
        media_id = p.json().get("id")
        # The media id is what media_insights is read by; without it a reel's
        # performance is unrecoverable once the container id expires.
        state.setdefault("media", {})[state_key] = media_id
        link = fetch_permalink(media_id)
        if link:
            state.setdefault("links", {})[state_key] = link
        print(f"PUBLISHED {day}: media_id={media_id} link={link or 'not yet resolved'}")
        state["published"].append(state_key)
        state["last"] = state_key
        save_state(state, repo)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

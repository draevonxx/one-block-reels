"""Instagram reel publisher for @thehighlight_____ (GitHub Actions).

A scheduled run stays alive and publishes each reel at its planned moment
(GitHub throttles the */10 cron to about hourly, so a one-pass run airs late).

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
# A caption label ("MM 02"): delete that reel and put it back up now, ignoring
# the ledger and the 6h window. Campaign submissions have a short clock, so a
# slot that posted while nobody was awake has to be replayed to be submittable.
REPOST = os.environ.get("REPOST", "").strip()

RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/clips-highlight"
COVER_BASE = "https://raw.githubusercontent.com/{repo}/main/covers-highlight"
STATE_FILE = "state_highlight.json"

# Paid campaign clips (ClipFarm x Solved with Mark Manson). One per day at
# 11:00 UTC = 16:30 IST, the earliest slot in the operator's waking window
# (16:00-22:00 IST = 10:30-16:30 UTC), so the reel lands while he is awake to
# submit it inside Content Rewards' 30-minute clock.
# Trade-off, accepted deliberately: 11:00 UTC is 06:00 ET / 04:00 PT, so the
# Manson audience sees it on the morning scroll rather than the US evening one
# the old 17:00 UTC slot bought.
HIGHLIGHT = [
    ("2026-11-01", "17:00", "mm01.mp4", "MM 01"),
    ("2026-11-02", "17:00", "mm02.mp4", "MM 02"),
    ("2026-11-03", "11:00", "mm03.mp4", "MM 03"),
    ("2026-11-04", "11:00", "mm04.mp4", "MM 04"),
    ("2026-11-05", "11:00", "mm05.mp4", "MM 05"),
]

# ClipFarm x FOMO (TOKEN2049 Singapore). Two per day at 13:30 and 16:00 UTC,
# both inside the 10:30-16:30 UTC waking window and 2h clear of the 11:00 Manson
# slot on this same account. 13:30 UTC hits the US morning and the UK afternoon;
# 16:00 UTC hits the US late morning and the UK evening, but no longer the
# Australian morning the old 21:00 slot covered.
FOMO = [
    ("2026-11-02", "13:00", "fo01.mp4", "FO 01"),
    ("2026-11-02", "21:00", "fo02.mp4", "FO 02"),
    ("2026-11-03", "13:00", "fo03.mp4", "FO 03"),
    ("2026-11-03", "16:00", "fo04.mp4", "FO 04"),
    ("2026-11-04", "13:30", "fo05.mp4", "FO 05"),
    ("2026-11-04", "16:00", "fo06.mp4", "FO 06"),
    ("2026-11-05", "13:30", "fo07.mp4", "FO 07"),
]

# ClipFarm x SEGA Virtua Fighter Crossroads (Lionel Rafale trailer). Two per day,
# 13:30 and 21:00 UTC, starting once the Manson and FOMO slates clear on Nov 5.
# 13:30 hits the US morning and the UK lunchtime; 21:00 is the one hour where all
# four Tier 1 regions are awake at once (US East mid-afternoon, US West lunch, UK
# 21:00 prime, Australia 07:00-08:00 commute), which is the slot that carries the
# 35% combined USA+Canada audience rule. Cost, accepted: 21:00 UTC is 02:30 IST,
# so those reels air while the operator is asleep and get submitted on waking.
# Each sv caption is the verbatim brief caption - tools/build_sega_caption.py
# writes them from the .docx so none of it is retyped - and each file carries the
# official logo burned in, because the brief requires it on every post.
SEGA = [
    ("2026-11-06", "13:30", "sv01.mp4", "sv01"),
    # sv02 was cut from this lane on 2026-11-06 and moved to publish.py's SV list
    # (@draevonxx) instead of being duplicated here: the brief files "the same video
    # on multiple accounts" under engagement fraud.
    ("2026-11-07", "13:30", "sv03.mp4", "sv03"),
    ("2026-11-07", "21:00", "sv04.mp4", "sv04"),
    ("2026-11-08", "13:30", "sv05.mp4", "sv05"),
]

# Call of Duty MW4 Season 1 trailer clipping. One per day, 16:00 UTC = 21:30 IST.
# The two Call of Duty lanes are 5h apart (16:00 and 21:00) rather than the 2h they
# first carried: same-day posts on one account compete for the same followers'
# scroll, and a 2h gap is close enough that the second one lands on the audience
# that already saw the first.
# Both slots are picked for a Tier 1 majority, not for the operator's schedule -
# see the WZ comment for why the old 10:30-16:30 UTC waking window no longer binds.
# 16:00 UTC = 11:00 US East, 08:00 US West, 16:00 UK: three of the four Tier 1
# regions mid-day and awake, which covers the combined USA+Canada share the brief
# cares about. Australia is at 03:00 and asleep, accepted because the second slot
# four hours later catches its morning.
# The brief bans violent content outright, so every cut window was chosen off a
# per-frame red-share scan of the source and excludes all nine measured blood
# bursts, not just the graphic ones. It also bans calling a trailer "gameplay",
# which is why no caption or headline uses the word.
COD = [
    ("2026-11-09", "16:00", "cod1.mp4", "cod1"),
    ("2026-11-10", "16:00", "cod2.mp4", "cod2"),
    ("2026-11-11", "16:00", "cod3.mp4", "cod3"),
    ("2026-11-12", "16:00", "cod4.mp4", "cod4"),
    ("2026-11-13", "16:00", "cod5.mp4", "cod5"),
]

# Call of Duty Warzone Season 1 trailer clipping. Instagram only: the campaign's own
# platforms list is ["instagram","tiktok"], so there is no YouTube surface to pay out on
# and no queue row for it in cr_yt_queue.json. One per day at 21:00 UTC = 02:30 IST,
# 5h clear of the 16:00 COD rows above.
# 21:00 UTC is the one hour where all four Tier 1 regions are awake at once - 16:00
# US East, 13:00 US West, 21:00 UK prime, 08:00 Australia commute - which is what
# carries the brief's "audience mostly Tier 1-2, over 30% Tier 3 gets removed" rule.
# It is the same reasoning sv04 used; what changed since then is the cost. sv04 was
# submitted by hand after waking because Content Rewards' clock is ~29 min from the
# reel's own publish time. That clock is now met by cr_submit.py unattended - the
# ledger holds submissions at 21:33 and 00:03 UTC - so the 10:30-16:30 UTC waking
# window that the earlier lanes were all constrained to no longer has to pick the
# slot, and reach can. The publisher is on an hourly clock, so a slot is only safe
# if the submitter's hourly run lands inside it, which it does.
# Cut from a different source file than the COD lane (the Warzone trailer, not the MW4
# one, and no sampled frame of either matches any frame of the other), so the two lanes
# are not posting the same footage to the same account.
# The brief bans explicit content, so every window here was chosen off a per-frame
# red-share scan of the trailer and excludes all six measured blood bursts, and the
# picture is the competitors' own 4:3 punch-in inside 9:16 rather than a full-bleed
# reframe - which is also why the crop was checked frame by frame: a 4:3 window throws
# away 480px at each edge, and the trailer's own "ALL OUT WARZONE" card came out of it
# reading "VARZONE".
WZ = [
    ("2026-11-09", "21:00", "wz1.mp4", "wz1"),
    ("2026-11-10", "21:00", "wz2.mp4", "wz2"),
    ("2026-11-11", "21:00", "wz3.mp4", "wz3"),
    ("2026-11-12", "21:00", "wz4.mp4", "wz4"),
    ("2026-11-13", "21:00", "wz5.mp4", "wz5"),
]

SCHEDULE = [
    {"date": d, "time": t, "file": f, "caption": c}
    for d, t, f, c in (*HIGHLIGHT, *FOMO, *SEGA, *COD, *WZ)
]

CAPTIONS = json.load(open("captions_highlight.json", encoding="utf-8"))


def cover_payload(repo: str, file_name: str) -> dict:
    """Use the committed 9:16 cover for this reel when one exists."""
    stem = os.path.splitext(file_name)[0]
    if not os.path.exists(os.path.join("covers-highlight", f"{stem}.jpg")):
        return {}
    return {"cover_url": f"{COVER_BASE.format(repo=repo)}/{stem}.jpg"}


def delete_media(media_id: str) -> bool:
    """Delete a reel this publisher put up, so a repost can replace it.

    The Content Publishing API only lets you delete what the API itself created,
    which is every reel in this ledger. Returns False on any doubt - a repost
    that cannot prove the old post is gone must not create a second copy.
    """
    if not media_id:
        return False
    try:
        r = requests.delete(
            f"{GRAPH}/{media_id}", params={"access_token": TOKEN}, timeout=60
        )
    except requests.RequestException as exc:
        print(f"  delete request failed for {media_id}: {exc}")
        return False
    print("delete:", r.status_code, r.text[:200])
    if r.status_code >= 400:
        return False
    try:
        return bool(r.json().get("success", True))
    except ValueError:
        return True


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


def fetch_remote_state(repo: str):
    """The authoritative ledger as committed on main, or None if unreadable.

    A run that slept across a slot holds the ledger from its own start. Reading
    the remote copy right before publishing is what stops two overlapping runs
    from both airing the same reel. None means "could not verify" - the caller
    falls back to its in-memory state rather than skipping a due slot blind.
    """
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if not gh_token or not repo:
        return None
    api = f"https://api.github.com/repos/{repo}/contents/{STATE_FILE}"
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(api, headers=headers, timeout=30)
        if r.status_code != 200:
            return None
        content = r.json().get("content", "")
        return json.loads(base64.b64decode(content).decode("utf-8"))
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"  remote state read failed: {exc}")
        return None


def merge_remote(state: dict, live: dict) -> None:
    """Adopt entries another run committed while we slept, without losing ours."""
    pub = state.setdefault("published", [])
    for key in live.get("published", []):
        if key not in pub:
            pub.append(key)
    for field in ("media", "links"):
        state.setdefault(field, {}).update(live.get(field, {}))
    if live.get("last"):
        state["last"] = live["last"]


# GitHub only starts a scheduled run here about once an hour however often the
# cron ticks, so a one-pass script airs every slot late - and a late airing can
# blow the Content Rewards window, which is counted from the scheduled moment.
# Like cr_submit.py, the scheduled path stays alive and sleeps to each slot
# instead of trusting the next wake. The horizon spans a working evening; the
# workflow timeout outlives it.
HORIZON_MINUTES = 300
JOB_BUDGET_MINUTES = 320


def slot_time(entry: dict) -> datetime:
    return datetime.strptime(
        f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)


def next_due(state: dict, at: datetime):
    """Earliest still-unpublished slot after `at`, across the whole schedule."""
    pub = set(state.get("published", []))
    future = [slot_time(e) for e in SCHEDULE
              if f"{e['date']}|{e['caption']}" not in pub and slot_time(e) > at]
    return min(future) if future else None


def publish_entry(entry: dict, state: dict, repo: str, now: datetime,
                  force: bool = False) -> str:
    """Publish one scheduled slot. Returns published/skip/not_yet/missed/failed/dryrun.

    force (repost) bypasses the ledger and time guards: the reel was just taken
    down and must go back up now regardless of its original slot.
    """
    file_name = entry["file"]
    day = entry["caption"]
    caption = CAPTIONS[day]
    publish_at = slot_time(entry)
    state_key = f"{entry['date']}|{day}"

    if not force:
        if state_key in state.get("published", []):
            print(f"{day}: already published (state); skipping.")
            return "skip"
        if now < publish_at:
            print(f"{day} due at {entry['time']} UTC (in {publish_at - now}); not yet.")
            return "not_yet"
        if now > publish_at + timedelta(hours=6):
            print(f"{day} window missed (due {publish_at}). Use FORCE_DATE to rerun.")
            return "missed"

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
        return "dryrun"

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
            return "failed"
    else:
        print("Processing timed out; the next run will retry.")
        return "failed"

    # Last reversible point: the reel goes public on media_publish, not on the
    # container upload above. A run that slept across this slot can start its
    # container before another run commits the ledger, so the live re-check has
    # to sit here. If the slot is already aired, let this container expire unused
    # rather than post a second copy.
    if not force:
        live = fetch_remote_state(repo)
        if live is not None:
            merge_remote(state, live)
            if state_key in live.get("published", []):
                print(f"{day}: already published by another run (remote ledger); "
                      "dropping this container, not publishing.")
                return "skip"
            print(f"{day}: ledger re-checked before publish "
                  f"({len(live.get('published', []))} slots already in the ledger)")

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
    return "published"


def main() -> int:
    if not TOKEN or not IG_USER_ID:
        # Not a failure: this workflow is scheduled before its secrets exist,
        # and a non-zero exit would mail the owner every 10 minutes.
        print("IG_TOKEN_HIGHLIGHT / IG_USER_ID_HIGHLIGHT secrets are not set; skipping.")
        return 0

    if REPOST and DRYRUN:
        # Dry run means "do not publish", but the delete would already be done.
        print("REPOST with dry-run would delete the live reel and not replace it; refusing.")
        return 1
    if REPOST and not FORCE_DATE:
        print(f"REPOST {REPOST}: pass the date that reel is scheduled for as well.")
        return 1

    state = load_state()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        if backfill_links(state):
            save_state(state, repo)
    except Exception as exc:
        # Bookkeeping only. A link that will not resolve must not cost a slot
        # that is due in the next few minutes.
        print(f"link backfill skipped: {exc}")

    # Manual dispatch (repost / force date / dry run) keeps the old single pass.
    if REPOST or FORCE_DATE or DRYRUN:
        now = datetime.now(timezone.utc)
        today = FORCE_DATE or now.strftime("%Y-%m-%d")
        todays = [e for e in SCHEDULE if e["date"] == today]
        if not todays:
            print(f"No reels scheduled for {today}; nothing to do.")
            return 0
        failures = 0
        replayed = 0
        for entry in todays:
            day = entry["caption"]
            state_key = f"{entry['date']}|{day}"
            if REPOST:
                if day != REPOST:
                    continue
                replayed += 1
                old = state.get("media", {}).get(state_key, "")
                print(f"REPOST {day}: taking down {old or 'nothing (no media id recorded)'}")
                if old and not delete_media(old):
                    print("REPOST aborted: the live reel is still up, so nothing new goes out.")
                    return 1
                state["published"] = [k for k in state.get("published", []) if k != state_key]
                state.get("links", {}).pop(state_key, None)
                res = publish_entry(entry, state, repo, datetime.now(timezone.utc), force=True)
            else:
                res = publish_entry(entry, state, repo, now)
            if res in ("missed", "failed"):
                failures += 1
        if REPOST and not replayed:
            print(f"REPOST {REPOST!r} matched nothing on {today}; check the label and the date.")
            return 1
        return 1 if failures else 0

    # Scheduled: stay alive and fire each slot on time rather than waiting for the
    # next hourly wake, which airs everything late.
    start = datetime.now(timezone.utc)
    budget = start + timedelta(minutes=min(JOB_BUDGET_MINUTES, HORIZON_MINUTES))
    failures = 0
    missed_seen = set()
    while True:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        for entry in [e for e in SCHEDULE if e["date"] == today]:
            res = publish_entry(entry, state, repo, now)
            if res == "failed":
                failures += 1
            elif res == "missed":
                key = f"{entry['date']}|{entry['caption']}"
                if key not in missed_seen:
                    missed_seen.add(key)
                    failures += 1
        nxt = next_due(state, now)
        if nxt is None:
            print("no further scheduled slots; done.")
            break
        if nxt > budget:
            print(f"next slot {nxt:%H:%M} UTC is past this run's {HORIZON_MINUTES} min "
                  "budget; leaving it to the next scheduled run")
            break
        wait = (nxt - datetime.now(timezone.utc)).total_seconds()
        if wait <= 0:
            continue
        print(f"staying alive {int(wait)}s until {nxt:%H:%M} UTC "
              f"({(nxt - start).total_seconds() / 60:.0f} min into this run)", flush=True)
        time.sleep(wait)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""File each campaign clip with Content Rewards while its window is still open.

Content Rewards accepts a clip with POST /api/submission/submissions and a body of
exactly {campaignId, url}, and it keeps the slot open for about 29 minutes from the
moment the post actually goes public. That window is the whole reason this runs on a
schedule instead of by hand.

Two lanes, found two different ways:

  Instagram  the platform crawls the linked account itself, so candidate-videos tells
             us what is new, when it aired and whether it is still submittable. The
             ledger decides which campaign a label belongs to, never that list: a new
             reel appears as a candidate under every joined campaign, so trusting the
             first list that contains it would file a Manson clip against SEGA.

  YouTube    the platform's crawler reports unavailable/quota for our channels, so
             nothing is ever discovered. These come from cr_yt_queue.json and go in
             through the paste-a-link path - and before sending, the clip has to be
             confirmed public, because a link that resolves to nothing is the one
             mistake here that can cost the clip outright.

State lives in cr_submitted.json and is committed back to this repo, so the next run
cannot repeat a submission. Auth is the CR_COOKIE secret: a browser session exported
once, which expires on Whop's own clock. When it does, this prints one line and stops
rather than retrying.

Env: CR_COOKIE (required)  CR_LIVE  "true" to actually POST, anything else to report
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://contentrewards.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

IG_STATE = "state_highlight.json"
YT_QUEUE = "cr_yt_queue.json"
LEDGER = "cr_submitted.json"

# Joined campaigns, read off /c/campaigns on 2026-11-06. A fourth campaign is joined
# to this account ("$20,000 CORE Boys At Shibuya Crossing", Reach) and nothing here
# maps to it, so it can never be filed to by accident.
CAMPAIGNS = {
    "sega": "8b79c473-d295-4901-8aee-e43c7ad03663",
    "manson": "6bdc8e4b-3ebf-44f1-aedb-067589ca9c89",
    "fomo": "2fb7df23-c446-4aa0-a8db-228015f782f8",
}

# Which campaign each reel prefix belongs to. The label is the only authority.
IG_PREFIX = {"MM": "manson", "FO": "fomo", "sv": "sega"}

MAX_AGE_MINUTES = 30
MARGIN_SECONDS = 60
PUBLIC_PAD_SECONDS = 180
# A queue entry older than this and still unconfirmed is a scheduling bug, not a clip
# that is about to air; stop chasing it so the report stays honest.
STALE_AFTER_HOURS = 24

# States in which the platform will not take the post again.
DEAD = {"too_old", "submitted_here", "used_elsewhere", "expired", "rejected"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_at(raw):
    if not raw or raw == "immediate":
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path, default):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return default


class Session:
    """One cookie-authenticated client. trust_env stays off so nothing on the
    runner's proxy settings can be consulted with a credential in the headers."""

    def __init__(self, cookie: str):
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({
            "user-agent": UA,
            "accept": "application/json",
            "origin": BASE,
            "referer": f"{BASE}/c/submissions",
            "cookie": cookie,
        })

    def candidates(self, campaign_id: str):
        r = self.s.get(f"{BASE}/api/submission/submissions/candidate-videos",
                       params={"campaignId": campaign_id}, timeout=30)
        if r.status_code != 200:
            return None, r.status_code
        return (r.json().get("data") or {}).get("videos") or [], 200

    def submit(self, campaign_id: str, url: str):
        r = self.s.post(f"{BASE}/api/submission/submissions",
                        json={"campaignId": campaign_id, "url": url}, timeout=30)
        return r.status_code, (r.text or "")[:200]


def is_public(video_id: str, log) -> bool:
    """oEmbed answers 200 only for a video that exists and is viewable.

    404 is private or deleted, so we hold off and try again next run; 401 is the
    embedding-disabled case, which is ambiguous enough that it gets reported instead
    of guessed at, because guessing wrong here can cost the clip.
    """
    r = requests.get("https://www.youtube.com/oembed",
                     params={"url": f"https://www.youtube.com/watch?v={video_id}",
                             "format": "json"}, timeout=20)
    if r.status_code == 200:
        return True
    if r.status_code == 401:
        log.append(f"oembed 401 for {video_id} - not embeddable, treated as unconfirmed")
    return False


def campaign_of(label: str):
    head = label[:2].upper()
    if head in ("MM", "FO"):
        return IG_PREFIX[head]
    if label[:2].lower() == "sv":
        return IG_PREFIX["sv"]
    return None


def ig_targets(state: dict, done: set[str], at: datetime):
    links = state.get("links") or {}
    out = []
    for key, url in sorted(links.items()):
        day, _, label = key.partition("|")
        campaign = campaign_of(label)
        code = url.rstrip("/").split("/")[-1]
        if campaign is None or code in done:
            continue
        try:
            aired = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # The window opens when the reel airs and cannot outlive that day, so a link
        # from an earlier day is closed for good - not retried forever.
        if aired.date() < at.date():
            continue
        out.append({"kind": "instagram", "campaign": campaign, "label": label,
                    "postId": code, "url": url})
    return out


def yt_targets(queue: dict, done: set[str], at: datetime):
    out, unknown = [], []
    for row in queue.get("items") or []:
        vid = row.get("videoId")
        if not vid or vid in done:
            continue
        air = parse_at(row.get("airAt"))
        if air is None or air + timedelta(seconds=PUBLIC_PAD_SECONDS) > at:
            continue
        if at - air > timedelta(hours=STALE_AFTER_HOURS):
            continue
        campaign = row.get("campaign")
        if campaign not in CAMPAIGNS:
            unknown.append(f"{row.get('label')}: unknown campaign {campaign}")
            continue
        out.append({"kind": "youtube", "campaign": campaign,
                    "campaignId": CAMPAIGNS[campaign], "label": row.get("label"),
                    "postId": vid, "url": row.get("url")})
    for u in unknown:
        print("  -", u)
    return out


def commit(path: str, obj, message: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY: state not persisted, a rerun may repeat")
        return
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    sha = None
    existing = requests.get(api, headers=headers, timeout=30)
    if existing.status_code == 200:
        sha = existing.json().get("sha")
    body = {"message": message,
            "content": base64.b64encode(json.dumps(obj, indent=1, ensure_ascii=False).encode()).decode()}
    if sha:
        body["sha"] = sha
    print("ledger commit:", requests.put(api, headers=headers, json=body, timeout=30).status_code)


def main() -> int:
    cookie = os.environ.get("CR_COOKIE", "").strip()
    if not cookie:
        # Not a failure: the workflow is scheduled before the secret exists, and a
        # non-zero exit would mail the owner every ten minutes.
        print("CR_COOKIE is not set; nothing submitted.")
        return 0

    live = os.environ.get("CR_LIVE", "").strip().lower() == "true"
    at = now()
    ses = Session(cookie)
    rows = read_json(LEDGER, [])
    done = {r.get("postId") for r in rows if r.get("postId")}
    queue = read_json(YT_QUEUE, {})
    state = read_json(IG_STATE, {})

    ig, yt = ig_targets(state, done, at), yt_targets(queue, done, at)

    # Always read one campaign, even when nothing looks due: this is what tells the
    # owner the exported session still works from this IP. Without it, a quiet day and
    # a dead cookie print the same line and you only find out at the first air slot.
    vids, code = ses.candidates(CAMPAIGNS["fomo"])
    if vids is None:
        print(f"AUTH FAILED ({code}) - CR_COOKIE no longer works; re-export it.")
        return 0
    lists = {CAMPAIGNS["fomo"]: {v.get("postId"): v for v in vids}}
    print(f"session ok from this IP at {at:%H:%M} UTC; {len(vids)} posts listed for fomo")

    if not ig and not yt:
        print(f"nothing due (ledger {len(rows)}, queue {len(queue.get('items') or [])} rows)")
        return 0

    notes, sent = [], 0
    for t in ig:
        cid = CAMPAIGNS[t["campaign"]]
        if cid not in lists:
            vids, code = ses.candidates(cid)
            if vids is None:
                print(f"AUTH FAILED reading candidates for {t['campaign']} ({code}); "
                      "re-export the cookie.")
                return 0
            lists[cid] = {v.get("postId"): v for v in vids}
        v = lists[cid].get(t["postId"])
        if v is None:
            notes.append(f"{t['label']} not indexed yet")
            continue
        submit_by = parse_at(v.get("submitBy"))
        if v.get("state") in DEAD or not submit_by or submit_by <= at:
            notes.append(f"{t['label']} closed ({v.get('state')})")
            continue
        t["campaignId"] = cid
        if not live:
            notes.append(f"[dry] would submit {t['campaign']}/{t['label']}")
            continue
        status, reply = ses.submit(cid, t["url"])
        if status in (200, 201):
            sent += 1
            rows.append({"campaign": t["campaign"], "campaignId": cid, "postId": t["postId"],
                         "platform": "instagram", "url": t["url"],
                         "publishedAt": v.get("publishedAt"),
                         "submittedAt": at.isoformat(timespec="seconds").replace("+00:00", "Z")})
            done.add(t["postId"])
            print(f"SUBMITTED {t['campaign']}/{t['label']} ig {t['url']}")
        else:
            notes.append(f"{t['label']} refused {status} {reply[:90]}")

    for t in yt:
        if not is_public(t["postId"], notes):
            notes.append(f"{t['label']} not public yet")
            continue
        if not live:
            notes.append(f"[dry] would submit {t['campaign']}/{t['label']} yt")
            continue
        status, reply = ses.submit(t["campaignId"], t["url"])
        if status in (200, 201):
            sent += 1
            rows.append({"campaign": t["campaign"], "campaignId": t["campaignId"],
                         "postId": t["postId"], "platform": "youtube", "url": t["url"],
                         "submittedAt": at.isoformat(timespec="seconds").replace("+00:00", "Z")})
            print(f"SUBMITTED {t['campaign']}/{t['label']} yt {t['url']}")
        else:
            notes.append(f"{t['label']} refused {status} {reply[:90]}")

    for n in notes:
        print("  -", n)
    if sent and live:
        commit(LEDGER, rows, f"cr: {sent} submission(s)")
    elif not live:
        print(f"dry run: {len(ig)} instagram + {len(yt)} youtube candidate(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

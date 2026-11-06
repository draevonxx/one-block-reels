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

Why a run waits instead of exiting. GitHub starts a scheduled run on this repo about
once an hour, not once every ten minutes - the cron is committed and real, but the
scheduler is throttled here (measured 2026-11-06 across this repo's three scheduled
workflows: ~25 runs a day each, and no two runs closer together than 32 minutes). A
29-minute window cannot be caught by hoping a run happens to start inside it. So a run
that can see a clip about to air stays alive, sleeps until the window opens, submits
then, and re-checks a clip that has aired but is not yet public or not yet indexed. The
ledger is committed after each accepted submission rather than once at the end, because
a run killed by the job timeout mid-sleep must not leave an unrecorded submission for
the next runner to repeat - YouTube has no platform-side dedupe.

Env: CR_COOKIE (required)  CR_LIVE  "true" to actually POST, anything else to report
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://contentrewards.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

IG_STATE = "state_highlight.json"
# The organic page carries campaign reels too now (the brief allows any page), so its
# ledger is a second source of Instagram permalinks.
IG_STATE_ORGANIC = "state.json"
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

# How long one run stays alive to catch slots, and how soon it looks again at a clip
# that has aired but is not visible to us yet. Both sit under the job timeout, so a run
# ends on its own terms having committed everything it filed.
HORIZON_MINUTES = 90
JOB_BUDGET_MINUTES = 100
RETRY_SECONDS = 120

# States in which the platform will not take the post again.
DEAD = {"too_old", "submitted_here", "used_elsewhere", "expired", "rejected"}

# The Instagram reel's own URL only exists once publish_highlight.py commits it, and the
# publisher is on the same hourly clock, so the reel can appear up to an hour after its
# planned minute. These bound how long a run keeps watching for one that is overdue.
IG_INDEX_SECONDS = 240
IG_WATCH_AFTER_HOURS = 3

GIVE_UP = object()


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


def ledger_links(path: str) -> dict:
    """One publisher's permalinks, as they stand on the default branch right now.

    The checkout is a snapshot taken the moment this run started, so a reel the
    publisher posts while this run sleeps would never appear if we kept re-reading the
    working copy. raw.githubusercontent needs no credential and cannot leak the cookie.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if repo:
        try:
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}",
                             timeout=30)
            if r.status_code == 200:
                return (json.loads(r.text) or {}).get("links") or {}
        except (ValueError, requests.RequestException):
            pass
    return (read_json(path, {}) or {}).get("links") or {}


def live_state() -> dict:
    """Both publishers' links in one map keyed "date|label".

    Campaign reels now air on two Instagram pages, and only the label decides which
    campaign each one belongs to, so a reel recorded by the organic publisher is as
    submittable as one recorded by the campaign page.
    """
    links = ledger_links(IG_STATE)
    links.update(ledger_links(IG_STATE_ORGANIC))
    return {"links": links}


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

    404 is private or deleted, so we hold off and look again shortly; 401 is the
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
                    "campaignId": CAMPAIGNS[campaign], "postId": code, "url": url})
    return out


def ig_upcoming(queue: dict, state: dict, at: datetime):
    """Campaign reels scheduled for today that the publisher has not recorded yet.

    Instagram gives the runner nothing to work from until the link is committed, and the
    publisher wakes on the same throttled hourly clock - so a reel planned for 13:30 can
    post at 14:20. Without this the runner sees an empty inbox, exits, and the next run
    starts after that reel's window has already shut.
    """
    links = state.get("links") or {}
    out = []
    for row in queue.get("igWatch") or []:
        air = parse_at(row.get("airAt"))
        label = row.get("label")
        if air is None or not label or air.date() != at.date():
            continue
        if at - air > timedelta(hours=IG_WATCH_AFTER_HOURS):
            continue
        if any(k.endswith(f"|{label}") for k in links):
            continue
        out.append({"label": label, "campaign": row.get("campaign"),
                    "openAt": air + timedelta(seconds=IG_INDEX_SECONDS)})
    return out


def yt_targets(queue: dict, done: set[str], at: datetime):
    """Split the queue into ready now, still waiting to open, and closed for good.

    Ready and pending rows carry openAt (the first moment the clip can be proved
    public) and deadline (the last second the platform will take it). Those two are what
    let the caller sleep to an opening instead of waiting for some later run to find it.
    """
    ready, pending, closed, unknown = [], [], [], []
    for row in queue.get("items") or []:
        vid = row.get("videoId")
        if not vid or vid in done:
            continue
        air = parse_at(row.get("airAt"))
        if air is None:
            continue
        campaign = row.get("campaign")
        if campaign not in CAMPAIGNS:
            unknown.append(f"{row.get('label')}: unknown campaign {campaign}")
            continue
        t = {"kind": "youtube", "campaign": campaign, "campaignId": CAMPAIGNS[campaign],
             "label": row.get("label"), "postId": vid, "url": row["url"],
             "openAt": air + timedelta(seconds=PUBLIC_PAD_SECONDS),
             "deadline": air + timedelta(minutes=MAX_AGE_MINUTES)
                         - timedelta(seconds=MARGIN_SECONDS)}
        if t["deadline"] <= at:
            if at - air <= timedelta(hours=STALE_AFTER_HOURS):
                closed.append(t)
            continue
        (pending if t["openAt"] > at else ready).append(t)
    for u in unknown:
        print("  -", u)
    return ready, pending, closed


def record(work: dict, t: dict, at: datetime, published_at=None) -> None:
    """Fold one accepted submission into the in-memory ledger and say so."""
    work["rows"].append({
        "campaign": t["campaign"], "campaignId": t["campaignId"], "postId": t["postId"],
        "platform": t["kind"], "url": t["url"], "publishedAt": published_at,
        "submittedAt": at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    })
    work["done"].add(t["postId"])
    work["sent"] += 1
    print(f"SUBMITTED {t['campaign']}/{t['label']} {t['kind']} {t['url']}", flush=True)


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


def pass_once(ses: Session, live: bool, work: dict, at: datetime):
    """Evaluate both lanes at clock time `at`.

    Returns the next instant worth waking for, None when there is nothing left to wait
    on, or GIVE_UP when the session is dead and no further pass can succeed.
    """
    rows, done = work["rows"], work["done"]
    queue, state = read_json(YT_QUEUE, {}), live_state()
    was_first = work["first"]

    # Always read one campaign, even when nothing looks due: this is what tells the
    # owner the exported session still works from this IP. Without it, a quiet day and
    # a dead cookie print the same line and you only find out at the first air slot.
    vids, code = ses.candidates(CAMPAIGNS["fomo"])
    if vids is None:
        print(f"AUTH FAILED ({code}) - CR_COOKIE no longer works; re-export it.")
        return GIVE_UP
    lists = {CAMPAIGNS["fomo"]: {v.get("postId"): v for v in vids}}
    if was_first:
        work["first"] = False
        print(f"session ok from this IP at {at:%H:%M} UTC; {len(vids)} posts listed for fomo; "
              f"mode={'LIVE, will submit' if live else 'read-only, reports only'}", flush=True)

    ig = ig_targets(state, done, at)
    yt, later, closed = yt_targets(queue, done, at)
    due_ig = ig_upcoming(queue, state, at)

    if not ig and not yt and not later and not closed and not due_ig:
        if was_first:
            print(f"nothing due (ledger {len(rows)}, "
                  f"queue {len(queue.get('items') or [])} rows)")
        return None

    notes = [f"{t['label']} window closed - the platform will not take it now" for t in closed]
    notes += [f"{t['label']} reel is not published/recorded yet (planned "
              f"{t['openAt'] - timedelta(seconds=IG_INDEX_SECONDS):%H:%M} UTC) - watching"
              for t in due_ig if t["label"] not in work["watched"]]
    work["watched"].update(t["label"] for t in due_ig)
    retry = []

    for t in ig:
        cid = t["campaignId"]
        if cid not in lists:
            vids, code = ses.candidates(cid)
            if vids is None:
                print(f"AUTH FAILED reading candidates for {t['campaign']} ({code}); "
                      "re-export the cookie.")
                return GIVE_UP
            lists[cid] = {v.get("postId"): v for v in vids}
        v = lists[cid].get(t["postId"])
        if v is None:
            notes.append(f"{t['label']} not indexed by the platform yet")
            retry.append(t)
            continue
        submit_by = parse_at(v.get("submitBy"))
        if v.get("state") in DEAD or not submit_by or submit_by <= at:
            notes.append(f"{t['label']} closed ({v.get('state')})")
            continue
        if not live:
            notes.append(f"[dry] would submit {t['campaign']}/{t['label']}")
            continue
        status, reply = ses.submit(cid, t["url"])
        if status in (200, 201):
            record(work, t, at, published_at=v.get("publishedAt"))
            commit(LEDGER, rows, f"cr: {t['campaign']}/{t['label']}")
        else:
            notes.append(f"{t['label']} refused {status} {reply[:90]}")

    for t in yt:
        if not is_public(t["postId"], notes):
            notes.append(f"{t['label']} aired but not public yet")
            retry.append(t)
            continue
        if not live:
            notes.append(f"[dry] would submit {t['campaign']}/{t['label']} yt")
            continue
        status, reply = ses.submit(t["campaignId"], t["url"])
        if status in (200, 201):
            record(work, t, at)
            commit(LEDGER, rows, f"cr: {t['campaign']}/{t['label']}")
        else:
            notes.append(f"{t['label']} refused {status} {reply[:90]}")

    for n in notes:
        print("  -", n, flush=True)
    if not live:
        print(f"dry run: {len(ig)} instagram + {len(yt)} youtube candidate(s), "
              f"{len(later)} waiting to open")
        return None

    # Wake for the next slot that has not opened, and sooner for anything just tried
    # that we could not see yet - never later than that row's own deadline, so a clip
    # whose window is about to shut gets a real last attempt instead of a missed one.
    wake = [t["openAt"] for t in later]
    wake += [max(t["openAt"], at + timedelta(seconds=RETRY_SECONDS)) for t in due_ig]
    for t in retry:
        nxt = at + timedelta(seconds=RETRY_SECONDS)
        end = t.get("deadline")
        if end is not None:
            nxt = min(nxt, end - timedelta(seconds=15))
        wake.append(nxt)
    wake = [w for w in wake if w > at]
    return min(wake) if wake else None


def main() -> int:
    cookie = os.environ.get("CR_COOKIE", "").strip()
    if not cookie:
        # Not a failure: the workflow is scheduled before the secret exists, and a
        # non-zero exit would mail the owner every run.
        print("CR_COOKIE is not set; nothing submitted.")
        return 0

    live = os.environ.get("CR_LIVE", "").strip().lower() == "true"
    ses = Session(cookie)
    rows = read_json(LEDGER, [])
    work = {"rows": rows, "done": {r.get("postId") for r in rows if r.get("postId")},
            "sent": 0, "first": True, "watched": set()}
    start = now()
    budget = start + timedelta(minutes=min(JOB_BUDGET_MINUTES, HORIZON_MINUTES))

    while True:
        nxt = pass_once(ses, live, work, now())
        if nxt is GIVE_UP or nxt is None:
            break
        if nxt > budget:
            print(f"next opening is {nxt:%H:%M} UTC, past this run's {HORIZON_MINUTES} min "
                  "budget; leaving it to the next scheduled run")
            break
        wait = (nxt - now()).total_seconds()
        if wait <= 0:
            continue
        print(f"staying alive {int(wait)}s until {nxt:%H:%M} UTC "
              f"({(nxt - start).total_seconds() / 60:.0f} min into this run)", flush=True)
        time.sleep(wait)

    print(f"done: {work['sent']} submitted, ledger {len(rows)} rows at {now():%H:%M} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())

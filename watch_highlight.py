"""Fail loudly when a scheduled highlight reel has not published (GitHub Actions).

Read-only watchdog for publish_highlight.py. It runs on its own cron and exits
non-zero while a due reel is missing from the publish ledger, so a GitHub
notification replaces someone having to check by hand.

The schedule is parsed out of publish_highlight.py rather than duplicated here,
so the two can never drift apart.
"""
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PUBLISHER = Path("publish_highlight.py")
STATE_FILE = Path("state_highlight.json")

# The */10 cron only actually fires every 30-90 min on the free tier, and a
# publish takes a few minutes, so a reel is not "late" until it is this stale.
ALERT_AFTER = timedelta(hours=2)
# Mirrors publish_highlight.py, which stops posting a reel once this has passed.
PUBLISH_WINDOW = timedelta(hours=6)


def scheduled_entries(source: Path) -> list[dict]:
    """Read the HIGHLIGHT and FOMO tuples straight out of the publisher."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    rows = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        if node.targets[0].id in ("HIGHLIGHT", "FOMO"):
            rows[node.targets[0].id] = ast.literal_eval(node.value)
    missing = {"HIGHLIGHT", "FOMO"} - set(rows)
    if missing:
        raise SystemExit(
            f"{source} no longer defines {sorted(missing)}; update this watchdog"
        )
    return [
        {"date": d, "time": t, "file": f, "label": label}
        for entries in rows.values()
        for d, t, f, label in entries
    ]


def due_at(entry: dict) -> datetime:
    return datetime.strptime(
        f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        published = set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("published", []))
    except (OSError, ValueError):
        published = set()

    late, missed, pending, done = [], [], [], []
    for entry in scheduled_entries(PUBLISHER):
        key = f"{entry['date']}|{entry['label']}"
        publish_at = due_at(entry)
        if key in published:
            done.append((key, publish_at))
        elif now >= publish_at + PUBLISH_WINDOW:
            missed.append((key, publish_at))
        elif now >= publish_at + ALERT_AFTER:
            late.append((key, publish_at))
        elif now >= publish_at:
            pending.append((key, publish_at))

    print(f"watchdog at {now.isoformat()}  (ledger holds {len(published)} entries)")
    print(f"  published ............. {len(done)}")
    for key, at in sorted(done, key=lambda r: r[1])[-3:]:
        print(f"    ok      {key}  due {at:%Y-%m-%d %H:%M}")
    for key, at in sorted(pending, key=lambda r: r[1]):
        mins = (now - at).total_seconds() / 60
        print(f"    waiting {key}  due {at:%H:%M} UTC ({mins:.0f} min ago, next cron tick)")
    for key, at in sorted(late, key=lambda r: r[1]):
        hrs = (now - at).total_seconds() / 3600
        print(f"    LATE    {key}  due {at:%H:%M} UTC, {hrs:.1f} h overdue")
    for key, at in sorted(missed, key=lambda r: r[1]):
        hrs = (now - at).total_seconds() / 3600
        print(f"    MISSED  {key}  due {at:%H:%M} UTC, {hrs:.1f} h ago - outside the "
              f"{PUBLISH_WINDOW.total_seconds() / 3600:.0f} h window")

    if not (late or missed):
        print("nothing overdue; publishing pipeline is on schedule.")
        return 0

    print()
    if missed:
        print(f"{len(missed)} reel(s) fell outside the publish window. Re-run "
              "publish-highlight.yml with the date input set to that date, or the "
              "slot is lost.")
    if late:
        print(f"{len(late)} reel(s) are more than {ALERT_AFTER} behind schedule. "
              "Read the last run log of publish-highlight.yml: a green run can mean "
              "the script skipped on absent secrets, not that the reel posted.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

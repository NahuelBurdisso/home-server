"""Sends the alerts to the iPhone through the server's own ntfy, from a background thread.

Each alert has buttons: "Hecho" (no more alerts for that time), "+1 h" and "Mañana" (9:00).
The phone calls them back over Tailscale, so they go through the same owner check as the app."""
import json
import os
import re
import sqlite3
import threading
import time as clock
import urllib.request
from datetime import datetime, timedelta

from . import db, when

NTFY_URL = os.environ.get("NTFY_URL", "http://host.docker.internal:8091")
TOPIC_FILE = os.environ.get("NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
PUBLIC_URL = os.environ.get("RECORDATORIOS_URL", "https://mac-server.tailnet-ejemplo.ts.net:3014")
CATCH_UP = timedelta(hours=12)  # after a power cut, alerts missed in this window still arrive (marked late)
TICK = 20  # seconds between checks
BIRTHDAY_WORDS = ("cumple", "aniversario")


def topic() -> str:
    with open(TOPIC_FILE) as f:
        return f.read().strip()


def publish(msg: dict):
    req = urllib.request.Request(NTFY_URL, json.dumps({"topic": topic(), **msg}).encode(),
                                 {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()


LINK = re.compile(r"https?://[^\s<>\"']+")


def links(notes: str | None) -> list[str]:
    """The web links written in the notes (a bill's payment page, a paper in Papeles), without the
    punctuation a sentence puts after them."""
    return [m.group().rstrip(".,;:)!?") for m in LINK.finditer(notes or "")]


def message(r, occ: datetime, now: datetime, fire: datetime) -> dict:
    all_day = not r["time"]
    text = when.when_text(occ, all_day, now)
    if not all_day and now < occ < now + timedelta(days=1):
        text += f" · {when.in_text(occ - now)}"
    if (n := when.years(r, occ)) is not None:
        text += f" · {n} años"
    if fire < now - timedelta(minutes=10):
        text += " · (aviso atrasado)"
    if r["notes"]:
        text += "\n" + r["notes"]
    lowered = r["title"].lower()
    tag = "birthday" if any(w in lowered for w in BIRTHDAY_WORDS) else "date" if all_day else "alarm_clock"
    api = f"{PUBLIC_URL}/api/r/{r['id']}"
    occ_q = f"occ={occ:%Y-%m-%dT%H:%M}"
    return {
        "title": r["title"],
        "message": text,
        "tags": [tag],
        "priority": 4 if occ <= fire + timedelta(minutes=1) else 3,  # the "it's now" one buzzes more
        # Tapping it opens what the reminder is about when the notes link to it (where a bill is paid),
        # else the reminder itself.
        "click": next(iter(links(r["notes"])), f"{PUBLIC_URL}/r/{r['id']}"),
        "actions": [
            {"action": "http", "label": "Hecho", "url": f"{api}/hecho?{occ_q}", "method": "POST", "clear": True},
            {"action": "http", "label": "+1 h", "url": f"{api}/posponer?min=60&{occ_q}", "method": "POST",
             "clear": True},
            {"action": "http", "label": "Mañana", "url": f"{api}/posponer?hasta=manana&{occ_q}", "method": "POST",
             "clear": True},
        ],
    }


def check(conn, now: datetime, send=publish) -> int:
    """One pass: sends every alert due since the last pass. Returns how many went out.
    An alert ntfy refuses doesn't hold up the others: they go out, the next pass starts again at
    the earliest failed one (what went out is in `sent`), and the first error is raised at the end."""
    last = db.get_meta(conn, "last_check")
    start = max(datetime.fromisoformat(last), now - CATCH_UP) if last else now - timedelta(minutes=1)
    count, error, retry_from = 0, None, None
    for r in conn.execute("SELECT * FROM reminders").fetchall():
        for fire, occ in when.fires(r, start, now):
            key = fire.isoformat(timespec="minutes")
            if conn.execute("SELECT 1 FROM sent WHERE reminder_id = ? AND fire_at = ?", (r["id"], key)).fetchone():
                continue
            try:
                send(message(r, occ, now, fire))
            except Exception as e:
                error, retry_from = error or e, min(retry_from or fire, fire)
                continue
            conn.execute("INSERT INTO sent (reminder_id, fire_at) VALUES (?, ?)", (r["id"], key))
            conn.commit()
            count += 1
    due = conn.execute("SELECT s.id AS sid, s.occ, s.fire_at, r.* FROM snoozes s JOIN reminders r ON r.id = s.reminder_id"
                       " WHERE s.fire_at <= ?", (now.isoformat(timespec="minutes"),)).fetchall()
    for s in due:
        try:
            send(message(s, datetime.fromisoformat(s["occ"]), now, datetime.fromisoformat(s["fire_at"])))
        except Exception as e:  # the snooze stays due: the next pass tries it again
            error = error or e
            continue
        conn.execute("DELETE FROM snoozes WHERE id = ?", (s["sid"],))
        conn.commit()
        count += 1
    db.set_meta(conn, "last_check", (retry_from or now).isoformat(timespec="seconds"))
    conn.execute("DELETE FROM sent WHERE fire_at < ?", ((now - timedelta(days=30)).isoformat(),))
    conn.commit()
    if error:
        raise error
    return count


def snooze(conn, reminder_id: int, occ: datetime, now: datetime, minutes: int | None = None):
    """Another alert for this occurrence: in some minutes, or tomorrow at 9:00."""
    if minutes:
        fire = now + timedelta(minutes=minutes)
    else:
        fire = datetime.combine(now.date() + timedelta(days=1), when.ALL_DAY_AT)
    conn.execute("INSERT INTO snoozes (reminder_id, occ, fire_at) VALUES (?, ?, ?)",
                 (reminder_id, occ.isoformat(timespec="minutes"), fire.isoformat(timespec="minutes")))
    conn.commit()


def mark_done(conn, reminder_id: int, occ: datetime):
    """No more alerts (or snoozes) for this occurrence and the ones before it."""
    r = conn.execute("SELECT done_until FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if r is None:
        return
    done = max(filter(None, [r["done_until"], occ.isoformat(timespec="minutes")]))
    conn.execute("UPDATE reminders SET done_until = ? WHERE id = ?", (done, reminder_id))
    conn.execute("DELETE FROM snoozes WHERE reminder_id = ? AND occ <= ?", (reminder_id, done))
    conn.commit()


def run_forever():
    conn = None
    while True:
        try:
            if conn is None:  # in the try: a database that can't be opened right now doesn't end the thread
                conn = db.connect()
            check(conn, datetime.now().replace(microsecond=0))
        except Exception as e:  # ntfy down, DB busy...: try again next tick
            print(f"recordatorios: alert check failed: {e!r}", flush=True)
            if isinstance(e, sqlite3.Error):
                conn = None  # a fresh connection next tick: a broken one may never recover
        clock.sleep(TICK)


def start():
    threading.Thread(target=run_forever, name="alerts", daemon=True).start()

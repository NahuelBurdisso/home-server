"""Reading and writing samples and events. Times are unix seconds; days are Córdoba days (the
container's TZ), which have no daylight saving, so a day is always 24 hourly rows."""
import time
from datetime import datetime

from . import db

RAW_DAYS = 14  # minute samples kept this long; hourly rows forever
END = "￿"  # key < prefix + END: every key starting with prefix

_ids: dict[str, int] = {}


def sid(conn, key: str, create: bool = False) -> int | None:
    """The series id of a key (created on first write). Ids are never deleted while the app
    runs, so the cache stays right; one created in a rolled-back write is dropped (insert)."""
    if key not in _ids:
        row = conn.execute("SELECT id FROM series WHERE key = ?", (key,)).fetchone()
        if not row and not create:
            return None
        if not row:
            conn.execute("INSERT INTO series (key, last_ts) VALUES (?, 0)", (key,))
            row = conn.execute("SELECT id FROM series WHERE key = ?", (key,)).fetchone()
        _ids[key] = row[0]
    return _ids[key]


def insert(conn, ts: int, values: dict):
    known = set(_ids)
    try:
        _insert(conn, ts, values)
    except Exception:
        for k in set(_ids) - known:
            _ids.pop(k, None)
        raise


def _insert(conn, ts: int, values: dict):
    with db.tx(conn):
        ids = {k: sid(conn, k, create=True) for k in values}
        conn.executemany("INSERT OR REPLACE INTO samples (sid, ts, value) VALUES (?, ?, ?)",
                         [(ids[k], ts, v) for k, v in values.items()])
        conn.executemany("UPDATE series SET last_ts = MAX(last_ts, ?) WHERE id = ?", [(ts, i) for i in ids.values()])


def rollup(conn, now: int):
    """Hourly rows for every finished hour not rolled up yet, then drop old minute samples."""
    done = int(db.get_meta(conn, "rolled_until") or 0)
    hour = now // 3600 * 3600
    if done >= hour:
        return
    if not done:
        first = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        if first is None:
            return
        done = first // 3600 * 3600
    with db.tx(conn):
        conn.execute("""
            INSERT OR REPLACE INTO hourly (sid, ts, avg, min, max, n, fails)
            SELECT sid, ts / 3600 * 3600, AVG(value), MIN(value), MAX(value), COUNT(*), SUM(value IS NULL)
            FROM samples WHERE ts >= ? AND ts < ? GROUP BY sid, ts / 3600""", (done, hour))
        conn.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_DAYS * 86400,))
        db.set_meta(conn, "rolled_until", str(hour))


def add_event(conn, ts: int, source: str, level: str, title: str, body: str = "", ref: str | None = None) -> bool:
    """True when it's new (an event replayed after a reconnect has the same ref and is skipped)."""
    return conn.execute("INSERT OR IGNORE INTO events (ts, source, level, title, body, ref) VALUES (?, ?, ?, ?, ?, ?)",
                 (int(ts), source, level, title, body, ref)).rowcount == 1


def buckets(conn, key: str, start: int, end: int, step: int) -> list[dict | None]:
    """One entry per `step` seconds from start to end: avg, min, max, n (samples) and fails
    (samples that were NULL), or None where nothing was measured. Steps of whole hours read
    the hourly rows (plus the minutes not rolled up yet); shorter steps read the minutes."""
    if step % 3600 == 0:
        start = start // 3600 * 3600  # hourly rows start on the hour: a bucket must too
    out: list[dict | None] = [None] * ((end - start + step - 1) // step)
    i = sid(conn, key)
    if i is None:
        return out
    cut = max(start, int(db.get_meta(conn, "rolled_until") or 0)) if step % 3600 == 0 else start
    rows = conn.execute("""
        SELECT (ts - :start) / :step AS b,
               SUM(avg * (n - fails)) / NULLIF(SUM(n - fails), 0) AS avg,
               MIN(min) AS min, MAX(max) AS max, SUM(n) AS n, SUM(fails) AS fails
        FROM (
            SELECT ts, avg, min, max, n, fails FROM hourly
             WHERE sid = :sid AND ts >= :start AND ts < :cut
            UNION ALL
            SELECT ts, value, value, value, 1, value IS NULL FROM samples
             WHERE sid = :sid AND ts >= :cut AND ts < :end
        ) GROUP BY b""", {"sid": i, "start": start, "end": end, "step": step, "cut": cut}).fetchall()
    for r in rows:
        if 0 <= r["b"] < len(out):
            out[r["b"]] = dict(r)
    return out


def latest(conn, prefix: str = "", within: int = 300) -> dict:
    """The newest value of every key starting with `prefix`, if measured in the last 5 minutes."""
    rows = conn.execute("""
        SELECT k.key, s.value FROM series k JOIN samples s ON s.sid = k.id AND s.ts = k.last_ts
        WHERE k.key >= ? AND k.key < ? AND k.last_ts >= ?""",
                        (prefix, prefix + END, int(time.time()) - within)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def keys(conn, prefix: str, since: int) -> list[str]:
    """Keys starting with `prefix` measured at least once since then (a removed container drops out)."""
    rows = conn.execute("SELECT key FROM series WHERE key >= ? AND key < ? AND last_ts >= ? ORDER BY key",
                        (prefix, prefix + END, since)).fetchall()
    return [r[0] for r in rows]


def minutes(conn, key: str, start: int, end: int) -> tuple[int, int]:
    """(minutes measured, of those how many failed) for one key, from the minute samples."""
    i = sid(conn, key)
    if i is None:
        return 0, 0
    n, fails = conn.execute("SELECT COUNT(*), SUM(value IS NULL) FROM samples WHERE sid = ? AND ts >= ? AND ts < ?",
                            (i, start, end)).fetchone()
    return n or 0, fails or 0


def recent(conn, key: str, limit: int = 10) -> list[tuple[int, float | None]]:
    """The last `limit` minute samples of one key, newest first."""
    i = sid(conn, key)
    if i is None:
        return []
    return [tuple(r) for r in conn.execute("SELECT ts, value FROM samples WHERE sid = ? ORDER BY ts DESC LIMIT ?",
                                           (i, limit)).fetchall()]


def periods(conn, start: int, end: int, bad_keys: list[str], is_bad) -> list[dict]:
    """Stretches of consecutive minutes where every key in bad_keys was "bad" (is_bad(value)):
    the internet down (both pings failed), on battery (bat.ac = 0). Minute samples only."""
    ids = {sid(conn, k): k for k in bad_keys}
    if None in ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(f"SELECT ts, sid, value FROM samples WHERE sid IN ({marks}) AND ts >= ? AND ts < ? ORDER BY ts",
                        [*ids, start, end]).fetchall()
    by_ts: dict[int, dict] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[ids[r["sid"]]] = r["value"]
    out, cur = [], None
    for ts, values in by_ts.items():
        bad = all(k in values and is_bad(values[k]) for k in bad_keys)  # a key not measured: not bad
        if bad and cur and ts - cur["end"] <= 180:  # a missing minute or two doesn't split it
            cur["end"] = ts
        elif bad:
            cur = {"start": ts, "end": ts}
            out.append(cur)
        else:
            cur = None
    for p in out:
        p["minutes"] = (p["end"] - p["start"]) // 60 + 1
    return out


def internet_outages(conn, start: int, end: int) -> list[dict]:
    return periods(conn, start, end, ["inet.cf", "inet.google"], lambda v: v is None)


def power_cuts(conn, start: int, end: int) -> list[dict]:
    return periods(conn, start, end, ["bat.ac"], lambda v: v == 0)


def uptime_days(conn, key: str, days: int, today_start: int) -> list[dict]:
    """Per Córdoba day, oldest first: share of minutes `key` (a svc.*.up) was up, or None."""
    start = today_start - (days - 1) * 86400
    out = []
    for i, b in enumerate(buckets(conn, key, start, today_start + 86400, 86400)):
        measured = b["n"] - b["fails"] if b else 0
        out.append({"day": start + i * 86400, "up": b["avg"] if measured else None, "measured": measured,
                    "down_min": round((1 - b["avg"]) * measured) if measured else 0})
    return out


def disk_forecast(conn, now: int, days: int = 30, reserve_pct: float = 15) -> dict:
    """When the disk reaches its 15% reserve (backup.sh and disk-alert.sh use the same number),
    from a straight line through the hourly free space of the last `days` days."""
    start = now - days * 86400
    pts = [((start + i * 3600) / 86400, b["avg"])
           for i, b in enumerate(buckets(conn, "sys.disk_free", start, now, 3600)) if b and b["avg"] is not None]
    used = [b for b in buckets(conn, "sys.disk", now - 86400, now, 3600) if b and b["avg"] is not None]
    if len(pts) < 48 or not used:
        return {"status": "midiendo", "days_measured": round(len(pts) / 24, 1)}
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0  # bytes per day
    free, pct = ys[-1], used[-1]["avg"]
    total = free / (1 - pct / 100) if pct < 100 else free
    reserve = total * reserve_pct / 100
    out = {"free": free, "total": total, "per_day": slope, "days_measured": round(xs[-1] - xs[0], 1)}
    if slope >= -1e6 or free <= reserve:  # losing less than 1 MB a day: stable
        out["status"] = "lleno" if free <= reserve else "estable"
        return out
    out["status"] = "bajando"
    out["days_left"] = (free - reserve) / -slope
    out["date"] = datetime.fromtimestamp(now + out["days_left"] * 86400).date().isoformat()
    return out


def events(conn, start: int, end: int, levels: tuple[str, ...] | None = None, limit: int = 500,
           source: str | None = None, words: list[str] | None = None) -> list:
    """Newest first. source and words (all of them, in the title or the body) filter in SQL, before
    the limit: filtering after it would silently drop older matches."""
    sql = "SELECT * FROM events WHERE ts >= ? AND ts < ?"
    args: list = [start, end]
    if levels:
        sql += f" AND level IN ({', '.join('?' for _ in levels)})"
        args += list(levels)
    if source:
        sql += " AND source = ?"
        args.append(source)
    for w in words or []:
        sql += " AND (title || ' ' || body) LIKE ? ESCAPE '\\'"
        args.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", args + [limit]).fetchall()


def day_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts)
    return int(datetime(d.year, d.month, d.day).timestamp())

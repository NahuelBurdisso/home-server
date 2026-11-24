"""SQLite storage. One file in /data, so the nightly restic backup picks it up (as a VACUUM INTO
copy from setup/sqlite-dumps.sh). Open it only from inside the container (docker exec pulso ...):
the Mac's sqlite3 and the container don't share the WAL index safely."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("PULSO_DB", "/data/pulso.db")

SCHEMA = """
-- Every key ever measured (sys.cpu, inet.cf, ctr.immich_server.mem, svc.Immich.up ...), with a
-- small number that the samples use instead of the name, and when it was last seen.
CREATE TABLE IF NOT EXISTS series (
    id      INTEGER PRIMARY KEY,
    key     TEXT NOT NULL UNIQUE,
    last_ts INTEGER NOT NULL
);

-- One value per key per minute (the collector, app/collect.py). Kept 14 days.
-- value NULL = measured and failed (a ping that got no answer), which is not the same as no row.
CREATE TABLE IF NOT EXISTS samples (
    sid   INTEGER NOT NULL,   -- series.id
    ts    INTEGER NOT NULL,   -- unix seconds, rounded to the minute
    value REAL,
    PRIMARY KEY (sid, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

-- The same, one row per hour, kept for years: long graphs and the disk forecast.
CREATE TABLE IF NOT EXISTS hourly (
    sid   INTEGER NOT NULL,
    ts    INTEGER NOT NULL,   -- start of the hour
    avg   REAL,
    min   REAL,
    max   REAL,
    n     INTEGER NOT NULL,   -- samples in the hour
    fails INTEGER NOT NULL,   -- of those, how many were NULL
    PRIMARY KEY (sid, ts)
) WITHOUT ROWID;

-- What happened: phone alerts (ntfy), containers that stopped or turned unhealthy, power cuts.
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    ts     INTEGER NOT NULL,
    source TEXT NOT NULL,     -- ntfy, docker, pulso
    level  TEXT NOT NULL,     -- alerta, ok, info
    title  TEXT NOT NULL,
    body   TEXT NOT NULL DEFAULT '',
    ref    TEXT UNIQUE        -- the ntfy message id, so a reconnect never stores one twice
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Red (app/red.py): every device ever seen on the home Wi-Fi, from setup/red-scan.sh on the Mac.
CREATE TABLE IF NOT EXISTS red_devices (
    mac        TEXT PRIMARY KEY,  -- aa:bb:cc:dd:ee:ff
    name       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',   -- telefono, compu, tv, parlante, otro, ''
    person     TEXT NOT NULL DEFAULT '',   -- whose it is ('' = nobody's: a TV, the router)
    known      INTEGER NOT NULL DEFAULT 0, -- 0 = "desconocido" until someone names it
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    last_ip    TEXT NOT NULL DEFAULT '',
    present    INTEGER NOT NULL DEFAULT 0,
    seen_scan  INTEGER NOT NULL DEFAULT 0  -- red.scans when it was last seen: "away" needs 3 scans after it
);

-- A device's comings and goings (entro, salio, nuevo). People's go to events.
CREATE TABLE IF NOT EXISTS red_log (
    id   INTEGER PRIMARY KEY,
    ts   INTEGER NOT NULL,
    mac  TEXT NOT NULL,
    what TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS red_log_ts ON red_log (ts);

-- Guardia (app/guardia.py): the weekly look at the logs. One row per report.
CREATE TABLE IF NOT EXISTS guardia (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    text     TEXT NOT NULL,      -- the (at most) three lines that went to the phone
    by_model INTEGER NOT NULL,   -- 1 = worded by Ollama, 0 = by the rules
    findings TEXT NOT NULL,      -- JSON: every finding, ranked
    stats    TEXT NOT NULL       -- JSON: lines read, per container, and what failed to read
);

-- The error templates of each report, to tell next week what is new and what is growing.
CREATE TABLE IF NOT EXISTS guardia_patterns (
    report    INTEGER NOT NULL,  -- guardia.id
    container TEXT NOT NULL,
    template  TEXT NOT NULL,
    count     INTEGER NOT NULL,
    first_ts  INTEGER,
    last_ts   INTEGER,
    example   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (report, container, template)
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")  # the collector threads write while pages read
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


@contextmanager
def tx(conn):
    """One write transaction; rolled back on any error, so a failed write (a lock held past the
    timeout) never leaves the connection stuck inside a transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

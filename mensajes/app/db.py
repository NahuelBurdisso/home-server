"""SQLite storage. One file in /data, so the nightly restic backup picks it up (a VACUUM INTO copy
from setup/sqlite-dumps.sh). Open it only from inside the container (docker exec mensajes ...)."""
import os
import sqlite3

DB_PATH = os.environ.get("MENSAJES_DB", "/data/mensajes.db")

SCHEMA = """
-- Who may write to the bot. Someone who writes without an invitation is kept here with allowed = 0
-- until you authorize them on the Personas page; the bot doesn't answer them meanwhile.
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY,
    tg_id      INTEGER NOT NULL UNIQUE,    -- Telegram's user id (a person has no visible number there)
    name       TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL DEFAULT '',   -- @username, if they have one
    allowed    INTEGER NOT NULL DEFAULT 0,
    avisos     INTEGER NOT NULL DEFAULT 0, -- gets what n8n or the apps send through /api/enviar
    notified   INTEGER NOT NULL DEFAULT 0, -- the iPhone was told someone new wrote
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_at    TEXT
);

-- Every message in and out: the history page, the conversation the AI remembers, the dedupe of a
-- message seen twice (tg_key: chat:message_id) and what "borrar" takes back (undo: JSON list of
-- [method, url]).
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY,
    tg_key    TEXT UNIQUE,
    chat      TEXT NOT NULL,                -- Telegram chat id ('web': the page's try box)
    person_id INTEGER,
    incoming  INTEGER NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'text', -- text, audio, image, document, other
    text      TEXT NOT NULL DEFAULT '',     -- what was written, the transcript of a voice note, or the reply
    intent    TEXT,                         -- what the bot made of it (intents.py), on incoming ones
    app       TEXT,                         -- which app answered
    undo      TEXT,                         -- JSON, on the reply to something that wrote
    undone    INTEGER NOT NULL DEFAULT 0,
    at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat, id);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key: str, value: str | None):
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()

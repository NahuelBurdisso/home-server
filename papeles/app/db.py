"""SQLite storage and the files themselves, all under /data (the nightly restic backup takes the files
as they are and the database as a VACUUM INTO copy from setup/sqlite-dumps.sh). Open the database
only from inside the container (docker exec papeles ...)."""
import os
import sqlite3
from pathlib import Path

DATA = Path(os.environ.get("PAPELES_DATA", "/data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL,             -- factura, garantia, ... (classify.KINDS)
    original_name TEXT NOT NULL,
    file          TEXT NOT NULL,             -- under DATA/files: <sha256>.<ext>
    mime          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,      -- the same file twice is one document
    text          TEXT NOT NULL DEFAULT '',  -- its text, or what OCR read
    doc_date      TEXT,                      -- YYYY-MM-DD, the date it shows (or when it came in)
    expires       TEXT,                      -- YYYY-MM-DD, when it stops being valid (a due date, a warranty)
    expires_found TEXT,                      -- the one read from it, offered until confirmed
    reminder_id   INTEGER,                   -- the Recordatorios reminder for `expires`
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Full-text search, accents ignored ("garantia" finds "garantía").
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, text, notes, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts (rowid, title, text, notes) VALUES (new.id, new.title, new.text, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, title, text, notes) VALUES ('delete', old.id, old.title, old.text, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts (docs_fts, rowid, title, text, notes) VALUES ('delete', old.id, old.title, old.text, old.notes);
    INSERT INTO docs_fts (rowid, title, text, notes) VALUES (new.id, new.title, new.text, new.notes);
END;
"""


def files_dir() -> Path:
    d = DATA / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or str(DATA / "papeles.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn

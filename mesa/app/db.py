"""mesa.db: the schema and the connection. One process writes it (the web and its background thread);
WAL, so pages read while the thread writes. Times are UTC ms; session dates are 'YYYY-MM-DD' in New
York. Open it only from inside the container (docker exec mesa ...): the Mac's sqlite3 and the
container don't share the WAL index safely over the OrbStack mount.

The schema grows by appending to MIGRACIONES: PRAGMA user_version says how many already ran.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

# A write transaction at a time inside this process (the web's requests and the background thread):
# a decision is validated and saved against the same cash the thread fills against.
escritura = threading.RLock()

MIGRACIONES = [
    """
    -- The two portfolios: Claude's and the vara (SPY bought and held). Both start with the same cash.
    CREATE TABLE carteras (
        id              INTEGER PRIMARY KEY,
        clave           TEXT NOT NULL UNIQUE,       -- 'claude' | 'vara'
        nombre          TEXT NOT NULL,
        efectivo        REAL NOT NULL,
        capital_inicial REAL NOT NULL,
        inicio_ts       INTEGER                     -- the open of its first fill (UTC ms); NULL: not started
    );

    -- One row per ticker held (cantidad > 0). abierta_ts: when this holding started (first fill).
    CREATE TABLE posiciones (
        cartera_id  INTEGER NOT NULL,
        ticker      TEXT NOT NULL,
        cantidad    REAL NOT NULL,
        precio_prom REAL NOT NULL,                  -- average cost per share, commissions included
        abierta_ts  INTEGER NOT NULL,
        PRIMARY KEY (cartera_id, ticker)
    );

    CREATE TABLE decisiones (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           INTEGER NOT NULL,
        quien        TEXT NOT NULL,
        sesion       TEXT NOT NULL,                 -- lunes | jueves | manual
        resumen      TEXT NOT NULL,
        regimen_json TEXT,
        skills_json  TEXT,
        con_ordenes  INTEGER NOT NULL DEFAULT 0,
        lecciones    TEXT
    );

    -- An order: by amount (monto_usd), by shares (cantidad) or everything held (todo, sales only).
    -- automatica: the vara's own (the inception buy and the dividend reinvestments).
    CREATE TABLE ordenes (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        cartera_id         INTEGER NOT NULL,
        decision_id        INTEGER,
        ticker             TEXT NOT NULL,
        lado               TEXT NOT NULL,           -- compra | venta
        monto_usd          REAL,
        cantidad           REAL,
        todo               INTEGER NOT NULL DEFAULT 0,
        estado             TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente|ejecutada|rechazada|cancelada
        creada_ts          INTEGER NOT NULL,
        ejecutada_ts       INTEGER,                 -- the open it filled at (UTC ms); or when it was dropped
        fecha_rueda        TEXT,
        precio             REAL,                    -- the fill price, slippage included
        cantidad_ejecutada REAL,
        comision           REAL,
        deslizamiento_usd  REAL,
        motivo_rechazo     TEXT,
        automatica         INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX ordenes_estado ON ordenes (estado, cartera_id);
    CREATE INDEX ordenes_ticker ON ordenes (cartera_id, ticker);

    -- Why each thing is held, as a history: opened, added to, reviewed, closed.
    CREATE TABLE tesis (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        cartera_id      INTEGER NOT NULL,
        ticker          TEXT NOT NULL,
        decision_id     INTEGER,
        ts              INTEGER NOT NULL,
        tipo            TEXT NOT NULL,              -- apertura | refuerzo | revision | cierre
        texto           TEXT NOT NULL,
        horizonte_meses INTEGER,
        invalida        TEXT,
        fuentes_json    TEXT,
        revisar_el      TEXT                        -- YYYY-MM-DD
    );
    CREATE INDEX tesis_ticker ON tesis (cartera_id, ticker, ts);

    CREATE TABLE eventos (
        cartera_id INTEGER NOT NULL,
        ticker     TEXT NOT NULL,
        fecha      TEXT NOT NULL,                   -- ex-date / split date (New York)
        tipo       TEXT NOT NULL,                   -- dividendo | split
        valor      REAL NOT NULL,                   -- USD per share | the ratio (4.0 for 4:1)
        cantidad   REAL NOT NULL,                   -- shares it applied to (before the split)
        neto_usd   REAL NOT NULL DEFAULT 0,         -- dividends: credited after withholding
        ts         INTEGER NOT NULL,                -- when it was applied
        UNIQUE (cartera_id, ticker, fecha, tipo)
    );

    -- One point per session, at the close.
    CREATE TABLE equity (
        cartera_id INTEGER NOT NULL,
        fecha      TEXT NOT NULL,
        valor      REAL NOT NULL,
        efectivo   REAL NOT NULL,
        UNIQUE (cartera_id, fecha)
    );

    -- Daily bars from Yahoo. fecha: the session (New York); ts: that session's 09:30 New York open, UTC ms.
    CREATE TABLE precios (
        ticker TEXT NOT NULL,
        fecha  TEXT NOT NULL,
        ts     INTEGER NOT NULL,
        open   REAL NOT NULL,
        high   REAL,
        low    REAL,
        close  REAL NOT NULL,
        volume REAL,
        PRIMARY KEY (ticker, fecha)
    ) WITHOUT ROWID;

    -- Yahoo's meta for each ticker seen, and the dividends and splits it reported.
    CREATE TABLE tickers (
        ticker     TEXT PRIMARY KEY,
        nombre     TEXT,
        tipo       TEXT,                            -- Yahoo instrumentType: EQUITY, ETF...
        bolsa      TEXT,                            -- Yahoo exchangeName: NMS, NYQ, PCX...
        bolsa_nombre TEXT,                          -- fullExchangeName
        moneda     TEXT,
        meta_json  TEXT,
        visto_ts   INTEGER NOT NULL,                -- last good download
        error      TEXT                             -- the last download failed: why
    );
    CREATE TABLE dividendos_yahoo (ticker TEXT NOT NULL, fecha TEXT NOT NULL, monto REAL NOT NULL,
                                   PRIMARY KEY (ticker, fecha)) WITHOUT ROWID;
    CREATE TABLE splits_yahoo (ticker TEXT NOT NULL, fecha TEXT NOT NULL, numerador REAL NOT NULL,
                               denominador REAL NOT NULL, PRIMARY KEY (ticker, fecha)) WITHOUT ROWID;

    CREATE TABLE estado (clave TEXT PRIMARY KEY, valor TEXT NOT NULL, ts INTEGER NOT NULL);

    -- Changes to the rules after the first fill (DISENO: they are noted, with date and reason).
    CREATE TABLE cambios (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      INTEGER NOT NULL,
        quien   TEXT NOT NULL,
        campo   TEXT NOT NULL,
        antes   TEXT,
        despues TEXT,
        motivo  TEXT
    );

    -- A holding that went back to zero: what it cost, what it brought, the result (for "cuántas ganaron").
    CREATE TABLE cierres (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        cartera_id  INTEGER NOT NULL,
        ticker      TEXT NOT NULL,
        abierta_ts  INTEGER NOT NULL,
        cerrada_ts  INTEGER NOT NULL,
        costo       REAL NOT NULL,
        ingreso     REAL NOT NULL,
        dividendos  REAL NOT NULL,
        resultado   REAL NOT NULL
    );
    """,
]

_reloj = None  # tests set a function returning UTC ms


def ahora_ms() -> int:
    return _reloj() if _reloj else int(time.time() * 1000)


def ruta() -> str:
    return os.environ.get("MESA_DB", "/data/mesa.db")


def conectar() -> sqlite3.Connection:
    p = ruta()
    if os.path.dirname(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrar(conn)
    return conn


def migrar(conn: sqlite3.Connection):
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= len(MIGRACIONES):
        return
    with escritura:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(v, len(MIGRACIONES)):
            conn.executescript("BEGIN;" + MIGRACIONES[i] + f"; PRAGMA user_version = {i + 1}; COMMIT;")
        sembrar(conn)


def sembrar(conn: sqlite3.Connection):
    """The two portfolios with their starting cash, and when the base was born."""
    from . import reglas
    capital = reglas.numero("capital_inicial")
    for i, (clave, nombre) in enumerate([("claude", "Claude"), ("vara", "La vara (SPY)")], 1):
        conn.execute("INSERT OR IGNORE INTO carteras (id, clave, nombre, efectivo, capital_inicial) VALUES (?, ?, ?, ?, ?)",
                     (i, clave, nombre, capital, capital))
    if leer(conn, "creada") is None:
        guardar(conn, "creada", ahora_ms())


@contextmanager
def transaccion(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE … COMMIT, under the process's write lock; ROLLBACK on any error."""
    with escritura:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def leer(conn, clave: str, defecto=None):
    f = conn.execute("SELECT valor FROM estado WHERE clave = ?", (clave,)).fetchone()
    return defecto if f is None else json.loads(f["valor"])


def leer_con_ts(conn, clave: str):
    f = conn.execute("SELECT valor, ts FROM estado WHERE clave = ?", (clave,)).fetchone()
    return None if f is None else (json.loads(f["valor"]), f["ts"])


def guardar(conn, clave: str, valor):
    conn.execute("INSERT INTO estado (clave, valor, ts) VALUES (?, ?, ?) "
                 "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor, ts = excluded.ts",
                 (clave, json.dumps(valor, ensure_ascii=False), ahora_ms()))


def cartera_id(conn, clave: str) -> int:
    return conn.execute("SELECT id FROM carteras WHERE clave = ?", (clave,)).fetchone()["id"]

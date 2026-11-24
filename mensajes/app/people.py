"""Who is writing and whether the bot answers them. Nobody gets an answer until they're authorized:
by opening an invitation link from the Personas page (behind Tailscale), or by you on that page
after they wrote. Someone who writes without one is only noted, and the iPhone is told once.

Telegram shows no phone numbers: a person is their user id, with their name and @username."""
import secrets
from datetime import datetime, timedelta

from .db import get_state, set_state

INVITE_FOR = timedelta(hours=24)


def note(conn, tg_id: int, name: str, username: str = ""):
    """Finds or adds who wrote (new ones not allowed) and keeps their name up to date."""
    conn.execute("INSERT INTO people (tg_id, name, username, last_at) VALUES (?, ?, ?, datetime('now', 'localtime'))"
                 " ON CONFLICT (tg_id) DO UPDATE SET name = CASE WHEN people.name = '' THEN excluded.name ELSE people.name END,"
                 " username = excluded.username, last_at = excluded.last_at", (tg_id, name or "", username or ""))
    conn.commit()
    return conn.execute("SELECT * FROM people WHERE tg_id = ?", (tg_id,)).fetchone()


def allow(conn, person_id: int):
    """Authorized; the first person ever also gets the avisos (/api/enviar)."""
    first = conn.execute("SELECT COUNT(*) FROM people WHERE allowed = 1").fetchone()[0] == 0
    conn.execute("UPDATE people SET allowed = 1, avisos = CASE WHEN ? THEN 1 ELSE avisos END WHERE id = ?",
                 (first, person_id))
    conn.commit()


def invite(conn) -> str:
    """A code for the link t.me/<bot>?start=<code>: whoever opens it within a day is authorized.
    One at a time: a new one replaces the last."""
    code = secrets.token_urlsafe(9)
    set_state(conn, "invite", f"{code} {(datetime.now() + INVITE_FOR).isoformat(timespec='seconds')}")
    return code


def current_invite(conn) -> tuple[str, str] | None:
    """(code, until) while one is valid."""
    value = get_state(conn, "invite") or ""
    if " " not in value:
        return None
    code, until = value.split(" ", 1)
    return (code, until) if datetime.fromisoformat(until) > datetime.now() else None


def redeem(conn, code: str, person) -> bool:
    """'/start <code>' from the link: authorizes them and uses the invitation up."""
    inv = current_invite(conn)
    if not inv or not code or not secrets.compare_digest(code, inv[0]):
        return False
    allow(conn, person["id"])
    set_state(conn, "invite", None)
    return True


def who(person) -> str:
    """'Sofi (@sofi)', for the pages and the notifications."""
    name = person["name"] or "Sin nombre"
    return f"{name} (@{person['username']})" if person["username"] else name


def recipients(conn) -> list:
    """Who gets what's sent through /api/enviar: their private chat with the bot is their user id."""
    return conn.execute("SELECT * FROM people WHERE allowed = 1 AND avisos = 1 ORDER BY id").fetchall()

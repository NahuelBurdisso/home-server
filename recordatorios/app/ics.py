"""Calendar feed, so the iPhone's Calendar app shows everything (read only).
Alerts are left out on purpose: ntfy already sends them, and iOS drops them from subscribed calendars."""
from datetime import date, datetime, timedelta, timezone

FREQ = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}


def _escape(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")  # notes from a textarea come with CRLF
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _rrule(repeat: str, d: date) -> str:
    """The repeat rule, matching the app's alerts (when._shift_months): a monthly 31st falls on the
    last day of shorter months and a yearly Feb 29 on Feb 28, where a bare FREQ would skip them."""
    rule = f"RRULE:FREQ={FREQ[repeat]}"
    if repeat == "monthly" and d.day >= 29:
        days = ",".join(str(x) for x in range(28, d.day + 1))
        rule += f";BYMONTHDAY={days};BYSETPOS=-1"
    elif repeat == "yearly" and (d.month, d.day) == (2, 29):
        rule += ";BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1"
    return rule


def _fold(line: str) -> str:
    """Lines longer than 75 bytes continue on the next one, starting with a space (RFC 5545)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > 75:
            out.append(cur.decode())
            cur = b" "
        cur += b
    out.append(cur.decode())
    return "\r\n".join(out)


def calendar(rows) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mac-server//recordatorios//ES", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Recordatorios", "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    for r in rows:
        d = date.fromisoformat(r["date"])
        lines += ["BEGIN:VEVENT", f"UID:recordatorio-{r['id']}@mac-server", f"DTSTAMP:{stamp}",
                  f"SUMMARY:{_escape(r['title'])}"]
        if r["time"]:
            start = datetime.combine(d, datetime.strptime(r["time"], "%H:%M").time())
            # Floating local time: the phone is in the same time zone as the server.
            lines += [f"DTSTART:{start:%Y%m%dT%H%M%S}", "DURATION:PT1H"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        if r["repeat"] in FREQ:
            lines.append(_rrule(r["repeat"], d))
        if r["notes"]:
            lines.append(f"DESCRIPTION:{_escape(r['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"

"""When things happen: repeats, alert times, and how to say them in Spanish.

A reminder is a row with date, time (None = all day), repeat, alerts and done_until.
All datetimes are naive local time (the container runs with TZ=America/Argentina/Cordoba)."""
from calendar import monthrange
from datetime import date, datetime, time, timedelta

ALL_DAY_AT = time(9, 0)  # all-day dates alert at this hour

REPEATS = {
    "none": "No se repite",
    "daily": "Todos los días",
    "weekly": "Todas las semanas",
    "monthly": "Todos los meses",
    "yearly": "Todos los años",
}

# Minutes before. Labels for timed reminders and for all-day dates.
ALERTS = {
    0: ("A la hora", "Ese día a las 9"),
    15: ("15 min antes", None),
    60: ("1 hora antes", None),
    1440: ("1 día antes", "El día anterior a las 9"),
    10080: ("1 semana antes", "Una semana antes"),
}

DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DAYS_LONG = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def anchor(r) -> datetime:
    t = time.fromisoformat(r["time"]) if r["time"] else ALL_DAY_AT
    return datetime.combine(date.fromisoformat(r["date"]), t)


def alerts_of(r) -> list[int]:
    return sorted({int(x) for x in r["alerts"].split(",") if x.strip()}, reverse=True)


def _shift_months(dt: datetime, n: int) -> datetime:
    """Same day n months later; the 31st becomes the last day of shorter months, Feb 29 -> Feb 28."""
    y, m = divmod(dt.month - 1 + n, 12)
    y += dt.year
    return dt.replace(year=y, month=m + 1, day=min(dt.day, monthrange(y, m + 1)[1]))


def nth(r, k: int) -> datetime | None:
    base = anchor(r)
    rep = r["repeat"]
    if rep == "daily":
        return base + timedelta(days=k)
    if rep == "weekly":
        return base + timedelta(weeks=k)
    if rep == "monthly":
        return _shift_months(base, k)
    if rep == "yearly":
        return _shift_months(base, 12 * k)
    return base if k == 0 else None


def next_on_or_after(r, t: datetime) -> datetime | None:
    base = anchor(r)
    if t <= base:
        return base
    rep = r["repeat"]
    if rep in ("daily", "weekly"):
        step = timedelta(days=1 if rep == "daily" else 7)
        k = -((base - t) // step)  # ceil((t - base) / step)
    elif rep == "monthly":
        k = (t.year - base.year) * 12 + t.month - base.month
    elif rep == "yearly":
        k = t.year - base.year
    else:
        return None
    occ = nth(r, k)
    while occ < t:
        k += 1
        occ = nth(r, k)
    return occ


def occurrences(r, start: datetime, end: datetime):
    """Occurrences in [start, end), without the ones already marked done."""
    done = datetime.fromisoformat(r["done_until"]) if r["done_until"] else None
    occ = next_on_or_after(r, start)
    while occ is not None and occ < end:
        if done is None or occ > done:
            yield occ
        if r["repeat"] == "none":
            break
        occ = next_on_or_after(r, occ + timedelta(seconds=1))


def fires(r, start: datetime, end: datetime):
    """(alert time, occurrence) for every alert that goes off in [start, end). The closest alert
    comes first: when two fall on the same minute (a weekly one's "1 semana antes" is the moment of
    this week's), notify.py sends one, and it should be the "it's now" one."""
    for off in sorted(alerts_of(r)):
        delta = timedelta(minutes=off)
        for occ in occurrences(r, start + delta, end + delta):
            yield occ - delta, occ


def next_occurrence(r, now: datetime) -> datetime | None:
    return next(occurrences(r, now.replace(hour=0, minute=0, second=0, microsecond=0), datetime.max), None)


def years(r, occ: datetime) -> int | None:
    """Birthdays and anniversaries saved with their real year: how many years this time."""
    if r["repeat"] != "yearly":
        return None
    first = date.fromisoformat(r["date"]).year
    created = int(str(r["created_at"])[:4]) if r["created_at"] else occ.year
    return occ.year - first if first < created and occ.year > first else None


def short_date(d: date, today: date) -> str:
    return f"{d.day}/{d.month}" + (f"/{d.year}" if d.year != today.year else "")


def when_text(occ: datetime, all_day: bool, now: datetime) -> str:
    """'Hoy 18:00', 'Mañana', 'Viernes 9:30', 'sáb 15/3/2027'."""
    d, today = occ.date(), now.date()
    days = (d - today).days
    if days == 0:
        s = "Hoy"
    elif days == 1:
        s = "Mañana"
    elif days == -1:
        s = "Ayer"
    elif 1 < days < 7:
        s = DAYS_LONG[d.weekday()].capitalize()
    else:
        s = f"{DAYS[d.weekday()]} {short_date(d, today)}"
    return s if all_day else f"{s} {occ:%H:%M}"


def in_text(delta: timedelta) -> str:
    """'en 15 min', 'en 2 h', 'en 1 h 30 min'."""
    mins = round(delta.total_seconds() / 60)
    h, m = divmod(mins, 60)
    if not h:
        return f"en {m} min"
    return f"en {h} h" + (f" {m} min" if m else "")


def plural_day(wd: int) -> str:
    name = DAYS_LONG[wd]
    return name if name.endswith("s") else name + "s"


def long_when(r, now: datetime) -> str:
    """How the reminder reads in a sentence: 'mañana jueves 22/10 a las 18:00',
    'todos los años el 15/3', 'todos los lunes a las 9:00'."""
    d = date.fromisoformat(r["date"])
    at = f" a las {r['time']}" if r["time"] else ""
    rep = r["repeat"]
    if rep == "yearly":
        return f"todos los años el {d.day}/{d.month}{at}"
    if rep == "monthly":
        return f"todos los meses el {d.day}{at}"
    if rep == "weekly":
        return f"todos los {plural_day(d.weekday())}{at}"
    if rep == "daily":
        return f"todos los días{at}"
    days = (d - now.date()).days
    day = {0: "hoy ", 1: "mañana ", 2: "pasado mañana "}.get(days, "el ")
    return f"{day}{DAYS_LONG[d.weekday()]} {short_date(d, now.date())}{at}"


def alerts_text(r) -> str:
    all_day = not r["time"]
    names = []
    for off in alerts_of(r):
        label = ALERTS.get(off, (None, None))[1 if all_day else 0] or f"{off} min antes"
        names.append(label[0].lower() + label[1:])
    if not names:
        return "Sin avisos."
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]
    return f"Te aviso {joined}."

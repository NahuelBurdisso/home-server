"""Understands a reminder written or dictated in Spanish, in one line:

  "mañana 18hs dentista"                 "cumple de mamá 15/3"
  "el jueves a las 9 llamar al banco"    "pagar la tarjeta el 10 todos los meses"
  "en 20 minutos sacar la ropa"          "recordame que el 3 de diciembre vence el seguro"

Dates, times and repeats are taken out; what is left is the title. No AI involved: it is
fast, private, and always reads the same sentence the same way."""
import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, time, timedelta

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
NUMBERS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
           "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20, "treinta": 30}
REPEAT_WORDS = [
    (r"todos los dias|cada dia|diariamente|a diario", "daily"),
    (r"todas las semanas|cada semana|semanalmente", "weekly"),
    (r"todos los meses|cada mes|mensualmente", "monthly"),
    (r"todos los anos|cada ano|anualmente", "yearly"),
]
YEARLY_THINGS = r"\b(?:cumple|cumpleanos|aniversario)\b"
# Words that mean nothing at the start or end of a title once the date is gone.
FILLER = {"recordame", "recordarme", "recorda", "recordar", "acordame", "acordarme", "avisame", "avisarme",
          "agendame", "agenda", "agendar", "anotame", "anota", "anotar", "que", "tengo", "hay", "de", "del", "el",
          "la", "los", "las", "a", "al", "en", "para", "y", "por", "favor", "es", "me"}

_WD = "|".join(WEEKDAYS)
_MON = "|".join(MONTHS)
_NUM = r"\d+|" + "|".join(NUMBERS) + "|media"
TIME = re.compile(
    r"\b(?P<pre>(?:a las?|a eso de las?|tipo|desde las?) )?(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?"
    r"(?: ?(?P<suf>hs|h|horas|am|pm)\b)?(?: y (?P<frac>media|cuarto))?(?: de la (?P<part>manana|tarde|noche))?\b")


def fold(s: str) -> str:
    """Lowercase without accents, one character per character (so positions match the original)."""
    return "".join(unicodedata.normalize("NFD", c.lower())[0] for c in s)


class _Text:
    def __init__(self, s: str):
        self.orig, self.low = s, fold(s)

    def cut(self, a: int, b: int):
        self.low = self.low[:a] + " " * (b - a) + self.low[b:]
        self.orig = self.orig[:a] + " " * (b - a) + self.orig[b:]

    def take(self, pattern: str):
        m = re.search(pattern, self.low)
        if m:
            self.cut(*m.span())
        return m


def _next_weekday(today: date, wd: int, allow_today: bool) -> date:
    ahead = (wd - today.weekday()) % 7
    return today + timedelta(days=ahead if ahead or allow_today else 7)


def _day_of_month(today: date, day: int, clamp: bool = False) -> date | None:
    """The next day-th (this month if it hasn't passed). clamp: a month without that day counts
    with its last one (a monthly "el 31" is due on Nov 30), instead of being skipped."""
    if not 1 <= day <= 31:
        return None
    for k in range(13):
        y, m = divmod(today.month - 1 + k, 12)
        last = monthrange(today.year + y, m + 1)[1]
        if day > last and not clamp:
            continue
        d = date(today.year + y, m + 1, min(day, last))
        if d >= today:
            return d
    return None


def _future_date(today: date, day: int, month: int, year: int | None, clamp: bool = False) -> date | None:
    """The next day/month from today. Feb 29 is the next leap year's, or with clamp (a yearly
    birthday) Feb 28 in a year without one, so no year is skipped (when.py repeats it the same way)."""
    if year:
        try:
            return date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            return None
    if not 1 <= month <= 12:
        return None
    for y in range(today.year, today.year + 9):  # leap years can be 8 apart (2096 -> 2104)
        last = monthrange(y, month)[1]
        if day < 1 or (day > last and not (clamp and (month, day) == (2, 29))):
            continue
        d = date(y, month, min(day, last))
        if d >= today:
            return d
    return None


def _clock(m, part: str | None = None) -> time | None:
    """The time a TIME match says. part ("manana", "tarde", "noche") comes from elsewhere in the
    sentence when the match itself has none: "esta noche a las 10"."""
    h, mins = int(m["h"]), int(m["m"] or 0)
    part = m["part"] or part
    if m["frac"]:
        mins = 30 if m["frac"] == "media" else 15
    if part == "noche" and h == 12:
        h = 0  # "las 12 de la noche": midnight (parse moves it to the next day)
    elif m["suf"] == "pm" or part in ("tarde", "noche"):
        h += 12 if h < 12 else 0
    elif m["suf"] == "am" or part == "manana":
        h = 0 if h == 12 else h
    elif m["suf"] not in ("am", "pm") and 1 <= h <= 6:  # "5hs" too, like "a las 5"
        h += 12  # "a las 5" for a reminder means the afternoon
    return time(h, mins) if h <= 23 and mins <= 59 else None


def _loose(m) -> bool:
    """A time said without am/pm or part of the day ("a las 8"), so it could be either."""
    return m is not None and m["suf"] not in ("am", "pm") and not m["part"]


def clean_title(s: str) -> str:
    words = s.replace(",", " ").split()
    strip = lambda w: fold(w).strip(".;:!?¿¡-")  # noqa: E731
    while words and strip(words[0]) in FILLER | {""}:
        words.pop(0)
    while words and strip(words[-1]) in FILLER | {""}:
        words.pop()
    title = " ".join(words).strip(" .;:-")
    return (title[:1].upper() + title[1:]) if title else "Recordatorio"


def parse(text: str, now: datetime | None = None) -> dict | None:
    """{'title', 'date', 'time', 'repeat', 'alerts'} or None when there is no date or time in it."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    today = now.date()
    t = _Text(text.strip())
    day: date | None = None
    clock: time | None = None
    repeat = "none"
    soon = False  # "en 20 minutos": one alert, right then

    # How often
    m = t.take(rf"\b(?:todos los|todas las|cada) ({_WD})s?\b")
    if m:
        repeat, day = "weekly", _next_weekday(today, WEEKDAYS[m[1]], allow_today=True)
    m = t.take(r"\b(?:los|el) (\d{1,2}) de cada mes\b")
    if m:
        repeat, day = "monthly", _day_of_month(today, int(m[1]), clamp=True)
    for words, rep in REPEAT_WORDS:
        if t.take(rf"\b(?:{words})\b"):
            repeat = rep
    birthday = bool(re.search(YEARLY_THINGS, t.low))
    if birthday and repeat == "none":
        repeat = "yearly"

    # In a while
    # "en 2 horas", "dentro de 20 minutos", "en una hora y media"
    m = t.take(rf"\b(?:en|dentro de) ({_NUM}) ?(?:(horas?|hs|h)(?: y (media|cuarto))?|"
               r"(minutos?|mins?|dias?|semanas?|mes|meses))\b")
    if m:
        n = 30 if m[1] == "media" else int(m[1]) if m[1].isdigit() else NUMBERS[m[1]]
        unit = m[4] or "h"
        if m[1] == "media":
            unit = "min"  # "en media hora"
        if unit.startswith(("min", "h")):
            mins = n if unit.startswith("min") else n * 60 + {"media": 30, "cuarto": 15}.get(m[3], 0)
            at = now + timedelta(minutes=mins)
            day, clock, soon = at.date(), at.time(), True
        elif unit.startswith("dia"):
            day = today + timedelta(days=n)
        elif unit.startswith("semana"):
            day = today + timedelta(weeks=n)
        else:
            y, mo = today.year + (today.month - 1 + n) // 12, (today.month - 1 + n) % 12 + 1
            day = date(y, mo, min(today.day, monthrange(y, mo)[1]))  # Oct 31 + 1 month: Nov 30

    # Dates
    if day is None:
        m = t.take(r"\b(?:el )?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
        if m:
            day = _future_date(today, int(m[1]), int(m[2]), int(m[3]) if m[3] else None, clamp=repeat == "yearly")
    if day is None:
        m = t.take(rf"\b(?:el )?(\d{{1,2}}|primero) de ({_MON})(?: (?:de |del )?(\d{{4}}))?\b")
        if m:
            d = 1 if m[1] == "primero" else int(m[1])
            day = _future_date(today, d, MONTHS[m[2]], int(m[3]) if m[3] else None, clamp=repeat == "yearly")

    # Times
    midnight = False  # "las 12 de la noche" belongs to the night of the day it's said with
    said = None  # the TIME match the clock came from
    if clock is None:
        for m in TIME.finditer(t.low):
            if (m["pre"] or m["m"] or m["suf"] or m["part"]) and (c := _clock(m)):
                clock, said = c, m
                midnight = m["part"] == "noche" and c == time(0, 0)
                t.cut(*m.span())
                break
    if clock is None and t.take(r"\b(?:al )?mediodia\b"):
        clock = time(12, 0)
    m = t.take(r"\b(?:(?:a|por|de) la|esta) (manana|tarde|noche)\b")
    if m:
        if _loose(said) and not (m[1] == "manana" and said["h"] == "12") and (c := _clock(said, m[1])):
            # "esta noche a las 10", "mañana a la tarde a las 7": the part of the day says which 10.
            # ("a la mañana a las 12" stays noon.)
            clock, midnight, said = c, m[1] == "noche" and c == time(0, 0), None
        clock = clock or {"manana": time(9, 0), "tarde": time(17, 0), "noche": time(21, 0)}[m[1]]
        if m[0].startswith("esta") and day is None:
            day = today

    # Days in words
    if day is None:
        if t.take(r"\bpasado manana\b"):
            day = today + timedelta(days=2)
        elif t.take(r"\bmanana\b"):
            day = today + timedelta(days=1)
        elif t.take(r"\bhoy\b"):
            day = today
    if day is None:
        m = t.take(rf"\b(?:el |este |el proximo |proximo )?({_WD})(?: que viene| proximo)?\b")
        if m:
            day = _next_weekday(today, WEEKDAYS[m[1]], allow_today=False)
    if day is None:
        m = t.take(r"\bel (\d{1,2})\b(?![:./]| ?(?:hs|h|am|pm)\b)")
        if m:
            day = _day_of_month(today, int(m[1]), clamp=repeat == "monthly")

    if day is None and clock is None:
        return None
    if midnight and day is not None:
        day += timedelta(days=1)
    evening = clock.replace(hour=clock.hour + 12) if clock and clock.hour < 12 else None
    if (_loose(said) and 7 <= clock.hour <= 11 and now.hour >= 12 and repeat == "none" and day in (None, today)
            and datetime.combine(today, clock) <= now < datetime.combine(today, evening) <= now + timedelta(hours=8)):
        # "a las 8" said at 19:30: tonight's 20:00, not tomorrow morning. Said in the morning, or with
        # the evening hour still far ("a las 10" at 12:30), it stays tomorrow's.
        clock = evening
    if day is None:
        day = today if datetime.combine(today, clock) > now else today + timedelta(days=1)

    if soon:
        alerts = [0]
    elif birthday:
        alerts = [1440, 0]
    elif clock:
        alerts = [60, 0]
    else:
        alerts = [0]
    return {
        "title": clean_title(t.orig),
        "date": day.isoformat(),
        "time": clock.strftime("%H:%M") if clock else None,
        "repeat": repeat,
        "alerts": ",".join(str(a) for a in alerts),
    }

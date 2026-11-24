"""Second try with the local AI (Ollama) when parse.py finds no date in a sentence:
"el finde largo de noviembre", "cuando cobre el aguinaldo... no, el viernes que viene a la tarde".
parse.py stays first because it is instant and always reads a sentence the same way; the model
only sees sentences it couldn't read, and its answer is checked before anything is saved.

RECORDATORIOS_IA=ollama (default) or none."""
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta

from .parse import WEEKDAYS, clean_title, fold

URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
REPEATS = ("none", "daily", "weekly", "monthly", "yearly")
DAY_NAMES = list(WEEKDAYS)  # Monday first, like date.weekday()
MONTH_NAMES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
               "octubre", "noviembre", "diciembre"]


def enabled() -> bool:
    return os.environ.get("RECORDATORIOS_IA", "ollama") == "ollama"


def _calendar(today: date) -> str:
    """The next two weeks spelled out: small models get weekdays wrong when they count them."""
    days = [today + timedelta(days=i) for i in range(15)]
    return "\n".join(f"- {DAY_NAMES[d.weekday()]} {d.day} de {MONTH_NAMES[d.month - 1]}: {d.isoformat()}" for d in days)


def ask(prompt: str) -> dict:
    schema = {"type": "object", "required": ["titulo", "fecha", "hora", "repetir"], "properties": {
        "titulo": {"type": "string"}, "fecha": {"type": "string"}, "hora": {"type": "string"},
        "repetir": {"type": "string", "enum": list(REPEATS)}}}
    body = {"model": MODEL, "stream": False, "think": False, "format": schema, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{URL}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(json.load(resp)["message"]["content"])


def understand(text: str, now: datetime, asker=ask) -> dict | None:
    """The same fields as parse.parse(), or None when the model can't place it either."""
    if not enabled() or not text.strip():
        return None
    today = now.date()
    prompt = (
        "Sos un asistente que agenda recordatorios para alguien en Córdoba, Argentina.\n"
        f"Ahora es {DAY_NAMES[today.weekday()]} {today.isoformat()} a las {now:%H:%M}. Próximos días:\n"
        f"{_calendar(today)}\n\n"
        "De la frase, sacá:\n"
        '- "titulo": qué hay que hacer o qué pasa, corto, sin la fecha ni "recordame".\n'
        '- "fecha": el día en formato YYYY-MM-DD, o "" si la frase no dice ningún día ni momento.\n'
        '- "hora": HH:MM en 24 horas, o "" si no dice hora. "A la tarde" es 17:00, "a la noche" 21:00, '
        '"a la mañana" 09:00.\n'
        '- "repetir": none, daily, weekly, monthly o yearly.\n\n'
        f"Frase: {text}")
    try:
        got = asker(prompt)
        day = date.fromisoformat(got["fecha"])
        clock = datetime.strptime(got["hora"], "%H:%M").strftime("%H:%M") if got.get("hora") else None
    except Exception:
        return None
    if day < today or day > today + timedelta(days=3 * 365):
        return None
    # The model reads "la semana que viene" as weekly: a repeat counts only if the sentence asks for one.
    asks_repeat = re.search(r"\b(?:todos|todas|cada|diari|semanal|mensual|anual)", fold(text))
    repeat = got.get("repetir") if asks_repeat and got.get("repetir") in REPEATS else "none"
    return {"title": clean_title(got.get("titulo") or text), "date": day.isoformat(), "time": clock,
            "repeat": repeat, "alerts": "60,0" if clock else "0", "ia": True}

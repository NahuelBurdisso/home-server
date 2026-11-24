"""Reminders as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model
(qwen3 on Ollama) answer "¿qué tengo esta semana?" or save "recordame el jueves llamar al banco".

Dates are worked out here, never by the model: agendar takes the person's own sentence and
reads it like the quick-add box does (parse.py, then ia.py). Mounted at /ia in main.py."""
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from . import when
from .db import connect
from .ia import understand
from .parse import fold, parse

ia = FastAPI(
    title="Recordatorios",
    version="1",
    description="Los recordatorios y fechas del dueño (eventos, cumpleaños, vencimientos) que le llegan al iPhone. "
                "Las fechas ya vienen calculadas: no las recalcules.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


def _item(r, occ, at) -> dict:
    return {"titulo": r["title"], "cuando": when.when_text(occ, not r["time"], at), "fecha": occ.date().isoformat(),
            "hora": r["time"], "se_repite": when.REPEATS[r["repeat"]] if r["repeat"] != "none" else None,
            "cumple_anos": when.years(r, occ), "notas": r["notes"] or None}


@ia.get("/proximos", operation_id="proximos_recordatorios",
        summary="Lo que viene en los próximos días (eventos, cumpleaños, vencimientos), del más cercano al más lejano")
def upcoming(dias: int = Query(7, description="Cuántos días mirar hacia adelante, de 1 a 60.")):
    from .main import now, upcoming as all_upcoming  # main imports this module
    at = now()
    items = [i for i in all_upcoming(connect(), at, max(1, min(dias, 60))) if not i["past"]]
    return {"hoy": at.date().isoformat(), "dias": dias, "cantidad": len(items),
            "recordatorios": [_item(i["r"], i["occ"], at) for i in items[:30]]}


@ia.get("/buscar", operation_id="buscar_recordatorio",
        summary="Busca un recordatorio por su título (por ejemplo: cumple de mamá, seguro, dentista) y dice cuándo es")
def search(texto: str = Query(..., description="Palabras del título.")):
    from .main import now
    at = now()
    words = fold(texto).split()
    found = []
    for r in connect().execute("SELECT * FROM reminders").fetchall():
        if all(w in fold(r["title"]) for w in words):
            nxt = when.next_occurrence(r, at)
            found.append({**_item(r, nxt, at), "ya_paso": False} if nxt else
                         {"titulo": r["title"], "fecha": r["date"], "ya_paso": True})
    return {"buscado": texto, "cantidad": len(found), "recordatorios": found[:20]}


class Reminder(BaseModel):
    texto: str = Field(..., description='La frase de la persona tal cual, con el cuándo: "el jueves a las 9 llamar al banco".')


@ia.post("/agendar", operation_id="agendar_recordatorio",
         summary="Guarda un recordatorio a partir de la frase de la persona; le avisa al iPhone a su hora")
def schedule(body: Reminder):
    from .main import add, confirmation, get, now
    at = now()
    found = parse(body.texto, at) or understand(body.texto, at)
    if found is None:
        return {"guardado": False, "respuesta": "No encontré cuándo. Pedile el día (y la hora si hace falta)."}
    conn = connect()
    r = get(conn, add(conn, found))
    return {"guardado": True, "respuesta": "Listo. " + confirmation(r, at), "titulo": r["title"],
            "fecha": r["date"], "hora": r["time"]}

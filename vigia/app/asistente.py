"""Vigía as tools for the local AI: Open WebUI and the Telegram bot read /ia/openapi.json and let
the model answer "¿qué estoy vigilando?" or "¿qué guardé sobre la yerba?", or keep a link. What it
reads comes from the rules (watch.py, guardado.py), never from the model. Mounted at /ia in main.py."""
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from . import guardado, watch
from .db import connect

ia = FastAPI(
    title="Vigía",
    version="1",
    description="Páginas que el server vigila (precios, cambios, turnos) y links guardados con resumen.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


@ia.get("/vigilancias", operation_id="ver_vigilancias",
        summary="Las páginas que vigila el server: qué leyó por última vez, cuándo y si anda bien")
def watches():
    rows = connect().execute("SELECT * FROM watches ORDER BY active DESC, id").fetchall()
    return {"cantidad": len(rows), "vigilancias": [
        {"nombre": w["name"], "link": w["url"], "tipo": w["kind"], "lee": watch.card_text(w),
         "ultima_vez": w["last_ok"], "ultimo_cambio": w["last_change"], "estado": watch.state(w),
         "pagina": watch.link(w["id"])} for w in rows]}


@ia.get("/guardado", operation_id="buscar_en_guardado",
        summary="Busca en los links, videos y audios guardados (título, resumen y texto)")
def search(q: str = Query("", description="Las palabras a buscar; vacío: lo último guardado.")):
    rows = guardado.search(connect(), q, 8)
    return {"resultados": [{"titulo": r["title"], "tipo": r["kind"], "link": r["url"], "estado": r["status"],
                            "fragmento": (r["snippet"] or "").replace("\x02", "").replace("\x03", ""),
                            "pagina": guardado.link(r["id"])} for r in rows]}


class Link(BaseModel):
    url: str = Field(..., description="El link a guardar, completo (https://…).")


@ia.post("/guardar", operation_id="guardar_link", summary="Guarda un link: lee el texto y escribe un resumen")
def save(body: Link):
    try:
        iid, new, sentence = guardado.save(connect(), url=body.url)
    except guardado.Invalid as e:
        return {"respuesta": str(e)}
    return {"respuesta": sentence, "id": iid, "nuevo": new}

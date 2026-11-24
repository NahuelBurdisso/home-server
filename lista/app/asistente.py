"""The list as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model answer
"¿qué falta comprar?" or do "agregá yerba y lavandina". The sentence is read by the same rules as
the app (parse.py), never by the model. Mounted at /ia in main.py."""
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from . import store
from .db import connect
from .parse import SECTIONS

ia = FastAPI(
    title="Lista",
    version="1",
    description="La lista de compras de la casa, por sección del súper.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


@ia.get("/lista", operation_id="ver_lista_de_compras", summary="Lo que falta comprar, por sección del súper")
def show():
    conn = connect()
    by = {}
    for r in store.pending(conn):
        by.setdefault(SECTIONS[r["section"]][0], []).append(r["text"])
    return {"cantidad": sum(len(v) for v in by.values()), "por_seccion": by,
            "en_el_carrito": [r["text"] for r in store.cart(conn)],
            "lo_de_siempre_que_no_esta": [r["text"] for r in store.usual(conn, 6)]}


class Things(BaseModel):
    texto: str = Field(..., description="Lo que dijo la persona, tal cual: 'leche, pan y 2 kg de papas'.")


@ia.post("/agregar", operation_id="agregar_a_la_lista", summary="Agrega cosas a la lista de compras")
def add(body: Things):
    res = store.add(connect(), body.texto)
    return {"respuesta": store.added_sentence(res), "agregado": res["added"], "ya_estaba": res["already"]}


@ia.post("/comprado", operation_id="marcar_comprado", summary="Marca como compradas cosas de la lista ('ya compré la leche')")
def bought(body: Things):
    done = store.tick(connect(), body.texto)
    return {"marcado": done} if done else {"marcado": [], "nota": "No encontré eso en la lista."}


@ia.get("/historial", operation_id="historial_de_compras", summary="Las últimas compras y lo que más se compra")
def history(veces: int = Query(5, description="Cuántas compras pasadas mostrar.")):
    conn = connect()
    return {"compras": [{"cuando": t["when"][:16], "cosas": [r["text"] for r in t["items"]]}
                        for t in store.trips(conn, max(1, min(veces, 20)))],
            "lo_que_mas_se_compra": [{"que": r["text"], "veces": r["times"]} for r in store.usual(conn, 10, True)]}

"""Pedidos as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model answer
"¿qué pedidos hay?" or "¿cuánto está la banana?", or build a draft ("armá el pedido de la
verdulería"), also from a saved order ("usá lo de siempre"). Building a draft sends nothing (a
person sends it from the page), so the model may do it. Which product, how many and what it costs
come from the rules (match.py), never from the model, and catalog text is never handed to it as
instructions. Mounted at /ia in main.py."""
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from . import orders
from .db import connect

ia = FastAPI(
    title="Pedidos",
    version="1",
    description="Los pedidos de comida de la casa a la verdulería, la carnicería y el súper.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


@ia.get("/pedidos", operation_id="ver_pedidos", summary="Los pedidos abiertos: borradores sin mandar y pedidos que no llegaron")
def show():
    conn = connect()
    return {"respuesta": orders.open_sentence(conn),
            "pedidos": [{"proveedor": o["provider"], "estado": orders.STATUS[o["status"]],
                         "total_estimado": orders.money(o["total_est"]) if o["total_est"] else None,
                         "pagina": orders.link(o["id"])} for o in orders.open_orders(conn)]}


class Order(BaseModel):
    texto: str = Field("", description="Qué pedir, tal cual lo dijo la persona ('2 kg de papas y bananas'); "
                                       "vacío: lo pendiente en la Lista.")
    proveedor: str = Field("", description="A quién: un nombre o una palabra como 'verdulería', 'carne', 'súper'; "
                                           "vacío: a cada uno lo suyo.")


@ia.post("/armar", operation_id="armar_pedido",
         summary="Arma el borrador del pedido (no lo manda: una persona lo revisa y lo manda)")
def build(body: Order):
    conn = connect()
    try:
        res = orders.armar(conn, body.texto, body.proveedor, "asistente")
    except orders.Unavailable as e:
        return {"respuesta": str(e)}
    return {"respuesta": orders.armar_sentence(conn, res), "pedidos": res["orders"]}


@ia.get("/guardados", operation_id="ver_guardados", summary="Los pedidos guardados para repetir")
def saved():
    conn = connect()
    orders.reprice_saved(conn)
    return {"respuesta": orders.saved_sentence(conn),
            "guardados": [{"nombre": o["name"], "proveedor": o["provider"], "cosas": o["n"],
                           "total_estimado": orders.money(o["total_est"]) if o["total_est"] else None,
                           "pagina": orders.link(o["id"])} for o in orders.saved_orders(conn)]}


class Saved(BaseModel):
    nombre: str = Field(..., description="El nombre del guardado ('lo de siempre') o del proveedor.")


@ia.post("/usar", operation_id="usar_guardado",
         summary="Suma un pedido guardado al borrador de su proveedor (no lo manda)")
def use(body: Saved):
    conn = connect()
    found = orders.find_saved(conn, body.nombre)
    if found is None:
        return {"respuesta": orders.unknown_saved_sentence(conn, body.nombre)}
    try:
        res = orders.use(conn, found, "asistente")
    except orders.Invalid as e:
        return {"respuesta": str(e)}
    return {"respuesta": orders.use_sentence(conn, res, found), "pedido": res["order"]}


@ia.get("/precio", operation_id="precio_de", summary="Cuánto está algo en los catálogos de los proveedores")
def price(q: str = Query(..., description="Qué buscar: 'banana', 'papa'.")):
    conn = connect()
    return {"respuesta": orders.price_sentence(conn, q), "encontrado": orders.prices(conn, q)}

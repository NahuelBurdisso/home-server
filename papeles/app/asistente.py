"""The documents as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model
answer "¿dónde está la garantía de la heladera?" or "¿qué papeles vencen este mes?". The search is
the app's own (full text, accents ignored). Mounted at /ia in main.py."""
from fastapi import FastAPI, Query

from . import store
from .db import connect
from .extract import KINDS

ia = FastAPI(
    title="Papeles",
    version="1",
    description="Los documentos de la casa (facturas, garantías, contratos, recetas, impuestos), buscables por "
                "cualquier palabra que contengan.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


def _doc(d, snippet: bool = True) -> dict:
    out = {"titulo": d["title"], "tipo": KINDS[d["kind"]][0], "fecha": d["doc_date"], "vence": d["expires"],
           "link": f"{store.PUBLIC_URL}/d/{d['id']}"}
    if snippet and d["snip"]:
        out["fragmento"] = d["snip"]
    return out


@ia.get("/buscar", operation_id="buscar_papeles",
        summary="Busca documentos de la casa por palabras (garantía heladera, contrato alquiler, factura epec)")
def find(texto: str = Query(..., description="Las palabras a buscar."),
         tipo: str | None = Query(None, description="Opcional: " + ", ".join(KINDS))):
    docs = store.search(connect(), texto, tipo if tipo in KINDS else "", limit=10)
    if not docs and len(texto.split()) > 1:  # every word required: try with the longest one alone
        docs = store.search(connect(), max(texto.split(), key=len), tipo if tipo in KINDS else "", limit=10)
    return {"buscado": texto, "cantidad": len(docs), "documentos": [_doc(d) for d in docs]}


@ia.get("/vencimientos", operation_id="papeles_que_vencen", summary="Documentos que vencen pronto (garantías, pólizas, facturas)")
def soon(dias: int = Query(60, description="Cuántos días hacia adelante.")):
    return {"documentos": [_doc(d, False) for d in store.upcoming(connect(), max(1, min(dias, 730)))]}

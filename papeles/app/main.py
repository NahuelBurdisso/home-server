"""Papeles: the house's documents (bills, warranties, contracts, prescriptions, taxes), found by any
word in them. A PDF gives its text; a photo or a scan is read by OCR on this Mac. What it is and
when it expires are read from it; a confirmed expiry becomes a reminder in Recordatorios."""
import os
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, store
from .db import connect, files_dir
from .extract import KINDS

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("PAPELES_ALLOWED_LOGINS", "").split(",") if s.strip()}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(KINDS=KINDS, PUBLIC_URL=store.PUBLIC_URL)


def ddmmyyyy(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


templates.env.filters["dmy"] = ddmmyyyy


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_HOSTS = {"mac-server.tailnet-ejemplo.ts.net", "localhost", "127.0.0.1"}


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. Siri, n8n and curl send neither, and are let through."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "same-site", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlparse(origin).hostname in OWN_HOSTS | {request.url.hostname}
    return True


@app.middleware("http")
async def tailnet_owner_only(request: Request, call_next):
    """tailscale serve tells us who is asking (Tailscale-User-Login). Only the people in
    PAPELES_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, {"page": template.split(".")[0], **ctx})


@app.get("/health")
def health():
    connect().execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def home(request: Request, q: str = "", tipo: str = ""):
    conn = connect()
    docs = store.search(conn, q, tipo if tipo in KINDS else "")
    return render(request, "papeles.html", docs=docs, q=q, tipo=tipo, counts=store.counts(conn),
                  soon=store.upcoming(conn) if not q and not tipo else [])


@app.get("/subir")
def upload_form(request: Request):
    return render(request, "subir.html", results=None)


async def _receive(files: list[UploadFile]) -> list[dict]:
    conn = connect()
    out = []
    for f in files:
        try:
            if f.size and f.size > store.MAX_BYTES:  # known before reading it into memory
                raise ValueError("Es muy grande (más de 40 MB).")
            data = await f.read(store.MAX_BYTES + 1)
            # OCR takes seconds: in a worker thread, so the app keeps answering meanwhile.
            doc, new = await run_in_threadpool(store.save, conn, f.filename or "documento", data, f.content_type)
            out.append({"doc": doc, "new": new})
        except ValueError as e:
            out.append({"name": f.filename, "error": str(e)})
    return out


@app.post("/subir")
async def upload(request: Request, files: list[UploadFile]):
    results = await _receive(files)
    if len(results) == 1 and "doc" in results[0]:
        return RedirectResponse(f"/d/{results[0]['doc']['id']}?nuevo=1", status_code=303)
    return render(request, "subir.html", results=results)


@app.get("/d/{doc_id}")
def doc_page(request: Request, doc_id: int, nuevo: int = 0, msg: str = ""):
    doc = store.get(connect(), doc_id)
    if not doc:
        return RedirectResponse("/", status_code=303)
    return render(request, "papel.html", d=doc, nuevo=nuevo, msg=msg, error=None)


@app.post("/d/{doc_id}")
def doc_save(request: Request, doc_id: int, title: str = Form(""), kind: str = Form("otro"), doc_date: str = Form(""),
             expires: str = Form(""), notes: str = Form("")):
    conn = connect()
    try:
        msg = store.update(conn, doc_id, title, kind, doc_date, expires, notes)
    except ValueError:
        return render(request, "papel.html", d=store.get(conn, doc_id), nuevo=0, msg="", error="Una de las fechas no es válida.")
    return RedirectResponse(f"/d/{doc_id}" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@app.post("/d/{doc_id}/aviso")
def doc_remind(doc_id: int):
    """Confirms the expiry read from the document and creates its reminder."""
    conn = connect()
    doc = store.get(conn, doc_id)
    if doc and doc["expires_found"] and not doc["expires"]:
        conn.execute("UPDATE docs SET expires = expires_found WHERE id = ?", (doc_id,))
        conn.commit()
        return RedirectResponse(f"/d/{doc_id}?msg={quote(store.remind(conn, doc_id))}", status_code=303)
    return RedirectResponse(f"/d/{doc_id}", status_code=303)


@app.post("/d/{doc_id}/borrar")
def doc_delete(doc_id: int):
    store.delete(connect(), doc_id)
    return RedirectResponse("/", status_code=303)


@app.get("/f/{doc_id}")
def doc_file(doc_id: int, bajar: int = 0):
    doc = store.get(connect(), doc_id)
    if not doc:
        return JSONResponse({"error": "No existe."}, status_code=404)
    ext = Path(doc["file"]).suffix
    name = doc["title"] if doc["title"].lower().endswith(ext) else doc["title"] + ext
    return FileResponse(files_dir() / doc["file"], media_type=doc["mime"], filename=name,
                        content_disposition_type="attachment" if bajar else "inline")


@app.get("/ayuda")
def help_page(request: Request):
    return render(request, "ayuda.html")


@app.post("/api/subir")
async def api_upload(files: list[UploadFile]):
    """For the iPhone's share sheet (a Shortcut): one sentence back per file."""
    lines = []
    for r in await _receive(files):
        if "error" in r:
            lines.append(f"{r['name']}: {r['error']}")
            continue
        d = r["doc"]
        line = f"{'Guardado' if r['new'] else 'Ya estaba'}: {d['title']} ({KINDS[d['kind']][0].lower()})."
        if d["expires_found"] and not d["expires"]:
            line += f" Vence el {ddmmyyyy(d['expires_found'])}: confirmalo en Papeles para que te avise."
        lines.append(line)
    return PlainTextResponse("\n".join(lines) or "No llegó ningún archivo.")

"""Run inside the container:  docker compose run --rm papeles python -m pytest -q"""
import io
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db, extract, store


def text_pdf(lines: list[str]) -> bytes:
    """A minimal PDF with a real text layer (what a bank or a utility sends)."""
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({l}) '" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def text_image(lines: list[str], fmt: str = "PNG") -> bytes:
    """A 'photo' of a paper: black text on white, big enough for Tesseract."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 120 + 70 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)
    for i, line in enumerate(lines):
        draw.text((60, 60 + 70 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    return db.connect()


@pytest.fixture
def client(conn, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    return TestClient(main.app)


BILL = ["EPEC - Empresa Provincial de Energia de Cordoba", "Factura B 0012-00345678", "Fecha de emision: 05/10/2026",
        "Consumo: 320 kWh", "Total a pagar: $ 45.210,00", "Vencimiento: 28/10/2026", "CAE 74123456789012"]


def test_kind_dates_expiry_title():
    text = "\n".join(BILL)
    assert extract.kind_of(text) == "servicio"
    assert extract.doc_date(text) == date(2026, 10, 5)
    assert extract.expiry_of(text, date(2026, 10, 5)) == date(2026, 10, 28)
    w = "Certificado de garantía\nHeladera No Frost\nFecha de compra: 31/01/2026\nGarantía de 12 meses"
    assert extract.kind_of(w) == "garantia"
    assert extract.expiry_of(w, date(2026, 1, 31)) == date(2027, 1, 31)
    assert extract.expiry_of("garantia de 1 mes", date(2026, 1, 31)) == date(2026, 2, 28)
    assert extract.title_of("IMG_1234.HEIC", w, "garantia", None) == "Certificado de garantía"
    assert extract.title_of("Contrato alquiler 2026.pdf", "", "contrato", None) == "Contrato alquiler 2026"
    assert extract.title_of("scan.pdf", "", "factura", date(2026, 3, 1)) == "Factura del 01/03/2026"
    assert extract.dates_in("vence el 3 de diciembre de 2026")[0][1] == date(2026, 12, 3)
    # AFIP's CAE expiry is not the bill's; a table header puts the issue date under "Vencimiento" too.
    cae = "Fecha de emision: 01/10/2026\nCAE 7412 Fecha de Vto. de CAE: 15/10/2026\nVencimiento: 20/10/2026"
    assert extract.expiry_of(cae, date(2026, 10, 1)) == date(2026, 10, 20)
    table = "Fecha de emision  Vencimiento\n21/10/2026  10/11/2026"
    assert extract.expiry_of(table, date(2026, 10, 21)) == date(2026, 11, 10)
    assert extract.expiry_of("Vencimiento: 10/03/2019", date(2019, 3, 1)) is None  # long gone
    assert extract.expiry_of("periodo hasta el 30/11/2026", None) is None
    assert extract.expiry_of("1 año de garantía", date(2026, 5, 4)) == date(2027, 5, 4)
    assert extract.expiry_of("garantía de un año", date(2026, 5, 4)) == date(2027, 5, 4)


def test_pdf_with_text(conn):
    doc, new = store.save(conn, "factura.pdf", text_pdf(BILL), "application/pdf")
    assert new and doc["kind"] == "servicio" and "45.210" in doc["text"]
    assert doc["expires_found"] == "2026-10-28" and doc["expires"] is None
    assert doc["title"] == "Factura"  # the file's name says what it is
    again, new = store.save(conn, "otra.pdf", text_pdf(BILL), "application/pdf")
    assert not new and again["id"] == doc["id"]


def test_photo_goes_through_ocr(conn):
    doc, _ = store.save(conn, "IMG_0042.png", text_image(["CERTIFICADO DE GARANTIA", "Lavarropas Drean",
                                                          "Fecha 10/02/2026", "Garantia de 12 meses"]), "image/png")
    assert "GARANTIA" in doc["text"].upper()
    assert doc["kind"] == "garantia" and doc["expires_found"] == "2027-02-10"


def test_scanned_pdf_goes_through_ocr(conn):
    from PIL import Image
    img = Image.open(io.BytesIO(text_image(["CONTRATO DE LOCACION", "Las partes acuerdan el alquiler"])))
    buf = io.BytesIO()
    img.save(buf, "PDF")  # a PDF with only a picture in it, like a scanner makes
    doc, _ = store.save(conn, "scan_001.pdf", buf.getvalue(), "application/pdf")
    assert doc["kind"] == "contrato" and "LOCACION" in doc["text"].upper()


def test_search_ignores_accents_and_takes_prefixes(conn):
    store.save(conn, "garantia.pdf", text_pdf(["Certificado de garantia", "Heladera Whirlpool"]), "application/pdf")
    store.save(conn, "luz.pdf", text_pdf(BILL), "application/pdf")
    assert [d["title"] for d in store.search(conn, "garantía heladera")] == ["Garantia"]
    assert len(store.search(conn, "helad")) == 1
    assert len(store.search(conn, "", "servicio")) == 1 and len(store.search(conn)) == 2
    assert store.search(conn, "'\"*") == store.search(conn)  # punctuation only: everything, no SQL error


def test_rejects_what_is_not_a_document(conn):
    with pytest.raises(ValueError):
        store.save(conn, "virus.exe", b"MZ...", "application/octet-stream")
    with pytest.raises(ValueError):
        store.save(conn, "vacio.pdf", b"", "application/pdf")


def test_expiry_creates_the_reminder(conn, monkeypatch):
    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        sent.update(json.loads(req.data))
        return Resp(json.dumps({"id": 7, "texto": "Vence: Factura: 28/10."}).encode())
    monkeypatch.setattr(store.urllib.request, "urlopen", fake_urlopen)
    doc, _ = store.save(conn, "factura.pdf", text_pdf(BILL), "application/pdf")
    msg = store.update(conn, doc["id"], "Luz octubre", "servicio", "2026-10-05", "2026-10-28", "")
    assert "Recordatorios" in msg and sent["fecha"] == "2026-10-28" and sent["avisos"] == [10080, 1440, 0]
    assert sent["titulo"] == "Vence: Luz octubre" and store.get(conn, doc["id"])["reminder_id"] == 7
    calls = []
    real = fake_urlopen
    monkeypatch.setattr(store.urllib.request, "urlopen", lambda req, timeout=0: calls.append(req.full_url) or real(req))
    store.update(conn, doc["id"], "Luz octubre", "servicio", "2026-10-05", "2026-10-30", "")  # a new date
    assert calls[0].endswith("/r/7/borrar") and calls[1].endswith("/api/recordatorio")
    assert store.update(conn, doc["id"], "Luz octubre", "servicio", "2026-10-05", "2026-10-30", "nota") is None  # same date: no new one
    with pytest.raises(ValueError):
        store.update(conn, doc["id"], "x", "servicio", "2026-13-01", "", "")


def test_pages_and_api(client, conn, tmp_path):
    r = client.post("/api/subir", files=[("files", ("factura.pdf", text_pdf(BILL), "application/pdf")),
                                         ("files", ("x.exe", b"MZ", "application/octet-stream"))])
    assert r.text.startswith("Guardado: Factura (servicio). Vence el 28/10/2026") and "x.exe: Solo PDF" in r.text
    doc = store.search(conn)[0]
    for path in ("/", "/?q=epec", "/?tipo=servicio", "/subir", "/ayuda", f"/d/{doc['id']}", f"/f/{doc['id']}", "/health"):
        assert client.get(path).status_code == 200, path
    assert client.get(f"/f/{doc['id']}").headers["content-type"] == "application/pdf"
    assert client.post(f"/d/{doc['id']}", data={"title": "x", "kind": "servicio", "doc_date": "mal", "expires": ""}).status_code == 200
    assert client.get("/ia/buscar?texto=epec factura").json()["cantidad"] == 1
    assert client.get("/ia/buscar?texto=zzz epec").json()["cantidad"] == 1  # retried with the longest word
    assert "buscar_papeles" in client.get("/ia/openapi.json").text
    client.post(f"/d/{doc['id']}/borrar")
    assert not store.search(conn) and not list((tmp_path / "files").iterdir())
    assert client.get("/", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403


def test_csrf_from_another_site_is_refused(client):
    for h, ok in [({"sec-fetch-site": "cross-site"}, False), ({"origin": "https://evil.example"}, False),
                  ({"sec-fetch-site": "same-origin", "origin": "https://mac-server.tailnet-ejemplo.ts.net:3016"}, True),
                  ({}, True)]:  # Siri / curl: no browser headers
        r = client.post("/d/999/borrar", headers=h, follow_redirects=False)
        assert (r.status_code != 403) == ok, h


# --- regressions from the review (Nov 2026) ---

class FakeRecordatorios:
    """Like Recordatorios: /api/recordatorio saves the same title on the same date once (and says so
    with "nuevo"), /r/{id}/borrar deletes."""

    def __init__(self):
        self.rows, self.next = {}, 1

    def urlopen(self, req, timeout=0):
        if req.full_url.endswith("/api/recordatorio"):
            body = json.loads(req.data)
            rid = next((i for i, r in self.rows.items() if (r["titulo"], r["fecha"]) == (body["titulo"], body["fecha"])), None)
            new = rid is None
            if new:
                rid, self.next = self.next, self.next + 1
                self.rows[rid] = body
            return io.BytesIO(json.dumps({"id": rid, "nuevo": new, "texto": body["titulo"]}).encode())
        self.rows.pop(int(req.full_url.split("/r/")[1].split("/")[0]), None)
        return io.BytesIO(b"")


@pytest.fixture
def recordatorios(monkeypatch):
    fake = FakeRecordatorios()
    monkeypatch.setattr(store.urllib.request, "urlopen", fake.urlopen)
    return fake


def test_clearing_the_expiry_removes_its_reminder(conn, recordatorios):
    doc, _ = store.save(conn, "seguro auto.txt", b"Poliza seguro automotor", "text/plain")
    store.update(conn, doc["id"], doc["title"], doc["kind"], "", "2026-12-01", "")
    assert len(recordatorios.rows) == 1
    msg = store.update(conn, doc["id"], doc["title"], doc["kind"], "", "", "")
    assert not recordatorios.rows and "saqué el aviso" in msg and store.get(conn, doc["id"])["reminder_id"] is None


def test_each_paper_gets_its_own_reminder(conn, recordatorios):
    a, _ = store.save(conn, "factura.pdf", b"EPEC luz", "text/plain")
    b, _ = store.save(conn, "factura.txt", b"Ecogas gas", "text/plain")
    assert a["title"] == b["title"] == "Factura"
    store.update(conn, a["id"], a["title"], a["kind"], "", "2026-11-10", "")
    store.update(conn, b["id"], b["title"], b["kind"], "", "2026-11-10", "")
    ra, rb = (store.get(conn, d["id"])["reminder_id"] for d in (a, b))
    assert ra != rb and recordatorios.rows[rb]["titulo"] == f"Vence: Factura (papel {b['id']})"
    store.delete(conn, a["id"])
    assert list(recordatorios.rows) == [rb]


def test_a_shared_reminder_is_kept_while_another_paper_holds_it(conn, recordatorios):
    a, _ = store.save(conn, "a.txt", b"uno", "text/plain")
    b, _ = store.save(conn, "b.txt", b"dos", "text/plain")
    recordatorios.rows[9] = {"titulo": "Vence: x", "fecha": "2026-11-10"}
    conn.execute("UPDATE docs SET reminder_id = 9 WHERE id IN (?, ?)", (a["id"], b["id"]))  # as before the fix
    conn.commit()
    store.delete(conn, a["id"])
    assert 9 in recordatorios.rows
    store.delete(conn, b["id"])
    assert 9 not in recordatorios.rows


@pytest.mark.parametrize("text, issued, expires", [
    # card statement: last month's due date first
    ("Resumen de cuenta Visa\nCierre anterior: 25/09/2026 Vencimiento anterior: 07/10/2026\n"
     "Cierre actual: 23/10/2026 Vencimiento actual: 04/11/2026", date(2026, 9, 25), date(2026, 11, 4)),
    # the due date has no year: the period after it isn't the expiry
    ("Vence el 10/11. Periodo facturado: 01/10/2026 al 31/10/2026", date(2026, 10, 1), None),
    ("Vence el 10/11\nPeriodo 01/10/2026 al 31/10/2026", date(2026, 10, 1), None),
    # a table header with more columns: the value is on the next line
    ("Fecha de emision  Vencimiento  Total a pagar\n21/10/2026  10/11/2026  $ 45.210", date(2026, 10, 21),
     date(2026, 11, 10)),
    ("Su poliza vence el 10 de noviembre del 2026", None, date(2026, 11, 10)),
    ("Vencimiento: 03/10/2026", date(2026, 10, 5), None),  # before the paper's own date
])
def test_expiry_edge_cases(text, issued, expires):
    assert extract.expiry_of(text, issued) == expires

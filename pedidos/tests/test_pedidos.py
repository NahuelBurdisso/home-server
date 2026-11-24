"""Run inside the container:  docker compose run --rm pedidos python -m pytest -q
Nothing here touches the network: pedix and Lista are httpx.MockTransports."""
import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from app import catalogo, db, fetch, lista, match, orders
from app.db import set_meta
from app.text import line_amount, presentation_amount

PAGE = (Path(__file__).parent / "fixtures" / "pedix.html").read_text(encoding="utf-8")
GRIN, PUNTO, VALLE = 1, 2, 3  # the seeded providers


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    return db.connect()


class Pedix:
    """pedix.app as a dict: url → (status, body, headers)."""

    def __init__(self):
        self.pages = {"https://pedix.app/verdufresca": (200, PAGE, {})}
        self.asked = []

    def __call__(self, request: httpx.Request):
        self.asked.append(str(request.url))
        status, body, headers = self.pages.get(str(request.url), (404, "no", {}))
        return httpx.Response(status, text=body, headers=headers)


class Lista:
    """Lista's three endpoints, in memory. down=True: it doesn't answer."""

    def __init__(self, items=()):
        self.items = [dict(i) for i in items]
        self.read = []      # what /api/leer answers
        self.ticked = []
        self.down = False

    def __call__(self, request: httpx.Request):
        if self.down:
            raise httpx.ConnectError("refused")
        path = request.url.path
        if path == "/api/pendientes":
            return httpx.Response(200, json=self.items)
        body = json.loads(request.content or b"{}")
        if path == "/api/leer":
            return httpx.Response(200, json=self.read)
        if path == "/api/tildar":
            self.ticked.append(body["ids"])
            return httpx.Response(200, json={"tildados": len(body["ids"])})
        return httpx.Response(404)


ITEMS = [{"id": 1, "text": "2 kg de papas", "key": "papas", "section": "verduleria"},
         {"id": 2, "text": "Rúcula", "key": "rucula", "section": "verduleria"},
         {"id": 3, "text": "1,5 kg de bananas", "key": "bananas", "section": "verduleria"},
         {"id": 4, "text": "Perejil fresco", "key": "perejil fresco", "section": "verduleria"},
         {"id": 5, "text": "1 kg de asado", "key": "asado", "section": "carniceria"},
         {"id": 6, "text": "Leche", "key": "leche", "section": "lacteos"},
         {"id": 7, "text": "Pilas", "key": "pilas", "section": "ferreteria"}]


@pytest.fixture
def pedix(monkeypatch):
    web = Pedix()
    monkeypatch.setattr(fetch, "transport", httpx.MockTransport(web))
    return web


@pytest.fixture
def fake_lista(monkeypatch):
    fake = Lista(ITEMS)
    monkeypatch.setattr(lista, "transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def grin(conn, pedix):
    """Verdufresca with the fixture's catalog stored."""
    catalogo.store(conn, GRIN, catalogo.parse(PAGE))
    return conn


@pytest.fixture
def client(conn, pedix, fake_lista, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    return TestClient(main.app)


# --- the catalog ---------------------------------------------------------------------------------------

def test_parse_the_real_page_shape():
    cat = catalogo.parse(PAGE)
    assert (cat["name"], cat["phone"], cat["min_purchase"]) == ("Verdufresca", "543515550456", 20000)
    by = {(p["name"], p["presentation"]): p for p in cat["products"]}
    names = {p["name"] for p in cat["products"]}
    assert "CEREZA" not in names and "SANDÍA BABY" not in names  # inactive products
    assert "Ravioles osobuco" not in names                       # inactive category (PASTAS)
    assert by[("MANGO", "")]["price"] == 2749 and by[("MANGO", "")]["pres_id"] == ""
    assert by[("FRUTILLAS", "1/4 KG")]["price"] == 2499.75 and by[("FRUTILLAS", "1/4 KG")]["grams"] == 250
    assert (by[("ANANA", "UNIDAD")]["units"], by[("ANANA", "MEDIA")]["units"]) == (1, .5)
    assert by[("BANANA ECUATORIANA", "500 GR")]["grams"] == 500
    assert by[("REPOLLO BLANCO", "1 MITAD")]["units"] == .5
    assert by[("LIMÓN", "1 KG")]["key"] == "limon"
    assert by[("HUEVO BLANCO MAPLE", "2 MAPLES")]["grams"] is None and by[("HUEVO BLANCO MAPLE", "2 MAPLES")]["units"] is None


@pytest.mark.parametrize("html", ["<html>nada</html>", '<script id="ng-state" type="application/json">{no</script>',
                                  '<script id="ng-state">{"app-request-context-state": {}}</script>', ""])
def test_parse_refuses_what_isnt_a_catalog(html):
    with pytest.raises(catalogo.CatalogError):
        catalogo.parse(html)


@pytest.mark.parametrize("name, amount", [
    ("1 KG", (1000, None)), ("2 KG", (2000, None)), ("1 kg", (1000, None)), ("500 GR", (500, None)),
    ("250 GR", (250, None)), ("500 gr", (500, None)), ("1/2 KG", (500, None)), ("1/2KG", (500, None)),
    ("1/4 kg", (250, None)), ("1/2  kg", (500, None)), ("UNIDAD", (None, 1)), ("1 UNID", (None, 1)),
    ("1 unid", (None, 1)), ("1 UNIDAD", (None, 1)), ("MEDIA", (None, .5)), ("1 MITAD", (None, .5)),
    ("1/2 UNIDAD", (None, .5)), ("1/2 SANDÍA", (None, .5)), ("2 MAPLES", (None, None)), ("1 MAPLE BLANCO", (None, None)),
])
def test_presentation_amounts(name, amount):
    assert presentation_amount(name) == amount


@pytest.mark.parametrize("text, amount", [
    ("2 kg de papas", (2000, None)), ("1,5 kg de bananas", (1500, None)), ("1.5 kg de papas", (1500, None)),
    ("Un kilo y medio de papas", (1500, None)), ("Medio kilo de tomates", (500, None)), ("½ kg de queso", (500, None)),
    ("500 g de champignon", (500, None)), ("2kg de cebolla", (2000, None)), ("3 bananas", (None, 3)),
    ("Una lechuga", (None, 1)), ("2 atados de rúcula", (None, 2)), ("Media ananá", (None, .5)),
    ("Leche", (None, None)), ("2 litros de leche", (None, None)), ("Unas galletitas", (None, None)),
    ("Medialunas", (None, None)),
])
def test_line_amounts(text, amount):
    assert line_amount(text) == amount


# --- matching --------------------------------------------------------------------------------------------

def _m(conn, text, key):
    return [(ln["product"], ln["presentation"], ln["qty"]) for ln in match.match(conn, GRIN, text, key)]


def test_matching_and_amounts(grin):
    assert _m(grin, "2 kg de papas", "papas") == [("Papa cepillada", "2 KG", 1)]  # plural → "PAPA ...", ranked by name
    assert _m(grin, "1,5 kg de bananas", "bananas") == [("Banana ecuatoriana", "1 KG", 1),
                                                         ("Banana ecuatoriana", "500 GR", 1)]  # two lines
    assert _m(grin, "3 kg de cebolla", "cebolla") == [("Cebolla", "1 KG", 3)]
    assert _m(grin, "Bananas", "bananas") == [("Banana ecuatoriana", "1 KG", 1)]   # no amount: the first
    assert _m(grin, "3 bananas", "bananas") == [("Banana ecuatoriana", "1 KG", 1)]  # sold by weight: as written
    assert _m(grin, "2 ananás", "ananas") == [("Anana", "UNIDAD", 2)]
    assert _m(grin, "1 kg de ananá", "anana") == [("Anana", "UNIDAD", 1)]          # kg asked, sold by unit
    assert _m(grin, "Media ananá", "anana") == [("Anana", "MEDIA", 1)]
    assert _m(grin, "3 mangos", "mangos") == [("Mango", "", 3)]                     # no presentations: its own price
    assert _m(grin, "300 g de champignon", "champignon") == [("Champignon", "250 GR", 2)]  # leftover rounds up
    assert _m(grin, "Pan", "pan") == []                                            # not found: goes as written
    assert _m(grin, "Huevos", "huevos") == [("Huevo blanco docena", "", 1)]


def test_ranking(grin):
    cat = match.catalog(grin, GRIN)
    names = lambda key: [cat[e][0]["name"] for e in match.candidates(cat, key)]  # noqa: E731
    assert names("tomate perita") == ["TOMATE PERITA"]                           # every word
    assert names("tomates") == ["TOMATE CHERRY", "TOMATE PERITA"]               # equal extra words: by name
    assert names("cebolla") == ["CEBOLLA"]
    assert names("rucula") == ["RUCULA"]
    assert names("papa") == ["PAPA CEPILLADA", "PAPA LAVADA"]
    assert names("lechuga") == ["LECHUGA MANTECOSA"]
    assert names("huevo") == ["HUEVO BLANCO DOCENA", "HUEVO BLANCO MAPLE"]
    assert names("blanco huevo")[:1] == ["HUEVO BLANCO DOCENA"]


def test_prefs_win(grin):
    cat = match.catalog(grin, GRIN)
    perita = next(e for e, rows in cat.items() if rows[0]["name"] == "TOMATE PERITA")
    half = next(r["pres_id"] for r in cat[perita] if r["presentation"] == "500 GR")
    match.remember(grin, GRIN, "tomates", perita, half)
    assert _m(grin, "Tomates", "tomates") == [("Tomate perita", "500 GR", 1)]     # its presentation, no amount
    assert _m(grin, "1 kg de tomates", "tomates") == [("Tomate perita", "1 KG", 1)]  # an amount is still covered
    match.remember(grin, GRIN, "tomates", "gone", "")
    assert _m(grin, "Tomates", "tomates") == [("Tomate cherry", "1 KG", 1)]       # the product went away


# --- building drafts ------------------------------------------------------------------------------------

def _lines(conn, oid):
    return [(r["text"], r["product"], r["presentation"], r["qty"], r["lista_id"]) for r in orders.lines(conn, oid)]


def test_armar_splits_by_sections(conn, pedix, fake_lista):
    res = orders.armar(conn, quien="nahuel")
    assert res["error"] is None and res["sin_proveedor"] == ["Pilas"]
    by = {conn.execute("SELECT provider_id FROM orders WHERE id = ?", (o,)).fetchone()[0]: o for o in res["orders"]}
    assert set(by) == {GRIN, PUNTO, VALLE}
    assert _lines(conn, by[GRIN]) == [("2 kg de papas", "Papa cepillada", "2 KG", 1, 1), ("Rúcula", "Rucula", "", 1, 2),
                                      ("1,5 kg de bananas", "Banana ecuatoriana", "1 KG", 1, 3),
                                      ("1,5 kg de bananas", "Banana ecuatoriana", "500 GR", 1, 3),
                                      ("Perejil fresco", "", "", 1, 4)]
    assert _lines(conn, by[PUNTO]) == [("1 kg de asado", "", "", 1, 5)]  # no catalog: as written
    assert _lines(conn, by[VALLE]) == [("Leche", "", "", 1, 6)]
    assert conn.execute("SELECT total_est FROM orders WHERE id = ?", (by[GRIN],)).fetchone()[0] == 6999 + 2530 + 4500 + 2250
    assert pedix.asked == ["https://pedix.app/verdufresca"]
    again = orders.armar(conn)  # replaces each shop's draft; the catalog is fresh (< 3 h): not read again
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'borrador'").fetchone()[0] == 3
    assert set(again["orders"]).isdisjoint(res["orders"]) and len(pedix.asked) == 1


def test_armar_one_provider_and_text(conn, pedix, fake_lista):
    res = orders.armar(conn, proveedor="carne")
    assert len(res["orders"]) == 1 and _lines(conn, res["orders"][0]) == [("1 kg de asado", "", "", 1, 5)]
    assert orders.find_provider(conn, "carnicería")["id"] == PUNTO
    assert orders.find_provider(conn, "al súper")["id"] == VALLE
    assert orders.find_provider(conn, "supermercado")["id"] == VALLE
    assert orders.find_provider(conn, "la verdulería")["id"] == GRIN
    assert orders.find_provider(conn, "verdufresca")["id"] == GRIN
    fake_lista.read = [{"text": "2 kg de papas", "key": "papas", "section": "verduleria"},
                       {"text": "Leche", "key": "leche", "section": "lacteos"}]
    res = orders.armar(conn, texto="2 kg de papas y leche", proveedor="verdulería")
    assert [(t, lid) for t, _, _, _, lid in _lines(conn, res["orders"][0])] == [("2 kg de papas", None), ("Leche", None)]
    assert orders.armar(conn, proveedor="ferretería")["error"].startswith("No conozco el proveedor")
    fake_lista.items = []
    assert orders.armar(conn)["error"] == "No hay nada pendiente en la Lista para pedir."
    fake_lista.down = True
    with pytest.raises(orders.Unavailable):
        orders.armar(conn)


def test_catalog_that_cant_be_read_keeps_the_old_one(grin, pedix, fake_lista):
    pedix.pages["https://pedix.app/verdufresca"] = (500, "error", {})
    p = grin.execute("SELECT * FROM providers WHERE id = ?", (GRIN,)).fetchone()
    assert catalogo.refresh(grin, p, force=True) == "pedix respondió con un error (500)."
    p = grin.execute("SELECT * FROM providers WHERE id = ?", (GRIN,)).fetchone()
    assert p["catalog_error"] and p["catalog_at"]
    assert grin.execute("SELECT COUNT(*) FROM products WHERE provider_id = ?", (GRIN,)).fetchone()[0] > 10
    pedix.pages["https://pedix.app/verdufresca"] = (200, "<html>rediseño</html>", {})
    assert "catálogo" in catalogo.refresh(grin, p, force=True)
    assert grin.execute("SELECT COUNT(*) FROM products WHERE provider_id = ?", (GRIN,)).fetchone()[0] > 10


# --- the message, the minimum, the status ------------------------------------------------------------------

def test_message_and_whatsapp_link(conn, pedix, fake_lista):
    """A pedix shop reads the message its own page would write: blocks, categories in capitals, prices."""
    from datetime import datetime
    fake_lista.items = ITEMS[:4]
    oid = orders.armar(conn)["orders"][0]
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    p = orders.provider_of(conn, order)
    msg = orders.pedix_message(conn, order, p, datetime(2026, 11, 13, 10, 56))
    assert msg.startswith("¡Hola! Te paso el resumen de mi pedido\n\nTienda: verdufresca\nFecha: 13/11/26 - 10:56hs\n\n"
                          "Total: $16.279\n\nEntrega: Delivery\n\nMi pedido es\n\n")  # nothing set in Ajustes
    assert msg.endswith("\n\nOTROS\nPerejil fresco\n\nTOTAL: $16.279\n\nEspero tu respuesta para confirmar mi pedido")
    body = msg.split("Mi pedido es\n\n")[1]
    cats = {r["ext_id"]: r["category"].upper() for r in conn.execute("SELECT * FROM products WHERE provider_id = ?", (GRIN,))}
    banana = conn.execute("SELECT ext_id FROM lines WHERE order_id = ? AND product = 'Banana ecuatoriana'", (oid,)).fetchone()[0]
    group = next(g for g in body.split("\n\n") if g.startswith(cats[banana] + "\n"))
    assert "1x BANANA ECUATORIANA (1 KG): $4.500" in group and "1x BANANA ECUATORIANA (500 GR): $2.250" in group
    assert "1x PAPA CEPILLADA (2 KG): $" in body and "Pedido: #" not in msg and "Ubicación" not in msg
    for k, v in (("direccion", "Calle Falsa 123"), ("nombre", "Nahuel"), ("telefono", "351 555 1234"),
                 ("pago", "Efectivo"), ("referencia", "Dúplex A"), ("aclaracion", "Timbre 2 & pago justo")):
        set_meta(conn, k, v)
    msg = orders.pedix_message(conn, order, p, datetime(2026, 11, 13, 10, 56))
    assert "Fecha: 13/11/26 - 10:56hs\nNombre: Nahuel\nTeléfono: 351 555 1234\n\nForma de pago: Efectivo\nTotal: $16.279\n\n" \
        "Entrega: Delivery\nDirección: Calle Falsa 123\nReferencia: Dúplex A\n\nMi pedido es" in msg
    assert msg.endswith("TOTAL: $16.279\n\nTimbre 2 & pago justo\n\nEspero tu respuesta para confirmar mi pedido")
    assert orders.pedix_money(4999.5) == "$4.999,50" and orders.pedix_money(37992) == "$37.992"
    links = orders.send_links(conn, order)
    assert links["whatsapp"].startswith("https://wa.me/543515550456?text=" + quote("¡Hola! Te paso el resumen", safe=""))
    assert "%0A" in links["whatsapp"] and "%26" in links["whatsapp"] and " " not in links["whatsapp"]
    assert links["tienda"] == "https://pedix.app/verdufresca" and links["falta"] is None


def test_whatsapp_shop_message(conn, pedix, fake_lista):
    """A shop that only takes WhatsApp gets the plain list, signed."""
    fake_lista.items = [ITEMS[4]]
    oid = orders.armar(conn)["orders"][0]
    set_meta(conn, "direccion", "Calle Falsa 123")
    set_meta(conn, "nombre", "Nahuel")
    set_meta(conn, "telefono", "351 555 1234")  # only for pedix shops
    assert orders.message(conn, oid) == "Hola! Quiero hacer un pedido a domicilio:\n• 1 kg de asado\n" \
        "Entrega: Calle Falsa 123\nGracias! Nahuel"
    set_meta(conn, "aclaracion", "Timbre 2")
    assert orders.message(conn, oid).endswith("Entrega: Calle Falsa 123\nTimbre 2\nGracias! Nahuel")
    set_meta(conn, "referencia", "Dúplex A")
    assert "Entrega: Calle Falsa 123 (Dúplex A)\nTimbre 2" in orders.message(conn, oid)


def test_as_written_lines_in_the_message(conn, pedix, fake_lista):
    fake_lista.items = [ITEMS[4]]
    oid = orders.armar(conn)["orders"][0]
    assert orders.message(conn, oid) == "Hola! Quiero hacer un pedido a domicilio:\n• 1 kg de asado\nGracias!"
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    assert orders.send_links(conn, order)["whatsapp"].startswith("https://wa.me/5493515550123?text=Hola%21")


def test_minimum_purchase(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:1]
    oid = orders.armar(conn)["orders"][0]
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    assert orders.short_of_minimum(conn, order) == 20000 - 6999
    assert "El mínimo es $ 20.000: faltan $ 13.001." in orders.block(conn, oid)
    conn.execute("UPDATE lines SET qty = 3 WHERE order_id = ?", (oid,))
    assert orders.short_of_minimum(conn, order) == 0


def test_unconfigured_provider(conn, pedix, fake_lista):
    conn.execute("INSERT INTO providers (name, kind, sections) VALUES ('Kiosco', 'whatsapp', 'ferreteria')")
    conn.commit()
    res = orders.armar(conn, proveedor="kiosco")
    oid = res["orders"][0]
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    links = orders.send_links(conn, order)
    assert links["whatsapp"] is None and links["falta"] == "Le falta el teléfono de WhatsApp."
    assert "Le falta el teléfono de WhatsApp. Cargalo en" in orders.block(conn, oid)
    assert "wa.me" not in orders.block(conn, oid)


def test_status_ticks_lista(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:3]
    oid = orders.armar(conn)["orders"][0]
    assert orders.set_status(conn, oid, "recibido") == "Listo: llegó."  # straight from a draft is fine too
    oid = orders.armar(conn)["orders"][0]
    assert orders.set_status(conn, oid, "enviado") == "Listo, quedó como pedido y lo tildé en la Lista."
    assert fake_lista.ticked == [[1, 2, 3]]  # bananas are two lines, one Lista item
    assert orders.set_status(conn, oid, "enviado") == "Ese pedido ya está pedido."
    assert orders.set_status(conn, oid, "cancelado") == "Pedido cancelado."  # a sent one too
    assert fake_lista.ticked == [[1, 2, 3]]  # nothing unticked or ticked again
    oid = orders.armar(conn)["orders"][0]
    fake_lista.down = True
    assert "No pude tildarlo en la Lista" in orders.set_status(conn, oid, "enviado")
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0] == "enviado"


def test_lo_de_siempre(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:2]
    for _ in range(2):
        orders.set_status(conn, orders.armar(conn)["orders"][0], "enviado")
    fake_lista.items = ITEMS[:1]
    oid = orders.armar(conn)["orders"][0]
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    assert [u["product"] for u in orders.usual(conn, order)] == ["Rucula"]


# --- fetching: only pedix.app ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, ok", [
    ("https://pedix.app/verdufresca", "https://pedix.app/verdufresca"),
    ("pedix.app/verdufresca", "https://pedix.app/verdufresca"),
    ("https://pedix.app/verdufresca/?utm_source=wa", "https://pedix.app/verdufresca"),
    ("https://PEDIX.app/verdufresca", "https://pedix.app/verdufresca"),
    ("http://pedix.app/verdufresca", None), ("https://pedix.app.evil.com/x", None), ("https://evil.com/pedix.app", None),
    ("https://pedix.app@127.0.0.1/x", None), ("https://x@pedix.app/x", None), ("https://pedix.app:8443/x", None),
    ("https://pedix.app/a/b", None), ("https://pedix.app/", None), ("https://pedix.app/Verdu_Fresca", None),
    ("https://localhost:3016/api/pendientes", None), ("ftp://pedix.app/x", None), ("", None),
])
def test_pedix_url_allowlist(url, ok):
    assert fetch.pedix_url(url) == ok


def test_fetch_never_leaves_pedix(pedix):
    pedix.pages["https://pedix.app/verdufresca"] = (302, "", {"location": "https://evil.example/steal"})
    with pytest.raises(fetch.FetchError):
        fetch.get("https://pedix.app/verdufresca")
    assert pedix.asked == ["https://pedix.app/verdufresca"]
    pedix.pages["https://pedix.app/verdufresca"] = (301, "", {"location": "/otra"})
    pedix.pages["https://pedix.app/otra"] = (200, "ok", {})
    assert fetch.get("https://pedix.app/verdufresca") == "ok"  # same host: followed
    pedix.pages["https://pedix.app/grande"] = (200, "x" * (fetch.MAX_BYTES + 1), {})
    with pytest.raises(fetch.FetchError, match="grande"):
        fetch.get("https://pedix.app/grande")
    with pytest.raises(fetch.FetchError):
        fetch.get("http://127.0.0.1:3016/api/pendientes")
    assert all(u.startswith("https://pedix.app/") for u in pedix.asked)


def test_user_agent_is_a_browser(monkeypatch):
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text="ok")
    monkeypatch.setattr(fetch, "transport", httpx.MockTransport(handler))
    fetch.get("https://pedix.app/verdufresca")
    assert seen["ua"].startswith("Mozilla/5.0")


# --- the app: pages, API, access ----------------------------------------------------------------------------

def test_api_text_answers(client, conn, fake_lista):
    r = client.post("/api/armar", json={"quien": "Sofía"})
    ids = [int(i) for i in r.headers["X-Ids"].split(",")]
    assert r.status_code == 200 and len(ids) == 3
    assert "Verdufresca: 4 cosas, $ 16.279 estimado." in r.text
    assert "No está en el catálogo (va como está escrito): perejil fresco." in r.text
    assert "El mínimo es $ 20.000: faltan $ 3.721." in r.text
    assert f"Revisalo: https://mac-server.tailnet-ejemplo.ts.net:3022/p/{ids[0]}" in r.text
    assert "Para mandarlo: https://wa.me/543515550456?text=" in r.text
    assert "Sin proveedor (quedan en la Lista): Pilas." in r.text
    assert conn.execute("SELECT created_by FROM orders WHERE id = ?", (ids[0],)).fetchone()[0] == "Sofía"
    assert client.get("/api/pedidos").text.startswith("Hay 3 pedidos abiertos. ")
    assert client.post(f"/p/{ids[1]}/cancelar").status_code == 200  # the bot's "borrar" (redirect followed)
    assert client.get("/api/pedidos").text.startswith("Hay 2 pedidos abiertos. ")
    assert client.get("/api/precio", params={"q": "banana"}).text == \
        "Banana ecuatoriana en Verdufresca: $ 4.500 el 1 KG, $ 2.250 el 500 GR. Son precios estimados: pesan y cobran ellos."
    assert client.get("/api/precio", params={"q": "caviar"}).text == "No encontré «caviar» en los catálogos."
    r = client.post("/api/armar", content="2 kg de papas".encode())  # a plain body: the things themselves
    assert r.status_code == 200
    fake_lista.down = True
    r = client.post("/api/armar", json={})
    assert r.status_code == 200 and r.text.startswith("No pude leer la Lista") and r.headers["X-Ids"] == ""
    r = client.post("/armar", data={}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_pages_and_editing(client, conn, fake_lista):
    fake_lista.items = ITEMS[:3]
    r = client.post("/armar", data={"proveedor": str(GRIN)}, follow_redirects=False)
    oid = int(r.headers["location"].split("/p/")[1].split("?")[0])
    page = client.get(f"/p/{oid}").text
    assert "Papa cepillada" in page and "wa.me/543515550456" in page and "Lo de siempre" not in page
    papas = orders.lines(conn, oid)[0]
    lavada = next(r for r in conn.execute("SELECT * FROM products WHERE name = 'PAPA LAVADA' AND presentation = '1 KG'"))
    client.post(f"/l/{papas['id']}/cambiar", data={"elegido": f"{lavada['ext_id']}|{lavada['pres_id']}"})
    assert orders.lines(conn, oid)[0]["product"] == "Papa lavada"
    assert conn.execute("SELECT ext_id FROM prefs WHERE key = 'papas'").fetchone()[0] == lavada["ext_id"]  # remembered
    client.post(f"/l/{papas['id']}/mas")
    assert orders.lines(conn, oid)[0]["qty"] == 2
    client.post(f"/l/{papas['id']}/menos")
    client.post(f"/l/{papas['id']}/menos")
    assert all(r["id"] != papas["id"] for r in orders.lines(conn, oid))
    client.post(f"/p/{oid}/agregar", data={"texto": "1 kg de cebolla"})
    assert orders.lines(conn, oid)[-1]["product"] == "Cebolla"
    assert conn.execute("SELECT total_est FROM orders WHERE id = ?", (oid,)).fetchone()[0] == orders.total(conn, oid)
    client.post(f"/p/{oid}/enviado")
    assert fake_lista.ticked == [[2, 3]]
    client.post(f"/l/{orders.lines(conn, oid)[0]['id']}/mas")  # sent: no more edits
    assert orders.lines(conn, oid)[0]["qty"] == 1
    client.post(f"/p/{oid}/recibido")
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0] == "recibido"
    for path in ("/", f"/p/{oid}", "/historial", "/proveedores", "/ajustes", "/ayuda", "/health", "/ia/openapi.json"):
        assert client.get(path).status_code == 200, path
    assert all(op in client.get("/ia/openapi.json").text for op in ("ver_pedidos", "armar_pedido", "precio_de"))
    assert client.get("/ia/precio", params={"q": "papa"}).json()["encontrado"]


def test_providers_page(client, conn):
    r = client.post("/proveedores", data={"nombre": "", "link": "https://pedix.app/verdufresca", "sections": ["otros"]})
    assert "Leí su catálogo" in r.text
    r = client.post("/proveedores", data={"nombre": "Pollería", "link": "+54 9 351 123-4567", "sections": ["carniceria"]})
    p = conn.execute("SELECT * FROM providers WHERE name = 'Pollería'").fetchone()
    assert (p["kind"], p["phone"], p["sections"]) == ("whatsapp", "5493511234567", "carniceria")
    client.post("/proveedores", data={"nombre": "Súper web", "link": "https://super.example/", "sections": []})
    assert conn.execute("SELECT kind FROM providers WHERE name = 'Súper web'").fetchone()[0] == "web"
    for bad in ("http://super.example", "https://pedix.app/a/b", "123"):
        r = client.post("/proveedores", data={"nombre": "X", "link": bad})
        assert 'class="warn"' in r.text, bad
    client.post(f"/proveedores/{p['id']}", data={"nombre": "Pollería", "phone": "351 123 4567", "minimo": "15.000",
                                                 "sections": ["carniceria", "nada"], "active": "1"})
    p = conn.execute("SELECT * FROM providers WHERE id = ?", (p["id"],)).fetchone()
    assert (p["phone"], p["min_purchase"], p["sections"], p["active"]) == ("5493511234567", 15000, "carniceria", 1)
    r = client.post(f"/proveedores/{GRIN}", data={"nombre": "Verdufresca", "url": "https://evil.example/verdufresca"})
    assert "pedix.app" in r.text
    assert conn.execute("SELECT url FROM providers WHERE id = ?", (GRIN,)).fetchone()[0] == "https://pedix.app/verdufresca"
    client.post("/ajustes", data={"direccion": " Calle  1 ", "nombre": "Nahuel", "aclaracion": ""})
    assert db.get_meta(conn, "direccion") == "Calle 1"


def test_access(client):
    assert client.get("/", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    assert client.get("/", headers={"tailscale-user-login": "otro@github", "x-forwarded-for": "x"}).status_code == 403
    assert client.get("/", headers={"tailscale-user-login": "yo@github", "x-forwarded-for": "x"}).status_code == 200
    assert client.get("/health", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200
    for h, ok in [({"sec-fetch-site": "cross-site"}, False), ({"origin": "https://evil.example"}, False),
                  ({"sec-fetch-site": "same-origin", "origin": "https://mac-server.tailnet-ejemplo.ts.net:3022"}, True),
                  ({}, True)]:  # the bot / curl: no browser headers
        r = client.post("/ajustes", data={"nombre": "x"}, headers=h, follow_redirects=False)
        assert (r.status_code != 403) == ok, h


def test_seed_and_its_upgrade(tmp_path):
    conn = db.connect(str(tmp_path / "new.db"))
    assert [tuple(r) for r in conn.execute("SELECT name, kind, phone FROM providers ORDER BY id")] == [
        ("Verdufresca", "pedix", ""), ("Carnes del Centro", "whatsapp", "5493515550123"),
        ("Almacén del Centro", "whatsapp", "5493515550789")]
    conn.execute("DELETE FROM providers")
    conn.commit()
    assert db.connect(str(tmp_path / "new.db")).execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 0
    old = db.connect(str(tmp_path / "old.db"))  # as the first seed left it: placeholders
    old.executescript("UPDATE providers SET name = 'Carnicería', phone = '' WHERE id = 2;"
                      "UPDATE providers SET name = 'Súper', kind = 'web', phone = '' WHERE id = 3;"
                      "UPDATE meta SET value = '1' WHERE key = 'seeded';")
    again = db.connect(str(tmp_path / "old.db"))
    assert [r[0] for r in again.execute("SELECT name FROM providers ORDER BY id")] == \
        ["Verdufresca", "Carnes del Centro", "Almacén del Centro"]
    assert again.execute("SELECT kind, phone FROM providers WHERE id = 3").fetchone()[:] == ("whatsapp", "5493515550789")


def test_api_order_as_data(client):
    """Claude Code reads a draft with its lines' ids and choices, and changes it with the page's POSTs."""
    oid = int(client.post("/api/armar", json={"proveedor": "verdulería"}).headers["X-Ids"])
    d = client.get(f"/api/p/{oid}").json()
    assert d["proveedor"] == "Verdufresca" and d["estado"] == "Borrador" and d["pagina"].endswith(f"/p/{oid}")
    assert d["mensaje"].startswith("¡Hola! Te paso el resumen de mi pedido")
    assert d["mandar"]["whatsapp"].startswith("https://wa.me/")
    line = next(r for r in d["renglones"] if r["en_catalogo"])
    assert line["opciones"] and any(o["on"] for o in line["opciones"])
    client.post(f"/l/{line['id']}/mas")
    after = next(r for r in client.get(f"/api/p/{oid}").json()["renglones"] if r["id"] == line["id"])
    assert after["cantidad"] == line["cantidad"] + 1
    assert client.get("/api/p/99999").status_code == 404


# --- guardados ------------------------------------------------------------------------------------------

def _get(conn, oid):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()


def _status(conn, oid):
    return _get(conn, oid)["status"]


def test_save_copies_and_replaces(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:4]
    oid = orders.armar(conn)["orders"][0]
    sid = orders.save(conn, _get(conn, oid), "", "nahuel")
    s = _get(conn, sid)
    assert (s["status"], s["name"], s["provider_id"], s["created_by"]) == ("guardado", "Verdufresca", GRIN, "nahuel")
    assert [ln[:4] for ln in _lines(conn, sid)] == [ln[:4] for ln in _lines(conn, oid)]
    assert all(ln[4] is None for ln in _lines(conn, sid)) and all(ln[4] for ln in _lines(conn, oid))  # no lista_id
    assert s["total_est"] == 16279 and _status(conn, oid) == "borrador"  # the draft stays as it was
    named = orders.save(conn, _get(conn, oid), "  Lo de   siempre ")
    assert _get(conn, named)["name"] == "Lo de siempre" and named != sid
    conn.execute("DELETE FROM lines WHERE id = ?", (orders.lines(conn, oid)[0]["id"],))
    conn.commit()
    again = orders.save(conn, _get(conn, oid), "LO DE SIEMPRE")  # same name at the same shop: replaced
    assert again == named and len(orders.lines(conn, again)) == 4 and _get(conn, again)["name"] == "LO DE SIEMPRE"
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'guardado'").fetchone()[0] == 2
    assert orders.save(conn, _get(conn, again), "otro") == again  # a guardado isn't saved again
    assert len(_get(conn, orders.save(conn, _get(conn, oid), "x" * 80))["name"]) == 60
    orders.set_status(conn, oid, "recibido")
    assert _get(conn, orders.save(conn, _get(conn, oid), "Llegó"))["status"] == "guardado"  # past orders too


def test_saved_order_edited_on_its_page(client, conn, fake_lista):
    fake_lista.items = ITEMS[:3]
    r = client.post("/armar", data={"proveedor": str(GRIN)}, follow_redirects=False)
    oid = int(r.headers["location"].split("/p/")[1].split("?")[0])
    assert "Guardar para repetir" in client.get(f"/p/{oid}").text
    r = client.post(f"/p/{oid}/guardar", data={"nombre": "Lo de siempre"}, follow_redirects=False)
    sid = int(r.headers["location"].split("/p/")[1].split("?")[0])
    assert sid != oid and "ok=" in r.headers["location"]
    page = client.get(f"/p/{sid}").text
    assert "Lo de siempre" in page and "Usar este pedido" in page and "Borrar el guardado" in page
    assert "Mandar por WhatsApp" not in page and "Cancelar el pedido" not in page and "Guardar para repetir" not in page
    papas = orders.lines(conn, sid)[0]
    client.post(f"/l/{papas['id']}/mas")
    assert orders.lines(conn, sid)[0]["qty"] == 2
    client.post(f"/l/{papas['id']}/menos")
    assert orders.lines(conn, sid)[0]["qty"] == 1
    lavada = conn.execute("SELECT * FROM products WHERE name = 'PAPA LAVADA' AND presentation = '1 KG'").fetchone()
    client.post(f"/l/{papas['id']}/cambiar", data={"elegido": f"{lavada['ext_id']}|{lavada['pres_id']}"})
    assert orders.lines(conn, sid)[0]["product"] == "Papa lavada"
    client.post(f"/l/{papas['id']}/borrar")
    assert all(r["id"] != papas["id"] for r in orders.lines(conn, sid))
    client.post(f"/p/{sid}/agregar", data={"texto": "1 kg de cebolla"})
    assert orders.lines(conn, sid)[-1]["product"] == "Cebolla"
    assert _get(conn, sid)["total_est"] == orders.total(conn, sid)
    assert len(orders.lines(conn, oid)) == 4  # the draft it came from is untouched
    client.post(f"/p/{sid}/nombre", data={"nombre": " Verdu  semanal "})
    assert _get(conn, sid)["name"] == "Verdu semanal"
    r = client.post(f"/p/{sid}/nombre", data={"nombre": "  "}, follow_redirects=False)
    assert "error=" in r.headers["location"] and _get(conn, sid)["name"] == "Verdu semanal"
    assert not orders.rename(conn, oid, "Un borrador")  # only a guardado has a name
    home = client.get("/").text
    assert "Guardados" in home and "Verdu semanal" in home and f'action="/p/{sid}/usar"' in home


def test_delete_saved(client, conn, fake_lista):
    fake_lista.items = ITEMS[:3]
    oid = orders.armar(conn)["orders"][0]
    sid = orders.save(conn, _get(conn, oid), "Lo de siempre")
    r = client.post(f"/p/{oid}/borrar", follow_redirects=False)  # not a guardado: nothing deleted
    assert _get(conn, oid) is not None and r.headers["location"].startswith(f"/p/{oid}")
    r = client.post(f"/p/{sid}/borrar", follow_redirects=False)
    assert r.headers["location"] == "/?ok=" + quote("Borré el guardado.")
    assert _get(conn, sid) is None and not orders.lines(conn, sid)
    assert "Guardados" not in client.get("/").text


def test_use_without_a_draft_takes_todays_prices(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:3]
    oid = orders.armar(conn)["orders"][0]
    sid = orders.save(conn, _get(conn, oid), "Lo de siempre")
    orders.set_status(conn, oid, "cancelado")
    papas = orders.lines(conn, sid)[0]
    conn.execute("UPDATE products SET price = 8000 WHERE provider_id = ? AND ext_id = ? AND pres_id = ?",
                 (GRIN, papas["ext_id"], papas["pres_id"]))
    conn.commit()
    res = orders.use(conn, _get(conn, sid), "sofia")
    assert res["new"] and (res["added"], res["skipped"]) == (4, 0) and res["order"] not in (oid, sid)
    d = _get(conn, res["order"])
    assert (d["status"], d["provider_id"], d["created_by"]) == ("borrador", GRIN, "sofia")
    assert orders.lines(conn, res["order"])[0]["unit_price"] == 8000
    assert d["total_est"] == 8000 + 2530 + 4500 + 2250
    assert all(r["lista_id"] is None for r in orders.lines(conn, res["order"]))
    assert orders.use_sentence(conn, res, _get(conn, sid)).startswith("Armé el borrador con «Lo de siempre».\nVerdufresca: 3 cosas")
    assert len(orders.lines(conn, sid)) == 4  # the guardado stays


def test_use_adds_to_the_draft_there_is(conn, pedix, fake_lista):
    fake_lista.items = ITEMS[:2]  # papas, rúcula
    sid = orders.save(conn, _get(conn, orders.armar(conn)["orders"][0]), "Lo de siempre")
    saved = _get(conn, sid)
    orders.add_line(conn, saved, "1 kg de cebolla")
    orders.add_line(conn, saved, "perejil fresco")  # not in the catalog: as written
    fake_lista.items = [ITEMS[0], ITEMS[2], ITEMS[3]]  # papas, bananas, perejil
    did = orders.armar(conn)["orders"][0]
    before = _lines(conn, did)
    res = orders.use(conn, _get(conn, sid))
    assert res == {"order": did, "added": 2, "skipped": 2, "new": False}
    after = _lines(conn, did)
    assert after[:len(before)] == before  # the draft's own lines, with their lista_id, untouched
    assert [(t, p, lid) for t, p, _, _, lid in after[len(before):]] == [("Rúcula", "Rucula", None),
                                                                       ("1 kg de cebolla", "Cebolla", None)]
    assert orders.use_sentence(conn, res, _get(conn, sid)).split("\n")[0] == \
        "Agregué 2 cosas de «Lo de siempre» al borrador de Verdufresca (2 ya estaban)."
    res = orders.use(conn, _get(conn, sid))
    assert (res["added"], res["skipped"]) == (0, 4)
    assert orders.use_sentence(conn, res, _get(conn, sid)).split("\n")[0] == "Todo lo de «Lo de siempre» ya estaba en el borrador."
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'borrador'").fetchone()[0] == 1
    conn.execute("UPDATE providers SET active = 0 WHERE id = ?", (GRIN,))
    conn.commit()
    with pytest.raises(orders.Invalid, match="no está activo"):
        orders.use(conn, _get(conn, sid))


def test_saved_product_that_left_the_catalog(client, conn, fake_lista):
    fake_lista.items = ITEMS[:2]
    oid = orders.armar(conn)["orders"][0]
    sid = orders.save(conn, _get(conn, oid), "")
    orders.set_status(conn, oid, "cancelado")
    rucula = orders.lines(conn, sid)[1]
    conn.execute("DELETE FROM products WHERE provider_id = ? AND ext_id = ?", (GRIN, rucula["ext_id"]))
    conn.commit()
    assert "va como está escrito" in client.get(f"/p/{sid}").text  # the page reprices
    ln = orders.lines(conn, sid)[1]
    assert (ln["text"], ln["ext_id"], ln["product"], ln["unit_price"]) == ("Rúcula", None, "", None)
    did = int(client.post("/api/usar", json={"nombre": "verdufresca"}).headers["X-Ids"])
    assert [(r["text"], r["ext_id"]) for r in orders.lines(conn, did)][1] == ("Rúcula", None)
    assert _get(conn, sid)["total_est"] == 6999


def test_guardado_never_leaks(client, conn, fake_lista):
    fake_lista.items = ITEMS[:3]
    oid = int(client.post("/api/armar", json={}).headers["X-Ids"])
    sid = orders.save(conn, _get(conn, oid), "Lo de siempre")
    assert client.get("/api/pedidos").text.startswith("Hay 1 pedido abierto. ")
    client.post(f"/p/{oid}/enviado")
    assert f"/p/{oid}" in client.get("/historial").text and f"/p/{sid}" not in client.get("/historial").text
    for op in ("enviado", "recibido", "cancelar"):
        r = client.post(f"/p/{sid}/{op}", follow_redirects=False)
        assert _status(conn, sid) == "guardado" and "ok=" in r.headers["location"]
    assert orders.set_status(conn, sid, "cancelado") == "Es un pedido guardado: usalo para armar un borrador y mandá ese."
    assert fake_lista.ticked == [[1, 2, 3]]  # only the sent draft ticked the Lista
    orders.armar(conn)
    orders.armar(conn)  # replaces drafts, never a guardado
    assert _status(conn, sid) == "guardado" and len(orders.lines(conn, sid)) == 4
    assert client.get("/api/pedidos").text.startswith("Hay 2 pedidos abiertos. ")  # the new draft and the sent one
    assert orders.find_saved(conn, "otra cosa") is None


def test_migration_adds_the_name(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'borrador', created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), sent_at TEXT, received_at TEXT,
        total_est REAL, note TEXT NOT NULL DEFAULT '');
        INSERT INTO orders (provider_id, status, total_est) VALUES (1, 'enviado', 1234);""")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert "name" in {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    assert tuple(conn.execute("SELECT provider_id, status, total_est, name FROM orders").fetchone()) == (1, "enviado", 1234, "")
    db.connect(path)  # twice: nothing to do


def test_api_and_ia_for_guardados(client, conn, fake_lista):
    assert client.get("/api/guardados").text == "No hay pedidos guardados."
    r = client.post("/api/usar", json={"nombre": "lo de siempre"})
    assert r.status_code == 200 and r.text == "No hay pedidos guardados." and r.headers["X-Ids"] == ""
    oid = int(client.post("/api/armar", json={"proveedor": "verdulería"}).headers["X-Ids"])
    r = client.post("/api/guardar", json={"id": oid, "nombre": "Lo de siempre"})
    sid = int(r.headers["X-Ids"])
    assert r.text == f"Guardado como «Lo de siempre». Para usarlo: {orders.link(sid)}"
    assert client.post("/api/guardar", json={"id": sid}).text == "Ya está guardado como «Lo de siempre»."
    assert client.post("/api/guardar", json={"id": 99999}).text == "Ese pedido no existe."
    assert client.post("/api/guardar", content=b"hola").headers["X-Ids"] == ""
    assert client.get("/api/guardados").text == "Hay 1 pedido guardado: «Lo de siempre» (Verdufresca, 4 cosas, $ 16.279)."
    client.post(f"/p/{oid}/cancelar")
    r = client.post("/api/usar", content="Lo de siempre".encode(), headers={"x-quien": "Sof%C3%ADa"})
    did = int(r.headers["X-Ids"])
    assert r.text.startswith("Armé el borrador con «Lo de siempre».\nVerdufresca: 4 cosas, $ 16.279 estimado.")
    assert (_status(conn, did), _get(conn, did)["created_by"]) == ("borrador", "Sofía")
    r = client.post("/api/usar", json={"nombre": "la verdulería"})  # the shop, which has only one
    assert r.text.startswith("Todo lo de «Lo de siempre» ya estaba en el borrador.") and r.headers["X-Ids"] == str(did)
    r = client.post("/api/usar", json={"nombre": "asado"})
    assert r.text == "No encontré «asado» entre los guardados. Están: «Lo de siempre»." and r.headers["X-Ids"] == ""
    d = client.get(f"/api/p/{sid}").json()
    assert (d["nombre"], d["estado"]) == ("Lo de siempre", "Guardado")
    assert any(ln["opciones"] for ln in d["renglones"])
    assert client.get(f"/api/p/{did}").json()["nombre"] == ""
    assert all(op in client.get("/ia/openapi.json").text for op in ("ver_guardados", "usar_guardado"))
    g = client.get("/ia/guardados").json()
    assert g["guardados"] == [{"nombre": "Lo de siempre", "proveedor": "Verdufresca", "cosas": 4,
                               "total_estimado": "$ 16.279", "pagina": orders.link(sid)}]
    r = client.post("/ia/usar", json={"nombre": "lo de siempre"}).json()
    assert r["pedido"] == did and r["respuesta"].startswith("Todo lo de «Lo de siempre»")
    assert client.post("/ia/usar", json={"nombre": "nada"}).json()["respuesta"].startswith("No encontré «nada»")
    r = client.post(f"/p/{sid}/usar", follow_redirects=False)
    assert r.headers["location"] == f"/p/{did}?ok=" + quote("Todo lo de «Lo de siempre» ya estaba en el borrador.")
    conn.execute("UPDATE providers SET active = 0 WHERE id = ?", (GRIN,))
    conn.commit()
    r = client.post(f"/p/{sid}/usar", follow_redirects=False)
    assert r.headers["location"].startswith(f"/p/{sid}?error=")
    assert client.post("/api/usar", json={"nombre": "Lo de siempre"}).text.startswith("Verdufresca no está activo")


def test_saved_prices_follow_the_catalog(client, conn, pedix):
    """A guardado's total in the lists is today's, not the day it was saved; the background job reads
    the catalogs again first, and a catalog that can't be read leaves the prices it had."""
    oid = int(client.post("/api/armar", json={"proveedor": "verdulería"}).headers["X-Ids"])
    sid = orders.save(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone(), "Verano")
    before = conn.execute("SELECT total_est FROM orders WHERE id = ?", (sid,)).fetchone()[0]
    line = conn.execute("SELECT * FROM lines WHERE order_id = ? AND ext_id IS NOT NULL", (sid,)).fetchone()
    conn.execute("UPDATE products SET price = price + 1000 WHERE provider_id = ? AND ext_id = ? AND pres_id = ?",
                 (GRIN, line["ext_id"], line["pres_id"]))
    conn.commit()
    assert orders.money(before + 1000 * line["qty"]) in client.get("/api/guardados").text
    assert conn.execute("SELECT total_est FROM orders WHERE id = ?", (sid,)).fetchone()[0] == before + 1000 * line["qty"]
    # the job: a stale catalog is read again (the fixture's prices come back)
    conn.execute("UPDATE providers SET catalog_at = '2020-01-01 00:00:00' WHERE id = ?", (GRIN,))
    conn.commit()
    asked = len(pedix.asked)
    assert orders.reprice_saved(conn, fetch=True) == 1 and len(pedix.asked) == asked + 1
    assert conn.execute("SELECT total_est FROM orders WHERE id = ?", (sid,)).fetchone()[0] == before
    # pedix down: the old catalog and its prices stay
    pedix.pages.clear()
    conn.execute("UPDATE providers SET catalog_at = '2020-01-01 00:00:00' WHERE id = ?", (GRIN,))
    conn.commit()
    orders.reprice_saved(conn, fetch=True)
    assert conn.execute("SELECT total_est FROM orders WHERE id = ?", (sid,)).fetchone()[0] == before

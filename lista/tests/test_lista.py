"""Run inside the container:  docker compose run --rm lista python -m pytest -q"""
import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.parse import key_of, section_of, split


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    return db.connect()


@pytest.fixture
def client(conn, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    return TestClient(main.app)


@pytest.mark.parametrize("text, things", [
    ("agregá leche, pan y 2 kg de papas a la lista", ["Leche", "Pan", "2 kg de papas"]),
    ("Che, hay que comprar dulce de leche y lavandina", ["Dulce de leche", "Lavandina"]),
    ("falta yerba", ["Yerba"]),
    ("jabón en polvo; shampoo\nalimento para perro", ["Jabón en polvo", "Shampoo", "Alimento para perro"]),
    ("un paquete de fideos más 3 latas de atún", ["Un paquete de fideos", "3 latas de atún"]),
    ("Anotá en la lista de compras pilas", ["Pilas"]),
    ("", []),
])
def test_split(text, things):
    assert split(text) == things


@pytest.mark.parametrize("text, section", [
    ("Leche", "lacteos"), ("Dulce de leche", "almacen"), ("2 kg de papas", "verduleria"),
    ("Jabón en polvo", "limpieza"), ("Jabón", "higiene"), ("Pilas", "otros"), ("Crema de leche", "lacteos"),
    ("Té", "almacen"), ("Tomate triturado", "almacen"), ("Tomates", "verduleria"),
])
def test_sections(text, section):
    assert section_of(text) == section


def test_keys():
    assert key_of("2 kg de papas") == key_of("Papas") == "papas"
    assert key_of("Un paquete de fideos") == "fideos"


def test_add_no_duplicates_and_back_from_the_cart(conn):
    assert store.add(conn, "leche y pan")["added"] == ["Leche", "Pan"]
    assert store.add(conn, "LECHE")["already"] == ["LECHE"]
    leche = conn.execute("SELECT id FROM items WHERE key = 'leche'").fetchone()[0]
    assert store.toggle(conn, leche) is True               # into the cart
    assert store.add(conn, "leche")["added"] == ["Leche"]  # needed again: back on the list
    assert len(store.pending(conn)) == 2 and not store.cart(conn)


def test_tick_by_sentence_matches_whole_words(conn):
    store.add(conn, "té, leche y 2 kg de papas")
    assert sorted(store.tick(conn, "ya compré la leche y las papas")) == ["2 kg de papas", "Leche"]
    assert [r["text"] for r in store.pending(conn)] == ["Té"]  # 'leche' doesn't tick 'té'


def test_finish_history_and_usual(conn):
    for _ in range(2):
        store.add(conn, "yerba, pan")
        for r in store.pending(conn):
            store.toggle(conn, r["id"])
        conn.execute("UPDATE items SET cleared_at = datetime('now', ?) WHERE done_at IS NOT NULL AND cleared_at IS NULL",
                     (f"-{_} minutes",))
        conn.commit()
    assert {u["key"] for u in store.usual(conn)} == {"yerba", "pan"}
    store.add(conn, "yerba")
    assert {u["key"] for u in store.usual(conn)} == {"pan"}  # yerba is already on the list
    assert len(store.trips(conn)) == 2


def test_sentences(conn):
    assert store.list_sentence(conn) == "La lista está vacía."
    store.add(conn, "leche, pan, papas")
    assert store.list_sentence(conn) == "Faltan 3 cosas. Verdulería: papas. Lácteos y fiambres: leche. Panadería: pan."
    assert store.added_sentence({"added": ["Yerba", "Pan"], "already": ["Leche"]}) == "Anotado: yerba y pan. Ya estaba: leche."


def test_pages_and_api(client, conn):
    assert client.post("/api/agregar", content="leche, pan y papas".encode()).text == "Anotado: leche, pan y papas."
    r = client.post("/api/agregar", json={"texto": "yerba"})
    assert r.text == "Anotado: yerba."
    assert conn.execute("SELECT text FROM items WHERE id = ?", (int(r.headers["X-Ids"]),)).fetchone()[0] == "Yerba"
    assert client.post("/api/agregar", content=b"yerba").headers["X-Ids"] == ""  # already there: nothing to take back
    assert client.get("/api/lista").text.startswith("Faltan 4 cosas.")
    for path in ("/", "/historial", "/ayuda", "/health"):
        assert client.get(path).status_code == 200, path
    item = store.pending(conn)[0]["id"]
    assert client.post(f"/i/{item}/tilde", headers={"Accept": "application/json"}).json() == {"ok": True, "done": True}
    assert "En el carrito" in client.get("/").text
    client.post("/terminar")
    assert store.trips(conn) and not store.cart(conn)
    assert client.post("/i/999/tilde", headers={"Accept": "application/json"}).json()["ok"] is False
    assert client.get("/", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    assert client.get("/", headers={"tailscale-user-login": "otro@github", "x-forwarded-for": "x"}).status_code == 403


def test_ai_tools(client):
    assert client.post("/ia/agregar", json={"texto": "leche y yerba"}).json()["agregado"] == ["Leche", "Yerba"]
    assert client.get("/ia/lista").json()["cantidad"] == 2
    assert client.post("/ia/comprado", json={"texto": "ya compré la leche"}).json()["marcado"] == ["Leche"]
    assert "ver_lista_de_compras" in client.get("/ia/openapi.json").text


def test_csrf_from_another_site_is_refused(client):
    for h, ok in [({"sec-fetch-site": "cross-site"}, False), ({"origin": "https://evil.example"}, False),
                  ({"sec-fetch-site": "same-origin", "origin": "https://mac-server.tailnet-ejemplo.ts.net:3016"}, True),
                  ({}, True)]:  # Siri / curl: no browser headers
        r = client.post("/agregar", data={"texto": "pan"}, headers=h, follow_redirects=False)
        assert (r.status_code != 403) == ok, h


@pytest.mark.parametrize("text, things", [
    ("1,5 kg de papas y 2,5 litros de leche", ["1,5 kg de papas", "2,5 litros de leche"]),  # decimal commas
    ("pan, y leche", ["Pan", "Leche"]),
    ("a la lista: pan", ["Pan"]),
    ("también pan", ["Pan"]),
    ("Leche Y Pan", ["Leche", "Pan"]),
    ("fideos E arroz", ["Fideos", "Arroz"]),
    ("agregá ½ kg de queso a la lista", ["½ kg de queso"]),
])
def test_split_edge_cases(text, things):
    assert split(text) == things


@pytest.mark.parametrize("text, key", [
    ("Una lechuga", "lechuga"), ("Unas galletitas", "galletitas"), ("Medialunas", "medialunas"),
    ("Medias", "medias"), ("1/2 kg de queso", "queso"), ("Un par de limones", "limones"), ("1,5 kg de papas", "papas"),
])
def test_amounts_are_whole_words(text, key):
    assert key_of(text) == key


@pytest.mark.parametrize("text, section", [
    ("Yogur de frutilla", "lacteos"), ("Jugo de naranja", "bebidas"), ("Pollo con papas", "carniceria"),
    ("Agua oxigenada", "higiene"), ("Dulce de leche", "almacen"),
])
def test_section_is_the_main_noun(text, section):
    assert section_of(text) == section


def test_tick_only_the_thing_itself(conn):
    store.add(conn, "pan, leche, una lechuga, tomates")
    assert store.tick(conn, "ya compré el pan rallado y el dulce de leche") == []
    assert sorted(store.tick(conn, "ya compré la lechuga y el tomate")) == ["Tomates", "Una lechuga"]


# --- regressions from the review (Nov 2026) ---

@pytest.mark.parametrize("text, things", [
    ("un kilo y medio de papas", ["Un kilo y medio de papas"]),
    ("dos litros y medio de leche y pan", ["Dos litros y medio de leche", "Pan"]),
    ("leche y media docena de huevos", ["Leche", "Media docena de huevos"]),  # "media" of the next thing
    ("pan y medialunas", ["Pan", "Medialunas"]),
    ("comprar más leche", ["Leche"]),
])
def test_split_halves_and_mas(text, things):
    assert split(text) == things


def test_amount_with_a_half():
    assert key_of("Un kilo y medio de papas") == "papas"
    assert section_of("Un kilo y medio de papas") == "verduleria"


@pytest.mark.parametrize("text, section", [("2 leches", "lacteos"), ("3 panes", "panaderia"), ("Yerbas", "almacen"),
                                           ("Papas fritas", "almacen"), ("Pilas", "otros")])
def test_section_in_singular(text, section):
    assert section_of(text) == section


def test_add_singular_and_plural_are_the_same_thing(conn):
    store.add(conn, "leche, papa")
    res = store.add(conn, "manteca, 2 leches, papas y más leche")
    assert (res["added"], res["already"]) == (["Manteca"], ["2 leches", "Papas", "Leche"])
    papa = conn.execute("SELECT id FROM items WHERE key = 'papa'").fetchone()[0]
    store.toggle(conn, papa)  # in the cart
    assert store.add(conn, "2 kg de papas")["ids"] == [papa]  # back on the list, not a second one
    assert sorted(r["text"] for r in store.pending(conn)) == ["2 kg de papas", "Leche", "Manteca"]


def test_api_add_odd_json(client):
    for body in ({"texto": 5}, {"texto": None}, {"texto": ["leche"]}):
        r = client.post("/api/agregar", json=body)
        assert r.status_code == 200 and r.text.startswith("No entendí"), body


OLD_SCHEMA = """
CREATE TABLE items (id INTEGER PRIMARY KEY, text TEXT NOT NULL, key TEXT NOT NULL, section TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')), done_at TEXT, cleared_at TEXT);
CREATE INDEX items_open ON items (cleared_at, done_at);
INSERT INTO items (id, text, key, section, done_at, cleared_at) VALUES
    (1, 'Yerba', 'yerba', 'almacen', '2026-10-01 10:00:00', '2026-10-01 11:00:00'),
    (3, 'Leche', 'leche', 'lacteos', NULL, NULL);
"""


def test_old_database_gets_autoincrement_keeping_everything(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.close()
    conn = db.connect(path)
    assert "AUTOINCREMENT" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()[0]
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'items_open'").fetchone()
    assert [tuple(r) for r in conn.execute("SELECT id, text, done_at, cleared_at FROM items ORDER BY id")] == \
        [(1, "Yerba", "2026-10-01 10:00:00", "2026-10-01 11:00:00"), (3, "Leche", None, None)]
    pan = store.add(conn, "pan")["ids"][0]
    assert pan == 4
    store.delete(conn, pan)
    assert store.add(conn, "queso")["ids"] == [5]  # the deleted id isn't handed out again
    again = db.connect(path)
    assert [tuple(r) for r in again.execute("SELECT name, seq FROM sqlite_sequence")] == [("items", 5)]


# --- the endpoints for Pedidos ---

def test_pedidos_endpoints(client, conn):
    store.add(conn, "leche y 2 kg de papas")
    pend = client.get("/api/pendientes").json()
    assert [(p["text"], p["key"], p["section"]) for p in pend] == [("2 kg de papas", "papas", "verduleria"),
                                                                    ("Leche", "leche", "lacteos")]
    assert all(isinstance(p["id"], int) for p in pend)
    assert client.post("/api/leer", json={"texto": "bananas y rúcula"}).json() == [
        {"text": "Bananas", "key": "bananas", "section": "verduleria"},
        {"text": "Rúcula", "key": "rucula", "section": "verduleria"}]
    assert len(store.pending(conn)) == 2  # reading stores nothing
    ids = [p["id"] for p in pend]
    assert client.post("/api/tildar", json={"ids": ids + [999, "x", True]}).json() == {"tildados": 2}
    assert client.post("/api/tildar", json={"ids": ids}).json() == {"tildados": 0}  # idempotent
    assert not store.pending(conn) and len(store.cart(conn)) == 2
    for body in ({"ids": "1"}, {}, [1, 2]):
        assert client.post("/api/tildar", json=body).json() == {"tildados": 0}
    assert client.post("/api/leer", json={"texto": 5}).json() == []

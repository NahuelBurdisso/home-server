"""Run inside the container:  docker compose run --rm mensajes python -m pytest -q"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, handle, house, ia, people, telegram
from app.db import set_state
from app.intents import read

ME = 111  # the owner's Telegram id in these tests
OTHER = 222
GROUP = -100200300


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    return db.connect()


class Fake:
    """Telegram and the apps, recorded instead of called."""

    def __init__(self, monkeypatch):
        self.sent, self.calls, self.notified, self.undone = [], [], [], []
        monkeypatch.setattr(telegram, "send", lambda chat, text, reply_to=None: self.sent.append((chat, text, reply_to)) or 900 + len(self.sent))
        monkeypatch.setattr(telegram, "download", lambda file_id: (b"bytes of " + file_id.encode(), f"voice/{file_id}.oga"))
        monkeypatch.setattr(telegram, "typing", lambda chat: handle._nothing())
        monkeypatch.setattr(handle.notify, "publish", lambda *a, **k: self.notified.append(a))
        for name in ("list_add", "list_tick", "expense", "reminder", "statement", "paper"):
            monkeypatch.setattr(house, name, self._app(name))
        monkeypatch.setattr(house, "list_show", lambda: house.Answer("Faltan 2 cosas.", "lista"))
        monkeypatch.setattr(house, "take_back", lambda undo: self.undone.append(undo) or True)

    def _app(self, name):
        def call(*args):
            self.calls.append((name, args[0]))
            if name == "reminder" and "manteca" in args[0]:
                return house.Answer("No entendí cuándo.", "recordatorios", ok=False)
            app = {"list_add": "lista", "list_tick": "lista", "expense": "finanzas", "reminder": "recordatorios",
                   "statement": "finanzas", "paper": "papeles"}[name]
            return house.Answer(f"ok {name}", app, undo=[["POST", f"http://x/{name}/1/borrar"]])
        return call


@pytest.fixture
def fake(monkeypatch):
    return Fake(monkeypatch)


def msg(text=None, user=ME, chat=None, first="Nahuel", username="nahuel", **extra):
    msg.n = getattr(msg, "n", 0) + 1
    chat = chat or user
    m = {"message_id": msg.n, "from": {"id": user, "is_bot": False, "first_name": first, "username": username},
         "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"}, "date": 0, **extra}
    if text is not None:
        m["text"] = text
    return m


def allow(conn, user=ME, name="Nahuel"):
    p = people.note(conn, user, name)
    people.allow(conn, p["id"])
    return p["id"]


# --- intents ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("comprar manteca", "list_add"), ("Comprá manteca y yerba", "list_add"), ("falta yerba", "list_add"),
    ("agregá leche, pan y 2 kg de papas a la lista", "list_add"), ("lista: pan", "list_add"),
    ("se acabó el café", "list_add"), ("anotá 1000 servilletas en la lista", "list_add"),
    ("gasté 8500 en la verdulería", "expense"), ("8500 verdulería", "expense"), ("super 12 mil", "expense"),
    ("compré pan por 3000", "expense"), ("15 lucas de ropa", "expense"), ("cobré 50000 de un trabajo", "expense"),
    ("anotá 8500 de nafta", "expense"), ("$3200 nafta", "expense"),
    ("ya compré la leche", "list_tick"), ("compré la leche y el pan", "list_tick"),
    ("recordame mañana a las 18 el dentista", "reminder"), ("recordatorio: pagar la luz el 10", "reminder"),
    ("avisame el viernes que vence el seguro", "reminder"),
    ("¿qué falta?", "list_show"), ("lista", "list_show"), ("que hay que comprar", "list_show"),
    ("terminé las compras", "list_finish"), ("Cerrar lista de compras", "list_finish"), ("cerrá la compra", "list_finish"),
    ("ya compré todo", "list_finish"), ("compras hechas", "list_finish"), ("listo las compras", "list_finish"),
    ("cerrar la lista del super", "list_finish"), ("buen día", "morning"), ("Buenos días!", "morning"),
    ("¿cómo está el server?", "server"), ("qué tengo mañana", "agenda"), ("agenda", "agenda"),
    ("saldos", "balances"), ("¿cuánto tengo?", "balances"),
    ("borrá eso", "undo"), ("deshacer", "undo"), ("ayuda", "help"), ("hola", "help"),
    ("cuánto gasté en delivery este mes?", "ai"), ("manteca", "ai"), ("¿falta manteca?", "ai"),
    ("500 g de queso", "ai"), ("", "none"),
    # Telegram's menu
    ("/start", "help"), ("/lista", "list_show"), ("/lista@casa_bot", "list_show"), ("/lista leche y pan", "list_add"),
    ("/gasto 8500 verdulería", "expense"), ("/agenda", "agenda"), ("/borrar", "undo"), ("/saldos", "balances"),
    ("/buendia", "morning"), ("/ia ¿cuánto gasté?", "ai"), ("/cualquiera", "help"),
    # Mesa
    ("¿cómo va la mesa?", "mesa"), ("la mesa", "mesa"), ("mesa", "mesa"), ("¿cómo van las inversiones?", "mesa"),
    ("trading", "mesa"), ("/mesa", "mesa"), ("cómo anda el trading", "mesa"),
    ("¿cómo va la cartera?", "mesa"), ("cómo van las inversiones", "mesa"),
    ("¿por qué compró MSFT?", "mesa_por_que"), ("¿por qué vendió?", "mesa_por_que"), ("por qué operó la mesa", "mesa_por_que"),
    ("pausá la cartera", "mesa_pausar"), ("pausá la mesa", "mesa_pausar"), ("pará la mesa", "mesa_pausar"), ("frená la mesa", "mesa_pausar"),
    ("reanudá la mesa", "mesa_reanudar"), ("seguí con la mesa", "mesa_reanudar"),
    ("la mesa del comedor es de madera", "ai"),
])
def test_intents(text, kind):
    assert read(text).kind == kind


def test_intents_details():
    assert read("che bot, comprar pilas").text == "comprar pilas"
    assert read("@casa_bot comprar pilas").text == "comprar pilas"
    assert read("/recordar el jueves a las 9 banco").text == "recordame el jueves a las 9 banco"
    assert read("qué tengo esta semana").args == {"dias": 7} and read("qué tengo hoy").args == {"dias": 1}
    assert read("¿por qué compró MSFT?").args == {"ticker": "MSFT"} and read("por qué vendió voo").args == {"ticker": "VOO"}
    assert read("¿por qué vendió?").args == {}


def test_intents_in_groups_stay_quiet_on_chat():
    assert read("llego tarde", in_group=True).kind == "none"
    assert read("hola", in_group=True).kind == "none"
    assert read("/cualquiera", in_group=True).kind == "none"
    assert read("comprar pilas", in_group=True).kind == "list_add"


# --- Telegram's formatting ------------------------------------------------------------------------

def test_to_html():
    assert telegram.to_html("*Hoy tenés $ 4.206.580* (US$ 2.708)") == "<b>Hoy tenés $ 4.206.580</b> (US$ 2.708)"
    assert telegram.to_html("🎙️ _gasté 12 mil_ <ok> & más") == "🎙️ <i>gasté 12 mil</i> &lt;ok&gt; &amp; más"
    assert telegram.to_html("Pago Dlo*pedidosya compra") == "Pago Dlo*pedidosya compra"
    assert telegram.to_html("mi_archivo_nuevo.pdf") == "mi_archivo_nuevo.pdf"


class FakeAPI:
    """Telegram's HTTP API, for the real telegram.py functions."""

    def __init__(self, monkeypatch, results):
        self.posts, self.results = [], results
        monkeypatch.setattr(telegram, "token", lambda: "123:abc")
        monkeypatch.setattr(telegram.httpx, "post", self.post)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], json, timeout))
        res = self.results.pop(0)

        class R:
            status_code = 200 if res.get("ok") else 400

            def json(self):
                return res
        return R()


def test_api_calls_carry_telegrams_own_params(monkeypatch):
    api = FakeAPI(monkeypatch, [{"ok": True, "result": [{"update_id": 7}]},
                                {"ok": False, "description": "Bad Request: can't parse entities"},
                                {"ok": True, "result": {"message_id": 55}}])
    assert telegram.updates(10) == [{"update_id": 7}]
    method, params, limit = api.posts[0]
    assert (method, params["timeout"], params["offset"], limit) == ("getUpdates", 50, 10, 65)
    assert telegram.send(1, "*hola* Pago*Dlo", reply_to=3) == 55  # the HTML refused: sent again as plain text
    assert api.posts[1][1]["parse_mode"] == "HTML" and "parse_mode" not in api.posts[2][1]
    assert api.posts[2][1]["text"] == "*hola* Pago*Dlo" and api.posts[2][1]["reply_parameters"]["message_id"] == 3


def test_api_errors_raise(monkeypatch):
    FakeAPI(monkeypatch, [{"ok": False, "description": "Unauthorized"}])
    with pytest.raises(telegram.TelegramError, match="Unauthorized"):
        telegram.me()


# --- people and invitations ------------------------------------------------------------------------

def test_invitation_authorizes_once(conn, fake):
    code = people.invite(conn)
    handle.message(conn, msg(f"/start {code}"), {})
    assert fake.sent[-1][1].startswith("Listo, ya te conozco.")
    p = conn.execute("SELECT * FROM people WHERE tg_id = ?", (ME,)).fetchone()
    assert (p["allowed"], p["avisos"]) == (1, 1)  # the first one gets the avisos
    handle.message(conn, msg(f"/start {code}", user=222, first="Otro"), {})  # used up
    assert conn.execute("SELECT allowed FROM people WHERE tg_id = 222").fetchone()[0] == 0
    assert len(fake.sent) == 1


def test_expired_or_wrong_invitations(conn, fake):
    people.invite(conn)
    handle.message(conn, msg("/start nope"), {})
    set_state(conn, "invite", "abc 2000-01-01T00:00:00")
    handle.message(conn, msg("/start abc"), {})
    assert fake.sent == [] and people.current_invite(conn) is None


def test_strangers_get_no_answer_and_the_iphone_hears_once(conn, fake):
    handle.message(conn, msg("hola", user=333, first="Juan", username="juancito"), {})
    handle.message(conn, msg("comprar pan", user=333, first="Juan"), {})
    assert fake.sent == [] and fake.calls == []
    assert len(fake.notified) == 1 and "Juan (@juancito)" in fake.notified[0][1]
    assert conn.execute("SELECT allowed FROM people WHERE tg_id = 333").fetchone()[0] == 0


# --- one message, start to end ----------------------------------------------------------------------

def test_list_add_answers_with_the_app_and_borrar_undoes_it(conn, fake):
    allow(conn)
    handle.message(conn, msg("comprar manteca"), {})
    assert fake.calls == [("list_add", "comprar manteca")]
    assert fake.sent[-1] == (ME, "🛒 ok list_add", None)
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", "http://x/list_add/1/borrar"]]]
    assert fake.sent[-1][1].startswith("↩️ Deshecho")
    handle.message(conn, msg("/borrar"), {})
    assert "nada reciente" in fake.sent[-1][1]


def test_the_same_message_is_answered_once(conn, fake):
    allow(conn)
    m = msg("gasté 8500 en la verdulería")
    handle.message(conn, m, {})
    handle.message(conn, m, {})
    assert len(fake.sent) == 1 and fake.calls == [("expense", "gasté 8500 en la verdulería")]


def test_reminder_to_buy_without_a_date_goes_to_the_list(conn, fake):
    allow(conn)
    handle.message(conn, msg("recordame comprar manteca"), {})
    assert fake.calls == [("reminder", "recordame comprar manteca"), ("list_add", "comprar manteca")]


def test_voice_notes_are_transcribed_and_shown(conn, fake, monkeypatch):
    allow(conn)
    got = {}

    def transcribe(data, suffix):
        got["data"], got["suffix"] = data, suffix
        return "gasté 12 mil en el súper"
    monkeypatch.setattr(handle.voz, "transcribe", transcribe)
    handle.message(conn, msg(voice={"file_id": "v1", "mime_type": "audio/ogg", "duration": 3}), {})
    assert got == {"data": b"bytes of v1", "suffix": ".oga"}
    assert fake.calls == [("expense", "gasté 12 mil en el súper")]
    assert fake.sent[-1][1] == "🎙️ _gasté 12 mil en el súper_\n\n💸 ok expense"
    row = conn.execute("SELECT kind, text, intent FROM messages WHERE incoming = 1").fetchone()
    assert tuple(row) == ("audio", "gasté 12 mil en el súper", "expense")


def test_photos_go_to_papeles_and_statements_to_finanzas(conn, fake):
    allow(conn)
    photo = [{"file_id": "small", "file_size": 10, "width": 90, "height": 90},
             {"file_id": "big", "file_size": 900, "width": 1280, "height": 960}]
    handle.message(conn, msg(photo=photo), {})
    handle.message(conn, msg(caption="resumen de la visa", document={"file_id": "d1", "mime_type": "application/pdf",
                                                                     "file_name": "visa.pdf"}), {})
    handle.message(conn, msg(caption="garantía heladera", document={"file_id": "d2", "mime_type": "application/pdf",
                                                                    "file_name": "g.pdf"}), {})
    handle.message(conn, msg(document={"file_id": "d3", "file_name": "mp.xlsx",
                                       "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}), {})
    assert [c[0] for c in fake.calls] == ["paper", "statement", "paper", "statement"]
    assert fake.calls[0][1] == "big.oga"  # the biggest size of the photo, named by Telegram's path


def test_a_ticket_with_an_expense_caption_does_both(conn, fake):
    allow(conn)
    handle.message(conn, msg(caption="gasté 8500 en la ferretería", photo=[{"file_id": "p", "file_size": 5}]), {})
    assert [c[0] for c in fake.calls] == ["paper", "expense"]
    assert fake.sent[-1][1] == "📄 ok paper\n\n💸 ok expense"


def test_other_media_gets_a_hint(conn, fake):
    allow(conn)
    handle.message(conn, msg(sticker={"file_id": "s"}), {})
    assert "entiendo texto" in fake.sent[-1][1]


def test_groups_only_answer_what_they_recognize(conn, fake):
    allow(conn)
    handle.message(conn, msg("llego tarde", chat=GROUP), {"username": "casa_bot"})
    handle.message(conn, msg(chat=GROUP, photo=[{"file_id": "p"}]), {"username": "casa_bot"})
    assert fake.sent == [] and fake.calls == []
    m = msg("@casa_bot comprar pilas", chat=GROUP)
    handle.message(conn, m, {"username": "casa_bot"})
    assert fake.calls == [("list_add", "comprar pilas")]
    assert fake.sent == [(GROUP, "🛒 ok list_add", m["message_id"])]


def test_bots_are_ignored(conn, fake):
    m = msg("comprar pan")
    m["from"]["is_bot"] = True
    handle.message(conn, m, {})
    assert fake.sent == [] and conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


def test_the_ai_gets_the_recent_conversation(conn, fake, monkeypatch):
    allow(conn)
    seen = {}

    def answer(text, history, now=None, who=""):
        seen["text"], seen["history"], seen["who"] = text, history, who
        return "Gastaste $ 40.000 en delivery.", ["gastos_por_categoria"]
    monkeypatch.setattr(handle.ia, "answer", answer)
    handle.message(conn, msg("comprar pan"), {})
    handle.message(conn, msg("¿cuánto gasté en delivery?"), {})
    assert seen["text"] == "¿cuánto gasté en delivery?"
    assert seen["history"] == [(True, "comprar pan"), (False, "🛒 ok list_add")]
    assert seen["who"] == "Nahuel"  # passed on to the apps (Finanzas: whose expense it is)
    assert fake.sent[-1][1] == "🤖 Gastaste $ 40.000 en delivery."


# --- Mesa ----------------------------------------------------------------------------------------

DECISIONES = [
    {"id": 3, "ts": 1793800000000, "sesion": "jueves", "resumen": "Rotación a salud",
     "ordenes": [{"ticker": "MSFT", "lado": "comprar", "monto_usd": 1200, "precio": 410.5, "estado": "llena",
                  "tesis": "Nube y IA con caja de sobra; " + "larga " * 40},
                 {"ticker": "KO", "lado": "vender", "monto_usd": 800, "estado": "llena", "tesis": None,
                  "motivo": "Se cumplió la tesis"},
                 {"ticker": "MSFT", "lado": "comprar", "estado": "rechazada", "tesis": "no cuenta"}]},
    {"id": 2, "ts": 1793700000000, "sesion": "lunes", "resumen": "Sin cambios", "ordenes": []},
]


def fake_mesa(monkeypatch, estado=200, body=None):
    seen = []

    def get(app, path, timeout=20, **params):
        seen.append((app, path, params))
        if path == "/api/frase":
            return httpx.Response(200, text="Claude va 3,2 % y la vara 2,1 %.")
        return httpx.Response(estado, json=body if body is not None else {"decisiones": DECISIONES})
    monkeypatch.setattr(house, "_get", get)
    return seen


def test_mesa_frase_and_why(monkeypatch):
    seen = fake_mesa(monkeypatch)
    assert house.mesa().text == "Claude va 3,2 % y la vara 2,1 %." and house.mesa().app == "mesa"
    a = house.mesa_por_que("")
    assert seen[-1] == ("mesa", "/api/decisiones", {"n": 20})
    assert "compró MSFT por USD 1.200 a USD 410,50" in a.text and "Nube y IA con caja de sobra" in a.text
    assert "vendió KO por USD 800" in a.text and "Se cumplió la tesis" in a.text
    assert "no cuenta" not in a.text and "…" in a.text  # the rejected one is left out, a long thesis is cut
    only_ko = house.mesa_por_que("ko").text
    assert seen[-1][2] == {"n": 20, "ticker": "KO"} and "KO" in only_ko and "MSFT" not in only_ko and "de KO" in only_ko
    assert "no compró ni vendió nada de SOL" in house.mesa_por_que("SOL").text


def test_mesa_down_or_odd_answer(monkeypatch):
    fake_mesa(monkeypatch, estado=503, body={"error": "x"})
    assert "Mesa no respondió" in house.mesa_por_que("").text
    fake_mesa(monkeypatch, body={"decisiones": "?"})
    assert "Mesa no respondió" in house.mesa_por_que("").text
    monkeypatch.setattr(house, "_get", lambda *a, **k: None)
    assert house.mesa().ok is False and "Mesa no respondió" in house.mesa().text
    assert "Mesa no respondió" in house.mesa_por_que("").text


def test_mesa_control_calls_the_web_with_who(monkeypatch):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        if url.endswith("/reanudar"):
            return httpx.Response(409, json={"error": "La mesa no está en pausa."})
        return httpx.Response(200, json={"ok": True, "mensaje": "Mesa en pausa."})
    monkeypatch.setattr(house.httpx, "post", post)
    a = house.mesa_control("pausar", "Nahuel B")
    assert a.text == "Mesa en pausa." and a.ok
    assert sent[0][0].endswith("/accion/mesa/pausar") and sent[0][2]["X-Quien"] == "Nahuel%20B"
    assert sent[0][2]["Accept"] == "application/json"
    b = house.mesa_control("reanudar", "Nahuel B")
    assert b.ok is False and "no está en pausa" in b.text


def test_mesa_is_for_the_owner_only(conn, fake, monkeypatch):
    allow(conn)  # the first one gets the avisos: the owner
    allow(conn, user=OTHER, name="Sofi")
    monkeypatch.setattr(house, "mesa", lambda: house.Answer("La mesa va bien.", "mesa"))
    monkeypatch.setattr(house, "mesa_control", lambda accion, who="": house.Answer(f"{accion} por {who}", "mesa"))
    handle.message(conn, msg("¿cómo va la mesa?"), {})
    assert fake.sent[-1][1] == "📈 La mesa va bien."
    handle.message(conn, msg("pausá la mesa"), {})
    assert fake.sent[-1][1] == "📈 pausar por Nahuel"
    handle.message(conn, msg("pausá la mesa", user=OTHER, first="Sofi"), {})
    assert "solo del dueño" in fake.sent[-1][1] and len(fake.sent) == 3


# --- the AI's tools -------------------------------------------------------------------------------

def openapi_of_lista():
    from fastapi import FastAPI
    from pydantic import BaseModel, Field

    sub = FastAPI(root_path_in_servers=False)

    class Things(BaseModel):
        texto: str = Field(..., description="Lo que dijo la persona.")

    @sub.get("/lista", operation_id="ver_lista_de_compras", summary="Lo que falta")
    def show(veces: int = 5):
        return {}

    @sub.post("/agregar", operation_id="agregar_a_la_lista", summary="Agrega cosas")
    def add(body: Things):
        return {}
    return sub.openapi()


def test_tools_from_openapi():
    tools = {t["function"]["name"]: (t, r) for t, r in ia.tools_from("lista", openapi_of_lista())}
    add, route = tools["agregar_a_la_lista"]
    assert add["function"]["parameters"]["properties"]["texto"]["type"] == "string"
    assert add["function"]["parameters"]["required"] == ["texto"]
    assert route == {"app": "lista", "method": "POST", "path": "/agregar", "body": True}
    show, route = tools["ver_lista_de_compras"]
    assert show["function"]["parameters"]["properties"]["veces"]["type"] == "integer"
    assert route["method"] == "GET" and not route["body"]


def test_answer_calls_the_tool_and_reads_its_result(monkeypatch):
    spec = openapi_of_lista()
    defs = [t for t, _ in ia.tools_from("lista", spec)]
    routes = {t["function"]["name"]: r for t, r in ia.tools_from("lista", spec)}
    monkeypatch.setattr(ia, "tools", lambda: (defs, routes))
    called = []
    monkeypatch.setattr(ia, "call", lambda route, args, who="": called.append((route["path"], args, who)) or '{"respuesta": "Anotado: manteca."}')
    turns = iter([
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "agregar_a_la_lista",
                                                                           "arguments": {"texto": "manteca"}}}]},
        {"role": "assistant", "content": "<think></think>**Listo**: anoté manteca."},
    ])
    reply, used = ia.answer("manteca", [], chat_fn=lambda messages, d: next(turns), who="Sofía")
    assert called == [("/agregar", {"texto": "manteca"}, "Sofía")]  # the apps hear who asked
    assert (reply, used) == ("*Listo*: anoté manteca.", ["agregar_a_la_lista"])


# --- the web side ----------------------------------------------------------------------------------

@pytest.fixture
def client(conn, monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(main, "ALLOWED", {"yo@github"})
    monkeypatch.setenv("MENSAJES_WORKERS", "0")
    monkeypatch.setattr(telegram, "TOKEN_FILE", str(tmp_path / "token"))
    return TestClient(main.app)


def test_pages_only_for_the_owner(client):
    proxied = {"X-Forwarded-For": "100.64.0.1"}
    assert client.get("/personas", headers={**proxied, "Tailscale-User-Login": "otro@github"}).status_code == 403
    assert client.get("/personas", headers=proxied).status_code == 403
    for path in ("/", "/personas", "/historial", "/ayuda", "/api/estado"):
        assert client.get(path, headers={**proxied, "Tailscale-User-Login": "yo@github"}).status_code == 200, path
    assert client.post("/invitar", headers={"Origin": "https://evil.example"}).status_code == 403


def test_token_is_checked_with_telegram_and_saved(client, monkeypatch, tmp_path):
    r = client.post("/token", data={"token": "hola"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?error=") and not (tmp_path / "token").exists()
    monkeypatch.setattr(telegram, "me", lambda tok=None: {"id": 1, "username": "casa_bot", "first_name": "Casa"})
    good = "123456789:" + "A" * 35
    r = client.post("/token", data={"token": good}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (tmp_path / "token").read_text().strip() == good
    client.post("/invitar")
    assert "https://t.me/casa_bot?start=" in client.get("/").text


def test_health_says_when_telegram_stopped_answering(client, conn, tmp_path):
    assert client.get("/health").json() == {"ok": True, "telegram": "sin token"}
    (tmp_path / "token").write_text("123:abc\n")
    assert client.get("/health").status_code == 503
    set_state(conn, "last_poll", "2999-01-01T00:00:00")
    assert client.get("/health").json() == {"ok": True, "telegram": "ok"}


def test_send_api_goes_only_to_authorized_people(client, conn, fake):
    assert client.post("/api/enviar", content="hola").status_code == 422
    allow(conn)
    allow(conn, 222, "Sofi")
    r = client.post("/api/enviar", content="Llegó el resumen de la Visa.")
    assert r.status_code == 200 and fake.sent == [(ME, "Llegó el resumen de la Visa.", None)]  # avisos: only the first
    assert client.post("/api/enviar", json={"texto": "Hola Sofi", "a": "sofi"}).status_code == 200
    assert fake.sent[-1] == (222, "Hola Sofi", None)
    assert client.post("/api/enviar", json={"texto": "x", "a": "nadie"}).status_code == 422


def test_try_box_runs_the_message(client, conn, fake):
    client.post("/probar", data={"texto": "comprar manteca"}, follow_redirects=False)
    rows = conn.execute("SELECT incoming, text, intent FROM messages WHERE chat = 'web' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "comprar manteca", "list_add"), (0, "🛒 ok list_add", None)]


# --- Review of 2026-11-06 ----------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    # bare numbers that are a year, a phone or a time: not cash
    ("pasaporte vence en 2027", "ai"), ("cumple de ana 1990", "ai"), ("turno médico 1530", "ai"),
    ("llamar a juan 3515551234", "ai"), ("8500 verdulería", "expense"), ("pagué 2025 de luz", "expense"),
    # "anotá" with a day or a time is a reminder; with an amount still money; alone, the list
    ("anotame que el jueves tengo dentista", "reminder"), ("anotá turno dentista el jueves a las 10", "reminder"),
    ("anotá 8500 de nafta", "expense"), ("anotá yerba", "list_add"),
    # "por qué …" about something else is not the mesa
    ("¿por qué cierra el súper a las 9?", "ai"), ("por qué vende tan caro el chino", "ai"),
    ("por qué cerró SOL la mesa", "mesa_por_que"), ("por qué vendió tan rápido el chino", "ai"),
    ("por qué compró msft", "mesa_por_que"), ("¿por qué se vendió VOO?", "mesa_por_que"),
    # undo, said the usual ways
    ("borrar eso", "undo"), ("borrar lo último", "undo"), ("borrar el último", "undo"), ("no, borralo", "undo"),
])
def test_intents_review(text, kind):
    assert read(text).kind == kind


def test_group_chat_phrases_dont_undo():
    for text in ("eso no", "me equivoqué", "no era eso", "cancelar"):
        assert read(text, in_group=True).kind != "undo", text
        assert read(text).kind == "undo", text  # in a private chat they still do
    assert read("borrar", in_group=True).kind == "undo"


def test_borrar_doesnt_skip_back_over_something_it_cant_undo(conn, fake, monkeypatch):
    allow(conn)
    handle.message(conn, msg("gasté 8500 en la verdulería"), {})
    monkeypatch.setattr(house, "list_tick", lambda text: house.Answer("Al carrito: leche.", "lista", wrote=True))
    handle.message(conn, msg("ya compré la leche"), {})
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == []  # the 8500 stays
    assert "no lo puedo deshacer" in fake.sent[-1][1]


def test_borrar_looks_past_answers_that_only_read(conn, fake):
    allow(conn)
    handle.message(conn, msg("gasté 8500 en la verdulería"), {})
    handle.message(conn, msg("¿qué falta?"), {})
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", "http://x/expense/1/borrar"]]]


def test_ai_writes_block_borrar(conn, fake, monkeypatch):
    allow(conn)
    handle.message(conn, msg("gasté 8500 en la verdulería"), {})
    monkeypatch.setattr(ia, "enabled", lambda: True)
    monkeypatch.setattr(ia, "_tools", (0.0, [], {"agregar_a_la_lista": {"method": "POST"}, "lista": {"method": "GET"}}))
    monkeypatch.setattr(ia, "answer", lambda *a, **k: ("Anoté manteca.", ["agregar_a_la_lista"]))
    handle.message(conn, msg("manteca"), {})
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == []
    assert ia.wrote(["lista"]) is False and ia.wrote(["agregar_a_la_lista"]) is True


def test_long_voice_notes_are_refused(conn, fake, monkeypatch):
    allow(conn)
    monkeypatch.setattr(handle.voz, "transcribe", lambda *a: pytest.fail("a 50-minute audio went to Whisper"))
    monkeypatch.setattr(handle, "enqueue_long_audio", lambda job: pytest.fail("a 50-minute audio was queued"))
    handle.message(conn, msg(voice={"file_id": "v1", "duration": 3000}), {})
    assert "muy largo" in fake.sent[-1][1]


def test_try_box_handles_money_ai_and_mesa(client, conn, fake, monkeypatch):
    monkeypatch.setattr(house, "mesa", lambda: house.Answer("La mesa va bien.", "mesa"))
    monkeypatch.setattr(ia, "enabled", lambda: False)
    for text in ("gasté 8500 en la verdulería", "¿cómo va la mesa?", "contame un chiste"):
        assert client.post("/probar", data={"texto": text}, follow_redirects=False).status_code == 303, text
    replies = [r[0] for r in conn.execute("SELECT text FROM messages WHERE chat = 'web' AND incoming = 0 ORDER BY id")]
    assert replies[0] == "💸 ok expense" and "La mesa va bien." in replies[1] and "No entendí" in replies[2]


def test_morning_leaves_the_mesa_out_for_others(conn, fake, monkeypatch):
    asked = []
    monkeypatch.setattr(house, "morning", lambda mesa=True: asked.append(mesa) or house.Answer("Hoy es…", "hoy"))
    allow(conn)
    allow(conn, OTHER, "Sofía")
    conn.execute("UPDATE people SET avisos = CASE WHEN tg_id = ? THEN 1 ELSE 0 END", (ME,))
    conn.commit()
    handle.message(conn, msg("buen día"), {})
    handle.message(conn, msg("buen día", user=OTHER, first="Sofía", username="sofi"), {})
    assert asked == [True, False]


def test_list_finish_survives_a_broken_lista(monkeypatch):
    monkeypatch.setattr(house, "_get", lambda *a, **k: httpx.Response(502, text="<html>Bad Gateway</html>"))
    assert house.list_finish().text == "Lista no respondió. Probá en un rato."
    monkeypatch.setattr(house, "_get", lambda *a, **k: httpx.Response(200, json={"en_el_carrito": ["Leche"], "cantidad": 0}))
    monkeypatch.setattr(house.httpx, "post", lambda *a, **k: httpx.Response(500, text="boom"))
    assert house.list_finish().ok is False


def test_new_bot_forgets_the_old_message_keys(client, conn, monkeypatch, tmp_path):
    set_state(conn, "bot", json.dumps({"id": 1, "username": "viejo_bot"}))
    conn.execute("INSERT INTO messages (tg_key, chat, incoming, kind, text) VALUES ('111:1', '111', 1, 'text', 'hola')")
    conn.commit()
    monkeypatch.setattr(telegram, "me", lambda tok=None: {"id": 2, "username": "nuevo_bot", "first_name": "Casa"})
    client.post("/token", data={"token": "123456789:" + "A" * 35}, follow_redirects=False)
    assert conn.execute("SELECT tg_key FROM messages").fetchone()[0] is None


def test_health_stays_green_while_a_slow_message_is_answered(client, conn, tmp_path):
    from datetime import datetime
    (tmp_path / "token").write_text("123:abc\n")
    set_state(conn, "last_poll", "2000-01-01T00:00:00")
    assert client.get("/health").status_code == 503
    set_state(conn, "busy_since", datetime.now().isoformat(timespec="seconds"))
    assert client.get("/health").status_code == 200


# --- Pagos del mes (Finanzas' bills) ------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("¿qué me falta pagar?", "bills"), ("que falta pagar", "bills"), ("que hay que pagar", "bills"),
    ("pagos", "bills"), ("pagos del mes", "bills"), ("servicios", "bills"), ("/pagos", "bills"),
    ("¿falta pagar algo?", "bills"), ("qué tengo que pagar este mes", "bills"), ("/pague", "bills"),
    ("pagué la luz", "bill_paid"), ("ya pagué el gas", "bill_paid"), ("pagamos el agua", "bill_paid"),
    ("Pagué la cuota de la casa", "bill_paid"), ("/pague luz", "bill_paid"), ("che, pagué la luz", "bill_paid"),
    # the neighbours keep what they had
    ("pagué 18000 de gas", "expense"), ("pagué $ 18.656 de luz", "expense"), ("pagamos 2500 dólares de la casa", "expense"),
    ("¿pagué la luz?", "ai"), ("pagué", "ai"), ("recordame pagar la luz el 10", "reminder"),
    ("¿qué falta?", "list_show"), ("que me falta", "list_show"), ("que hay que comprar", "list_show"),
    ("falta yerba", "list_add"), ("qué tengo esta semana", "agenda"), ("que tengo", "agenda"),
    ("saldos", "balances"), ("plata", "balances"), ("server", "server"), ("ya compré la leche", "list_tick"),
])
def test_intents_bills(text, kind):
    assert read(text).kind == kind


def test_bill_paid_in_a_group_and_its_fallback():
    assert read("pagué la luz", in_group=True).kind == "bill_paid"
    assert read("pagué la luz", in_group=True).args == {"si_no": "none"}
    assert read("pagué la luz").args == {"si_no": "ai"} and read("/pague luz").args == {}
    assert read("ya pagué el gas").text == "ya pagué el gas"


def fake_finanzas(monkeypatch, status=200, text="Listo: luz (EPEC) de noviembre, pagado hoy.", headers=None):
    sent = []

    def post(app, path, text_, timeout=90, who=""):
        sent.append((app, path, text_, who))
        return httpx.Response(status, text=text, headers=headers if headers is not None else {"X-Id": "3", "X-Mes": "2026-11"})
    monkeypatch.setattr(house, "_post_text", post)
    return sent


def test_bill_paid_marks_it_and_says_how_to_take_it_back(monkeypatch):
    sent = fake_finanzas(monkeypatch)
    a = house.bill_paid("pagué la luz", "Nahuel")
    assert sent == [("finanzas", "/api/pagos/pagar", "pagué la luz", "Nahuel")]
    assert a.ok and a.app == "finanzas" and a.text.startswith("Listo: luz")
    assert a.undo == [["POST", f"{house.URLS['finanzas']}/api/pagos/3/pendiente?m=2026-11"]]
    fake_finanzas(monkeypatch, text="Luz (EPEC) de noviembre ya estaba pagado (el 4/11).", headers={})
    a = house.bill_paid("pagué la luz")
    assert a.ok and a.undo == [] and not a.wrote and handle.undo_of(a) is None  # nothing changed, nothing to undo
    fake_finanzas(monkeypatch, status=422, text="No encontré ese pago. Tengo: luz, gas.", headers={})
    a = house.bill_paid("pagué el almuerzo")
    assert a.unknown and not a.ok and "No encontré" in a.text
    monkeypatch.setattr(house, "_post_text", lambda *a, **k: None)
    a = house.bill_paid("pagué la luz")
    assert "Finanzas no respondió" in a.text and not a.unknown


def test_bills_answer_finanzas_sentence(monkeypatch):
    seen = []
    monkeypatch.setattr(house, "_get", lambda app, path, **k: seen.append((app, path)) or
                        httpx.Response(200, text="Pagos de noviembre: 2 de 4 hechos. Faltan agua (el 10) y municipal (el 10)."))
    assert house.bills().text.startswith("Pagos de noviembre") and seen == [("finanzas", "/api/pagos")]
    monkeypatch.setattr(house, "_get", lambda *a, **k: None)
    assert house.bills().ok is False


def test_pague_la_luz_then_borrar(conn, fake, monkeypatch):
    allow(conn)
    sent = fake_finanzas(monkeypatch)
    handle.message(conn, msg("pagué la luz"), {})
    assert fake.sent[-1][1] == "💸 Listo: luz (EPEC) de noviembre, pagado hoy." and sent[0][3] == "Nahuel"
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", f"{house.URLS['finanzas']}/api/pagos/3/pendiente?m=2026-11"]]]


def test_already_paid_leaves_nothing_to_undo(conn, fake, monkeypatch):
    allow(conn)
    handle.message(conn, msg("gasté 8500 en la verdulería"), {})
    fake_finanzas(monkeypatch, text="Luz (EPEC) de noviembre ya estaba pagado (el 4/11).", headers={})
    handle.message(conn, msg("ya pagué la luz"), {})
    assert "ya estaba pagado" in fake.sent[-1][1]
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", "http://x/expense/1/borrar"]]]  # like any answer that only read


def test_a_bill_finanzas_doesnt_know_goes_where_it_went_before(conn, fake, monkeypatch):
    allow(conn)
    fake_finanzas(monkeypatch, status=422, text="No encontré ese pago. Tengo: luz, gas.", headers={})
    monkeypatch.setattr(ia, "enabled", lambda: True)
    asked = []
    monkeypatch.setattr(ia, "answer", lambda text, history, now=None, who="": asked.append(text) or ("Qué rico.", []))
    handle.message(conn, msg("pagué el almuerzo"), {})
    assert asked == ["pagué el almuerzo"] and fake.sent[-1][1] == "🤖 Qué rico."
    assert "No encontré" not in fake.sent[-1][1]
    n = len(fake.sent)
    handle.message(conn, msg("pagué el almuerzo", chat=GROUP), {"username": "casa_bot"})  # a group: quiet, as before
    assert len(fake.sent) == n
    handle.message(conn, msg("/pague almuerzo"), {})  # asked for by name: Finanzas' own answer
    assert "No encontré ese pago" in fake.sent[-1][1]


def test_an_amount_keeps_it_a_cash_expense(conn, fake, monkeypatch):
    allow(conn)
    monkeypatch.setattr(house, "_post_text", lambda *a, **k: pytest.fail("went to /api/pagos/pagar"))
    handle.message(conn, msg("pagué 18000 de gas"), {})
    assert fake.calls == [("expense", "pagué 18000 de gas")]


def test_help_mentions_the_bills():
    assert "¿qué me falta pagar?" in handle.HELP and "pagué la luz" in handle.HELP


# --- Vigía: links to keep, pages to watch, long voice notes ----------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("https://diario.example/nota", "save"), ("guardá https://diario.example/nota", "save"),
    ("resumime https://youtu.be/dQw4w9WgXcQ", "save"), ("leé esto https://diario.example/a", "save"),
    ("resumí www.diario.example/a", "save"), ("che, guardame https://x.example/a", "save"),
    ("seguí el precio de https://shop.example/heladera", "watch"), ("vigilá https://example.com/novedades", "watch"),
    ("avisame si cambia https://example.com/novedades", "watch"),
    ("avisame cuando aparezca \"turnos\" en https://example.com/turnos", "watch"),
    ("Avisame cuando desaparezca «No hay turnos» en https://example.com/turnos", "watch"),
    ("¿qué estoy vigilando?", "watch_list"), ("vigilancias", "watch_list"), ("/vigia", "watch_list"),
    ("/vigia avisame si cambia https://example.com", "watch"), ("/guardar https://diario.example/a", "save"),
    ("mirá esto https://diario.example/a", "ai"),  # a link with other words: not "just a link"
    ("avisame el viernes que vence el seguro", "reminder"), ("seguí con la mesa", "mesa_reanudar"),
])
def test_intents_vigia(text, kind):
    assert read(text).kind == kind


def test_intents_vigia_details():
    assert read("guardá https://Diario.example/Nota.").text == "https://Diario.example/Nota"  # case kept, dot out
    assert read("Che, seguí el precio de https://Shop.example/P?id=1").text == "seguí el precio de https://Shop.example/P?id=1"
    assert read("https://diario.example/nota", in_group=True).kind == "none"  # links shared in a group stay theirs
    assert read("guardá https://diario.example/nota", in_group=True).kind == "save"


class VigiaAPI:
    def __init__(self, monkeypatch, status=200, ids="7"):
        self.posts = []
        self.status, self.ids = status, ids
        monkeypatch.setattr(house.httpx, "post", self.post)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return httpx.Response(self.status, text="Listo, sigo el precio.", headers={"X-Ids": self.ids})


def test_house_vigia_calls(monkeypatch):
    api = VigiaAPI(monkeypatch)
    a = house.watch("seguí el precio de https://shop.example/p", "Sofi", 222)
    assert api.posts[-1] == (house.URLS["vigia"] + "/api/vigilar",
                             {"texto": "seguí el precio de https://shop.example/p", "quien": "Sofi", "chat": "222"})
    assert a.ok and a.app == "vigia" and a.undo == [["POST", house.URLS["vigia"] + "/v/7/borrar"]]
    a = house.save("guardá https://diario.example/nota.", "Nahuel", "web")
    assert api.posts[-1][1] == {"url": "https://diario.example/nota", "quien": "Nahuel", "chat": None}
    assert a.undo == [["POST", house.URLS["vigia"] + "/g/7/borrar"]]
    house.save("una idea para el finde", "Nahuel", 111)
    assert api.posts[-1][1]["texto"] == "una idea para el finde"
    api.ids = ""  # the same link again: nothing to take back
    assert house.save("https://diario.example/nota", "", 111).undo == []
    api.status = 422
    assert house.watch("vigilá https://spa.example/", "", 1).ok is False


def test_watch_from_telegram_answers_and_borrar_takes_it_back(conn, fake, monkeypatch):
    allow(conn)
    got = []
    monkeypatch.setattr(house, "watch", lambda text, who, chat: got.append((text, who, chat)) or house.Answer(
        "Listo, sigo el precio de *Heladera*: hoy está $ 1.000.000.", "vigia", undo=[["POST", "http://v/v/3/borrar"]]))
    handle.message(conn, msg("seguí el precio de https://shop.example/heladera"), {})
    assert got == [("seguí el precio de https://shop.example/heladera", "Nahuel", ME)]
    assert fake.sent[-1][1].startswith("🔭 Listo, sigo el precio")
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", "http://v/v/3/borrar"]]]


def test_long_voice_notes_go_to_the_background_and_then_to_vigia(conn, fake, monkeypatch):
    allow(conn)
    queued = []
    monkeypatch.setattr(handle, "enqueue_long_audio", queued.append)
    monkeypatch.setattr(handle.voz, "transcribe", lambda *a: pytest.fail("Whisper ran on the poller's thread"))
    handle.message(conn, msg(voice={"file_id": "v9", "duration": 1200}), {})
    assert fake.sent[-1][1] == "🎙️ Es largo: lo transcribo y te mando el resumen."
    job = queued[0]
    assert (job["file_id"], job["chat"], job["who"], job["duration"]) == ("v9", ME, "Nahuel", 1200)

    monkeypatch.setattr(handle.voz, "transcribe", lambda data, suffix: "Reunión del consorcio: se aprobó pintar el hall.")
    saved = []
    monkeypatch.setattr(house, "save_audio", lambda text, title, who, chat: saved.append((text, title, who, chat))
                        or house.Answer("Guardado: te mando el resumen cuando esté.", "vigia"))
    before = len(fake.sent)
    handle.long_audio(conn, job)
    text, title, who, chat = saved[0]
    assert text.startswith("Reunión del consorcio") and title.startswith("Audio de Nahuel del ") and title.endswith("(20 min)")
    assert (who, chat) == ("Nahuel", ME) and len(fake.sent) == before  # the summary comes from Vigía
    assert conn.execute("SELECT text FROM messages WHERE id = ?", (job["row_id"],)).fetchone()[0].startswith("Reunión")

    monkeypatch.setattr(house, "save_audio", lambda *a: house.Answer("Vigía no respondió. Probá en un rato.", "vigia", ok=False))
    handle.long_audio(conn, job)
    assert "no lo pude guardar" in fake.sent[-1][1] and "pintar el hall" in fake.sent[-1][1]
    monkeypatch.setattr(handle.voz, "transcribe", lambda *a: "")
    handle.long_audio(conn, job)
    assert "No pude entender el audio largo" in fake.sent[-1][1]


def test_the_long_audio_thread_takes_them_one_at_a_time(conn, monkeypatch):
    done = []
    monkeypatch.setattr(handle, "long_audio", lambda c, job: done.append(job["file_id"]))
    for i in range(3):
        handle.enqueue_long_audio({"file_id": f"a{i}"})
    handle.LONG_AUDIOS.join()
    assert done == ["a0", "a1", "a2"] and handle._long_thread.is_alive()


def test_send_api_answers_a_chat_only_where_someone_authorized_wrote(client, conn, fake):
    assert client.post("/api/enviar", json={"texto": "🔭 Bajó", "chat": "111"}).status_code == 422  # nobody wrote yet
    allow(conn)
    handle.message(conn, msg("hola", chat=GROUP), {})
    handle.message(conn, msg("hola", user=333, first="Extraño"), {})  # a stranger's chat: not an address
    r = client.post("/api/enviar", json={"texto": "🔭 Bajó", "chat": str(GROUP)})
    assert r.status_code == 200 and fake.sent[-1] == (GROUP, "🔭 Bajó", None)
    assert client.post("/api/enviar", json={"texto": "x", "chat": "333"}).status_code == 422
    assert client.post("/api/enviar", json={"texto": "x", "chat": "web"}).status_code == 422


def test_help_and_the_ai_know_vigia():
    assert "seguí el precio de" in handle.HELP and "vigia" in ia.TOOL_APPS
    assert house.ICONS["vigia"] and house.NAMES["vigia"] == "Vigía"


# --- Pedidos: the house's delivery orders ------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("armá el pedido", "order_make"), ("armá los pedidos", "order_make"), ("hacé el pedido", "order_make"),
    ("che, armame el pedido", "order_make"),
    ("pedí la verdura", "order_make"), ("pedí la fruta y la verdura", "order_make"), ("pedí a la verdulería", "order_make"),
    ("pedí la carne", "order_make"), ("pedí al súper", "order_make"), ("pedido de verdulería", "order_make"),
    ("pedí 2 kg de papas y bananas a la verdulería", "order_make"), ("pedile a la carnicería 1 kg de asado", "order_make"),
    ("pedidos", "orders_list"), ("¿qué pedidos hay?", "orders_list"), ("/pedidos", "orders_list"),
    ("¿cuánto está la banana?", "price"), ("precio de la palta", "price"), ("/precio banana", "price"),
    # what already had an intent keeps it
    ("comprar pedidos de carne", "list_add"), ("comprar carne", "list_add"), ("pagué 8500 al súper", "expense"),
    ("gasté 12 mil en la verdulería", "expense"), ("pedido 8500 verdulería", "expense"),
    ("pedidosya 8500", "expense"), ("pedí 8500 de verdulería", "expense"),
    ("recordame pedir la carne mañana", "reminder"), ("pagué la luz", "bill_paid"),
    ("¿cuánto está el dólar?", "ai"), ("cuánto está el bitcoin", "ai"), ("¿cuánto sale la luz?", "ai"),
    ("pedí turno con el dentista", "ai"), ("¿por qué cierra el súper a las 9?", "ai"),
])
def test_intents_pedidos(text, kind):
    assert read(text).kind == kind


def test_intents_pedidos_details():
    assert read("pedí la fruta y la verdura").args == {"proveedor": "verdura"}
    assert read("pedí a la verdulería").args == {"proveedor": "verdura"}
    assert read("pedí la carne").args == {"proveedor": "carne"}
    assert read("pedí al súper").args == {"proveedor": "super"}
    assert read("pedido de verdulería").args == {"proveedor": "verdura"}
    assert read("armá el pedido").args == {}
    it = read("Pedí 2 kg de papas y bananas a la verdulería")
    assert it.args == {"proveedor": "verdura", "texto": "2 kg de papas y bananas"}
    it = read("pedile a la carnicería 1 kg de asado")
    assert it.args == {"proveedor": "carne", "texto": "1 kg de asado"}
    assert read("¿cuánto está la banana?").text == "banana"
    assert read("precio de la palta").text == "palta"
    assert read("cuánto está el kilo de papa").text == "papa"
    assert read("pedí la verdura", in_group=True).kind == "order_make"


class PedidosAPI:
    def __init__(self, monkeypatch, status=200, ids="4,5"):
        self.posts, self.gets = [], []
        self.status, self.ids = status, ids
        monkeypatch.setattr(house.httpx, "post", self.post)
        monkeypatch.setattr(house.httpx, "get", self.get)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return httpx.Response(self.status, text="Verdufresca: 3 renglones.", headers={"X-Ids": self.ids})

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        return httpx.Response(self.status, text="Banana: $ 2.000 el kilo.")


def test_house_pedidos_calls(monkeypatch):
    api = PedidosAPI(monkeypatch)
    a = house.order_make(who="Sofi")
    assert api.posts[-1] == (house.URLS["pedidos"] + "/api/armar", {"quien": "Sofi"}, 60)
    assert a.ok and a.app == "pedidos"
    assert a.undo == [["POST", house.URLS["pedidos"] + "/p/4/cancelar"], ["POST", house.URLS["pedidos"] + "/p/5/cancelar"]]
    house.order_make("verdura", who="Nahuel")
    assert api.posts[-1][1] == {"proveedor": "verdura", "quien": "Nahuel"}
    house.order_make("carne", "1 kg de asado", "Nahuel")
    assert api.posts[-1][1] == {"proveedor": "carne", "texto": "1 kg de asado", "quien": "Nahuel"}
    assert house.price("banana").text.startswith("Banana") and api.gets[-1] == (house.URLS["pedidos"] + "/api/precio", {"q": "banana"})
    assert house.orders().ok and api.gets[-1][0] == house.URLS["pedidos"] + "/api/pedidos"
    api.status = 422
    assert house.order_make("carne", who="x").ok is False
    api.status = 502
    assert "no respondió" in house.order_make(who="x").text


def test_pedidos_from_telegram_answers_and_borrar_cancels(conn, fake, monkeypatch):
    allow(conn)
    got = []
    monkeypatch.setattr(house, "order_make", lambda proveedor="", texto="", who="": got.append((proveedor, texto, who)) or house.Answer(
        "Verdufresca: 3 renglones.", "pedidos", undo=[["POST", "http://p/p/4/cancelar"]]))
    handle.message(conn, msg("pedí la verdura"), {})
    assert got == [("verdura", "", "Nahuel")]
    assert fake.sent[-1][1].startswith("🛵 Verdufresca")
    handle.message(conn, msg("borrar"), {})
    assert fake.undone == [[["POST", "http://p/p/4/cancelar"]]]


def test_help_and_the_ai_know_pedidos():
    assert "pedí la verdura" in handle.HELP and "pedidos" in ia.TOOL_APPS
    assert house.ICONS["pedidos"] == "🛵" and house.NAMES["pedidos"] == "Pedidos"
    assert house.URLS["pedidos"].endswith(":3022")

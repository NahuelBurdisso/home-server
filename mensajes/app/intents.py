"""What a message asks for, by plain rules, like the apps' own parsers: the same sentence always
does the same thing, instantly. The apps read the details (Lista splits the things, Finanzas the
amount and the date, Recordatorios the when); this only picks which one.

Whatever the rules don't recognize goes to the local AI with the apps as tools (ia.py), except in
groups, where the bot stays quiet unless it recognizes the message. Telegram's /commands (the menu
under "/") are read here too: /lista, /lista leche y pan, /gasto 8500 verdulería…"""
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Intent:
    kind: str           # see KINDS
    text: str = ""      # what to send to the app (the message, cleaned)
    args: dict = field(default_factory=dict)


KINDS = {
    "help": "ayuda",
    "undo": "deshacer lo último",
    "list_add": "agregar a la lista",
    "list_show": "ver la lista",
    "list_tick": "marcar comprado",
    "list_finish": "terminar las compras",
    "expense": "anotar un gasto",
    "balances": "ver cuánto hay",
    "bills": "ver los pagos del mes",
    "bill_paid": "marcar un pago hecho",
    "reminder": "agendar",
    "agenda": "ver lo que viene",
    "morning": "el día",
    "server": "el server",
    "mesa": "cómo va la cartera contra el S&P",
    "mesa_por_que": "por qué compró o vendió la mesa",
    "mesa_pausar": "pausar la mesa",
    "mesa_reanudar": "reanudar la mesa",
    "save": "guardar un link",
    "watch": "vigilar una página",
    "watch_list": "ver lo vigilado",
    "order_make": "armar el pedido",
    "orders_list": "ver los pedidos",
    "price": "el precio de algo",
    "ai": "la IA",
    "none": "nada",
}


def fold(s: str) -> str:
    """Lowercase, without accents, one space between words."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


# Said before anything else and meaning nothing: "che", "hola bot,", "porfa".
POLITE = re.compile(r"^(?:(?:hola|buenas|che|ey|hey|bot|porfa|por favor|dale)\b[\s,.!:]*)+", re.I)
END = " .,!¡¿?:;"

HELP = re.compile(r"^(?:ayuda|help|\?+|menu|comandos|instrucciones|como (?:funciona|te uso|se usa)|"
                  r"que (?:podes|sabes|puedo) hacer(?: con vos)?|que haces)$")
GREETING = re.compile(r"^(?:hola|buenas|holis|hey|ey|que tal|como va|como andas)$")
UNDO = re.compile(r"^(?:no,? )?(?:borra(?:r|lo|la)?|deshace(?:r|lo)?)(?: eso| lo ultimo| el ultimo)?$|"
                  r"^(?:anula(?:r|lo)?(?: eso)?|cancela(?:r|lo)?(?: eso)?|me equivoque|eso no|no era eso|undo)$")
# In a group these are just conversation ("eso no", "me equivoqué"): there only borrar/deshacer undo.
UNDO_GROUP = re.compile(r"^(?:no,? )?(?:borra(?:r|lo|la)?|deshace(?:r|lo)?)(?: eso| lo ultimo| el ultimo)?$|^undo$")
MORNING = re.compile(r"^(?:buen dia|buenos dias|que hay hoy|como viene (?:el|hoy el) dia|el dia|hoy)$")
SERVER = re.compile(r"^(?:(?:como (?:esta|anda|va)|que onda|estado del?) (?:el |la )?(?:server|servidor|mac)|"
                    r"server|servidor)$")
# Mesa (the long-term paper portfolio): how it goes, why it bought or sold, pause and resume. "mesa" alone
# or with a "how is it going" in front; "cartera" and "inversiones" are the same thing said another way.
MESA = re.compile(r"^(?:(?:como (?:va|van|anda|andan|esta|estan|viene|vienen)|que tal|estado de|resumen de) )?"
                  r"(?:(?:la |las |mis )?(?:mesa(?: de (?:trading|inversiones|operaciones))?|cartera|inversiones)|(?:el )?trading)$")
MESA_WHY = re.compile(r"^por ?que (?:(?:la mesa|el bot|se) )?(?:compro|vendio|opero|cerro|compraron|vendieron|operaron|cerraron|"
                      r"compraste|vendiste|compra|vende|opera|cierra)\b")
MESA_PAUSE = re.compile(r"^(?:pausa(?:r|me)?|para|parar|frena(?:r)?|deten(?:e|er)?|apaga(?:r)?) (?:la )?(?:mesa|cartera|inversiones)$")
MESA_RESUME = re.compile(r"^(?:reanuda(?:r)?|reactiva(?:r)?|segui con|seguir con|sigue con|continua(?:r)? con|"
                         r"prende(?:r)?|vuelve a (?:operar|prender)) (?:la |las )?(?:mesa|cartera|inversiones)$")
# The ticker after the verb: "por qué compró msft" -> MSFT (1 to 5 letters, not a filler word).
MESA_TICKER = re.compile(r"\b(?:compro|vendio|opero|cerro|compraron|vendieron|operaron|cerraron|compraste|vendiste|"
                         r"compra|vende|opera|cierra)\s+(?:(?:las )?acciones de |el etf |el fondo |de )?(?P<tk>[a-z]{1,5})\b")
MESA_FILLER = {"a", "al", "algo", "ayer", "con", "de", "del", "el", "ella", "ese", "eso", "esa", "esto", "en", "hoy", "la", "las",
               "lo", "los", "mas", "mesa", "mi", "mis", "muy", "no", "nada", "para", "por", "que", "se", "si", "su", "sus",
               "tan", "tanto", "todo", "tu", "un", "una", "uno", "y", "claude", "bot", "casi", "poco", "mucho", "ahora"}
LIST_SHOW = re.compile(r"^(?:(?:la |ver (?:la )?|mostrame la |pasame la )?lista(?: de (?:las )?compras| del super)?|"
                       r"que (?:falta|nos falta|me falta|hay que comprar|compro|tengo que comprar|necesitamos)"
                       r"(?: comprar| en el super| del super)?)$")
# "Terminé las compras": what's in the cart becomes history (Lista's /terminar).
LIST_FINISH = re.compile(r"^(?:(?:ya )?(?:termine|terminamos|hice|hicimos) (?:las|la) compras?|"
                         r"(?:cerrar|cerra|cierro|cerramos|cerrame|finalizar|finaliza|terminar|termina) (?:la |las )?"
                         r"(?:lista|compras?)(?: de (?:las )?compras| del super)?|"
                         r"(?:listo|listas|hechas|fin de) (?:las |la )?compras?|compras? (?:hechas?|lista|terminadas?)|"
                         r"ya compre todo|ya compramos todo)(?: del super)?$")
# Things to buy: Lista's own LEAD (parse.py) reads these words off; here they only say it's for it.
LIST_ADD = re.compile(r"^(?:(?:agrega(?:me|r)?|suma(?:me|r)?|anota(?:me|r)?|pone(?:me|r)?|compra(?:r)?|"
                      r"hay que comprar|tengo que comprar|tenemos que comprar|nos faltan?|me faltan?|faltan?|"
                      r"se (?:acabo|acabaron|termino|terminaron)|no (?:hay|queda) mas)\b|lista ?:)")
LIST_TAIL = re.compile(r"\b(?:a|en|para) la lista(?: de (?:las )?compras| del super)?$")
LIST_TICK = re.compile(r"^(?:ya )?(?:compre|compramos|traje|trajimos|consegui|conseguimos)\b")
EXPENSE_LEAD = re.compile(r"^(?:gaste|gastamos|gasto|pague|pagamos|cobre|cobramos|me pagaron|nos pagaron|"
                          r"me depositaron|me entraron|ingreso|ingresaron|vendi|vendimos|compre|compramos|"
                          r"anota(?:me|r)?|carga(?:me|r)?)\b")  # "anotá 8500 de nafta": with an amount it's money
# A money amount: 8500, 8.500, $ 3.200, 12 mil, 15 lucas, 2,5 mil (not "2 kg", not "3 latas").
AMOUNT = re.compile(r"(?:\$\s?\d|\b\d[\d.,]*\s?(?:mil|k|lucas?|palos?|pesos)\b|"
                    r"\b\d{3,}(?:[.,]\d+)*\b(?!\s?(?:g|gr|grs|gramos|kg|kilos?|ml|cc|l|lts?|litros?|cm|mm|m|metros?|"
                    r"u|unidades|hs|horas|min|minutos)\b))")
# Money said as money: "$3200", "12 mil", "15 lucas", "8500 pesos".
MONEY_MARK = re.compile(r"\$\s?\d|\b\d[\d.,]*\s?(?:mil|k|lucas?|palos?|pesos)\b")
# Words that make a bare number a date, a time or a phone.
NOT_MONEY = re.compile(r"\b(?:vence|vencimiento|turno|cumple|cumpleanos|nacio|naci|nacimiento|llamar|llama|"
                       r"telefono|tel|celu|celular|whatsapp|wsp|a las|hs|dni|cuit|patente|piso|depto|codigo)\b")
# "anotame que el jueves tengo dentista", "anotá turno dentista a las 10": a reminder, not the list.
ANOTA_WHEN = re.compile(r"^anota(?:me|r)?\b.*\b(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo|"
                        r"pasado manana|manana a la|a las \d|turno|cumple)")
REMINDER = re.compile(r"^(?:(?:recorda(?:me|rme|r)?|recuerdame|acorda(?:me|rme|te)|avisa(?:me|rme)|"
                      r"agenda(?:me|r|lo)?|alarma)\b|recordatorio\b ?:?)")
AGENDA = re.compile(r"^(?:que (?:tengo|tenemos|hay|hago)(?: (?:hoy|manana|esta semana|la semana que viene|"
                    r"el finde|este finde|pasado manana))?|(?:mi |la )?agenda(?: de (?:hoy|manana|la semana))?|"
                    r"(?:los )?proximos(?: dias)?|que viene)$")
BALANCES = re.compile(r"^(?:cuanto (?:tengo|hay|plata tengo|plata hay|dinero tengo)|cuanta plata (?:tengo|hay)|"
                      r"saldos?|mis saldos|plata)$")
# The month's bills (Finanzas' "Pagos del mes"): what's left to pay. "que falta" alone is the list.
BILLS = re.compile(r"^(?:que (?:me |nos |te )?(?:falta|queda|hay que|tengo que|tenemos que) pagar(?: este mes)?|"
                   r"(?:que )?(?:me |nos )?falta pagar algo|(?:los |mis )?(?:pagos|servicios)(?: del mes| fijos)?)$")
# "pagué la luz", "ya pagué el gas", "pagamos la cuota de la casa": a bill, when there's no amount (with
# one it's a cash expense, above). Finanzas says whether it's one it knows.
BILL_PAID = re.compile(r"^(?:ya )?(?:pague|pagamos)\s+\S")


# Vigía: a link to keep (and summarize), or a page to watch. The link is read from the raw message
# (fold() would lowercase it); these only say which of the two it is.
URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»“”]+", re.I)
SAVE_LEAD = re.compile(r"^(?:guarda(?:me|lo|la|r)?|resumi(?:me|lo|la|r)?|resumen de|lee(?:me|lo|la|r)?|lei(?:me|lo|la)?|"
                       r"leete|lee esto)\b")
WATCH_LEAD = re.compile(r"^(?:vigila(?:me|lo|la|r)?|segui(?:me|le)?|seguir|avisa(?:me|rme)? (?:si|cuando)|"
                        r"fijate (?:si|cuando)|controla(?:me|lo|la|r)?|monitorea(?:me|lo|la|r)?)\b")
WATCH_LIST = re.compile(r"^(?:que (?:estoy|estas|estamos|esta) vigilando|que (?:vigilas|vigilo|estas siguiendo)|"
                        r"(?:mis |las |ver (?:las )?)?vigilancias|vigia)$")


# Pedidos (food delivered): "armá el pedido" builds one draft per shop from the list; "pedí la verdura"
# only that shop's; "pedí 2 kg de papas a la verdulería" those things. The text is read from the raw
# message (accents kept), the words are the shops' sections as Pedidos names them.
ORDER_ALL = re.compile(r"^(?:arma(?:me|r)?|hace(?:r|me)?|prepara(?:me|r)?) (?:el |los |un )?pedidos?"
                       r"(?: de (?:hoy|la casa)| con la lista)?$")
ORDERS_LIST = re.compile(r"^(?:(?:ver |mostrame |pasame )?(?:los |las |mis )?pedidos(?: (?:abiertos|pendientes|de la casa))?|"
                         r"que pedidos (?:hay|tenemos|tengo|estan pendientes)(?: abiertos)?)$")
def _shop_re(name: str) -> str:
    return rf"(?P<{name}>verduler[ií]a|fruter[ií]a|verduras?|frutas?|carnicer[ií]a|carnes?|s[uú]per(?:mercado)?)"



ORDER_LEAD = r"ped[ií](?:r|me|nos|le|les)?"
ORDER_SHOP = re.compile(rf"^(?:{ORDER_LEAD}\s+(?:(?:a|al|en|de|con)\s+)?(?:(?:la|el|las|los)\s+)?(?:fruta?s?\s+y\s+(?:la\s+)?)?{_shop_re('a')}|"
                        rf"pedido\s+(?:de|a|para)\s+(?:(?:la|el)\s+)?{_shop_re('b')})(?:\s+(?:hoy|ahora|nom[aá]s))?$", re.I)
ORDER_THINGS = re.compile(rf"^{ORDER_LEAD}\s+(?:(?:a|al|en)\s+(?:(?:la|el)\s+)?{_shop_re('c')}\s+(?P<t1>.+)|"
                          rf"(?P<t2>.+?)\s+(?:a|al|en|de|con)\s+(?:(?:la|el)\s+)?{_shop_re('d')})$", re.I)
PRICE = re.compile(r"^(?:cuanto (?:esta|estan|sale|salen|cuesta|cuestan|vale|valen)|"
                   r"(?:el |los )?precios? (?:de|del)|a cuanto (?:esta|estan|sale|salen))"
                   r"(?: (?:el|la|los|las|un|una))? ?(?P<q>[a-z][a-z ]*?)$")
PRICE_UNIT = re.compile(r"^(?:(?:el|un|medio) )?(?:kilo|kg|cajon|docena|media docena|atado|bolsa|pack) de ")
# What "cuánto está…" is not about: the dollar, a coin, the bills, the server (they have their own answer).
NOT_PRICE = re.compile(r"^(?:el |la |los |las )?(?:dolar|dolares|euro|euros|blue|oficial|mep|ccl|bitcoin|btc|eth|ethereum|sol|"
                       r"real|libra|luz|gas|agua|internet|alquiler|expensas|server|servidor|mesa|"
                       r"hora|tiempo|temperatura|todo)\b")
SHOP_KEY = {"verdu": "verdura", "fruta": "verdura", "fruter": "verdura", "carni": "carne", "carne": "carne", "super": "super"}


def _which(m: re.Match) -> str:
    """Whichever of the shop groups (a-d) matched."""
    return next(v for k, v in m.groupdict().items() if k in ("a", "b", "c", "d") and v)


def _shop(word: str) -> str:
    w = fold(word)
    return next(v for k, v in SHOP_KEY.items() if w.startswith(k))


def _order(raw: str) -> Intent | None:
    """'pedí la verdura', 'pedí 2 kg de papas a la verdulería', 'pedile a la carnicería 1 kg de asado'."""
    r = POLITE.sub("", raw).strip(END) or raw
    if m := ORDER_SHOP.match(r):
        return Intent("order_make", args={"proveedor": _shop(_which(m))})
    if m := ORDER_THINGS.match(r):
        things = (m["t1"] or m["t2"]).strip(END)
        if things and not AMOUNT.search(fold(things)):  # "pedí 8500 de verdulería" is money, not an order
            return Intent("order_make", args={"proveedor": _shop(_which(m)), "texto": things})
    return None


# /command (without args, with args): what it means. "/start" is what Telegram sends on the first open.
COMMANDS = {
    "start": ("help", None), "ayuda": ("help", None), "help": ("help", None),
    "lista": ("list_show", "list_add"), "compras": ("list_show", "list_add"), "comprar": ("list_show", "list_add"),
    "agenda": ("agenda", "reminder"), "recordar": (None, "reminder"), "recordame": (None, "reminder"),
    "gasto": (None, "expense"), "gaste": (None, "expense"), "saldos": ("balances", None),
    "pagos": ("bills", None), "pague": ("bills", "bill_paid"), "pagar": ("bills", "bill_paid"),
    "buendia": ("morning", None), "hoy": ("morning", None), "server": ("server", None), "mesa": ("mesa", None),
    "borrar": ("undo", None), "deshacer": ("undo", None), "ia": (None, "ai"), "pregunta": (None, "ai"),
    "vigia": ("watch_list", "watch"), "vigilar": ("watch_list", "watch"), "guardar": (None, "save"),
    "pedidos": ("orders_list", None), "pedido": ("orders_list", None), "precio": (None, "price"),
}
COMMAND = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s+(.*))?$", re.S)
MENTION = re.compile(r"^@\w+[\s,:]*")


def _command(raw: str, in_group: bool) -> Intent | None:
    m = COMMAND.match(raw.strip())
    if not m:
        return None
    alone, with_args = COMMANDS.get(fold(m[1]), (None, None))
    rest = (m[2] or "").strip()
    if rest and with_args:
        return Intent(with_args, f"recordame {rest}" if with_args == "reminder" else rest)
    if alone:
        return Intent(alone, args={"dias": 7} if alone == "agenda" else {})
    return Intent("none" if in_group else "help")


def _days(t: str) -> int:
    if "pasado manana" in t:
        return 3
    if "manana" in t:
        return 2
    if "hoy" in t:
        return 1
    if "semana que viene" in t:
        return 14
    return 7


def read(message: str, in_group: bool = False) -> Intent:
    raw = MENTION.sub("", " ".join((message or "").split()))  # "@casa_bot comprar pilas" in a group
    if (cmd := _command(raw, in_group)) is not None:
        return cmd
    t = fold(raw).strip(END)
    t_polite = POLITE.sub("", t).strip(END) or t
    question = raw.rstrip().endswith("?") or raw.lstrip().startswith("¿")

    if not t:
        return Intent("none")
    if HELP.match(t_polite) or (GREETING.match(t) and not in_group):
        return Intent("help")
    t = t_polite
    if (UNDO_GROUP if in_group else UNDO).match(t):
        return Intent("undo")
    if WATCH_LIST.match(t):
        return Intent("watch_list")
    if link := URL.search(raw):
        # Before the reminders: "avisame si cambia <link>" is a watch, not "avisame el jueves…".
        clean = POLITE.sub("", raw).strip() or raw
        if WATCH_LEAD.match(t):
            return Intent("watch", clean)
        if SAVE_LEAD.match(t):
            return Intent("save", link.group().rstrip(".,;:!?)"))
        if not (raw[:link.start()] + raw[link.end():]).strip(END):
            # Just a link: kept and summarized. In a group people share links with each other, so
            # there it takes "guardá <link>".
            return Intent("none" if in_group else "save", link.group().rstrip(".,;:!?)"))
    if ORDER_ALL.match(t):
        return Intent("order_make")
    if ORDERS_LIST.match(t):
        return Intent("orders_list")
    if not question and (order := _order(raw)) is not None:
        return order
    if MORNING.match(t):
        return Intent("morning")
    if SERVER.match(t):
        return Intent("server")
    if MESA.match(t):
        return Intent("mesa")
    if MESA_WHY.match(t):
        tk = MESA_TICKER.search(t)
        sym = tk["tk"].upper() if tk and tk["tk"] not in MESA_FILLER else ""
        # "¿por qué cierra el súper a las 9?" is not about the mesa: it has to say so, name a ticker, or
        # be just the question ("¿por qué vendió?").
        if sym or re.search(r"\b(?:mesa|el bot|claude)\b", t) or len(t.split()) <= 3:
            return Intent("mesa_por_que", raw, {"ticker": sym} if sym else {})
    if MESA_PAUSE.match(t):
        return Intent("mesa_pausar")
    if MESA_RESUME.match(t):
        return Intent("mesa_reanudar")
    if (m := PRICE.match(t)) and not NOT_PRICE.match(m["q"]):
        return Intent("price", PRICE_UNIT.sub("", m["q"]).strip())
    if LIST_SHOW.match(t):
        return Intent("list_show")
    if LIST_FINISH.match(t):
        return Intent("list_finish")
    if AGENDA.match(t):
        return Intent("agenda", args={"dias": _days(t)})
    if BALANCES.match(t):
        return Intent("balances")
    if BILLS.match(t):
        return Intent("bills")
    raw = POLITE.sub("", raw).strip() or raw  # "che bot, comprar pilas": the app gets "comprar pilas"
    if not question:
        if REMINDER.match(t):
            return Intent("reminder", raw)
        if ANOTA_WHEN.match(t) and not AMOUNT.search(t):
            return Intent("reminder", raw)  # "anotame que el jueves tengo dentista": not the list
        if LIST_TAIL.search(t):
            return Intent("list_add", raw)  # "… a la lista" says it, whatever it has
        has_amount = bool(AMOUNT.search(t))
        if EXPENSE_LEAD.match(t) and has_amount:
            return Intent("expense", raw)  # "compré pan por 3000" is money, "compré pan" ticks the list
        if LIST_TICK.match(t):
            return Intent("list_tick", raw)
        if LIST_ADD.match(t):
            return Intent("list_add", raw)
        if has_amount and _looks_like_expense(t):
            return Intent("expense", raw)
        if BILL_PAID.match(t) and not has_amount:
            # Last of all, so if Finanzas doesn't know that bill ("pagué el almuerzo") the message goes
            # where every rule above already let it go: the AI, or nothing in a group.
            return Intent("bill_paid", raw, {"si_no": "none" if in_group else "ai"})
    return Intent("none" if in_group else "ai", raw)


def _looks_like_expense(t: str) -> bool:
    """'8500 verdulería', 'super 12 mil', '$3200 nafta': an amount and a few words, nothing else.
    A bare number that reads as a year, a phone or a time ('pasaporte vence en 2027', 'llamar a juan
    3515551234', 'turno médico 1530') is not money: the AI gets it (it can still write it down)."""
    words = t.split()
    if len(words) > 6 or re.search(r"\b(?:cuanto|cuanta|cuando|como|que|donde|por que)\b", t):
        return False
    if MONEY_MARK.search(t):
        return True
    if NOT_MONEY.search(t):
        return False
    digits = [re.sub(r"\D", "", n) for n in re.findall(r"\b\d[\d.,]*\b", t)]
    return any(3 <= len(d) <= 8 and not re.fullmatch(r"(?:19|20)\d\d", d) for d in digits)

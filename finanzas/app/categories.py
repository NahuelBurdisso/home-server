"""Spending categories: groups (less detail) with subcategories (more detail), the starter
rules that fill them, and the merchant key that lets the app learn from your corrections.

A category id is 'group.sub', e.g. 'comida.delivery'."""
import re

from .importers.generic import norm

GROUPS = [
    ("vivienda", "🏠", "Vivienda", [("alquiler", "Alquiler"), ("servicios", "Luz, gas y agua"),
                                    ("internet", "Internet y celular"), ("expensas", "Expensas y arreglos")]),
    ("comida", "🍽️", "Comida", [("super", "Supermercado"), ("delivery", "Delivery"),
                                 ("salidas", "Restaurantes y bares"), ("kiosco", "Kiosco y almacén")]),
    ("transporte", "🚗", "Transporte", [("combustible", "Combustible"), ("peajes", "Peajes"),
                                        ("apps", "Uber, taxi y colectivo"), ("auto", "Auto (seguro, service)"),
                                        ("estacionamiento", "Estacionamiento")]),
    ("salud", "💊", "Salud", [("prepaga", "Prepaga"), ("farmacia", "Farmacia"),
                              ("consultas", "Médicos y estudios"), ("deporte", "Gimnasio y deporte")]),
    ("digital", "📱", "Suscripciones", [("streaming", "Streaming"), ("software", "Software e IA"),
                                        ("juegos", "Juegos"), ("otras", "Otras suscripciones")]),
    ("compras", "🛍️", "Compras", [("ropa", "Ropa y calzado"), ("hogar", "Hogar y deco"),
                                   ("tecnologia", "Tecnología"), ("regalos", "Regalos"), ("varias", "Otras compras")]),
    ("ocio", "🎉", "Ocio y viajes", [("eventos", "Entradas y eventos"), ("viajes", "Viajes"),
                                     ("salidas", "Salidas y clubes"), ("hobbies", "Hobbies")]),
    ("impuestos", "🧾", "Impuestos y banco", [("monotributo", "Monotributo y ARCA"),
                                              ("percepciones", "Percepciones e impuestos"),
                                              ("comisiones", "Comisiones bancarias"), ("municipal", "Tasas municipales")]),
    ("personas", "👥", "Transferencias a personas", [("personas", "Transferencias")]),
    ("efectivo", "💵", "Efectivo", [("retiros", "Retiros")]),
    ("otros", "❓", "Sin categoría", [("sin", "Sin categoría")]),
]
UNKNOWN = "otros.sin"

GROUP = {g: {"id": g, "emoji": e, "label": label} for g, e, label, _ in GROUPS}
CATEGORIES = {f"{g}.{s}": label for g, _, _, subs in GROUPS for s, label in subs}


def group_of(category: str | None) -> str:
    return (category or UNKNOWN).split(".")[0]


def label(category: str | None, with_group: bool = False) -> str:
    category = category or UNKNOWN
    g = GROUP[group_of(category)]
    sub = CATEGORIES.get(category, "Sin categoría")
    if with_group and sub != g["label"]:
        return f"{g['emoji']} {g['label']} · {sub}"
    return f"{g['emoji']} {sub}"


# Starter rules, checked in order against the normalized description (lowercase, no
# accents). More specific ones first: a cash withdrawal at Farmacity is cash, not pharmacy.
SEED_RULES = [(cat, re.compile(pattern)) for cat, pattern in [
    ("efectivo.retiros", r"retiro de dinero|extraccion|cajero"),
    ("impuestos.percepciones", r"percep|\bperc\b|impuesto|sellos|\biva\b|iibb|ganancias|db\.rg|\brg \d{4}"),
    ("impuestos.monotributo", r"\barca\b|\bafip\b|monotributo"),
    ("impuestos.comisiones", r"\bcom pq\b|comision|mantenimiento de cuenta|compensacion de fondos|ajuste intereses"),
    ("impuestos.municipal", r"municipalidad|rentas|tasa municipal"),
    ("vivienda.alquiler", r"alquiler"),
    ("vivienda.servicios", r"\bepec\b|edenor|edesur|ecogas|metrogas|naturgy|aguas cordobesas|aysa|camuzzi"),
    ("vivienda.internet", r"\bclaro\b|personal flow|\bpersonal\b|movistar|telecentro|fibertel|rednet|starlink|tuenti"),
    ("vivienda.expensas", r"expensas"),
    ("comida.delivery", r"pedidosya|pedidos ya|rappi"),
    ("comida.super", r"carrefour|\bdisco\b|jumbo|\bvea\b|\bcoto\b|\bdia\b|changomas|walmart|supermercado|"
                     r"hipermercado|\blibertad\b|cordiez|\bsuper\b"),
    ("comida.kiosco", r"kiosco|kioscon|almacen"),
    ("comida.salidas", r"mcdonald|burger king|starbucks|\bcafe\b|cerveceria|parrilla|restaurant|pizzeria|"
                       r"heladeria|grido|freddo"),
    ("transporte.combustible", r"\bypf\b|\bshell\b|axion|puma energy|estacion de servicio"),
    ("transporte.peajes", r"caminos de las s\b|caminos de las si|peaje|autopista|telepase|aubasa"),
    ("transporte.apps", r"\buber\b|cabify|\bdidi\b|\bsube\b|\btaxi\b|remis"),
    ("transporte.estacionamiento", r"estacionamiento|parking"),
    ("salud.prepaga", r"swiss medical|\bosde\b|galeno|sancor salud|omint|medife|apross"),
    ("salud.farmacia", r"farmacity|farmacia"),
    ("salud.deporte", r"megatlon|smart ?fit|gimnasio|\bgym\b|sport club"),
    ("digital.streaming", r"netflix|hbo ?max|prime ?video|disney|spotify|youtube|paramount|crunchyroll|\bflow\b"),
    ("digital.software", r"anthropic|openai|chatgpt|google one|icloud|apple\.com/bill|github|notion|dropbox|adobe|canva"),
    ("digital.juegos", r"xbox|playstation|steam|nintendo|epic games"),
    ("digital.otras", r"meli\+|suscripcion"),
    ("compras.ropa", r"macowens|\bzara\b|\bnike\b|adidas|dexter|grimoldi|kevingston|equus|levi"),
    ("compras.tecnologia", r"fravega|garbarino|musimundo|cetrogar"),
    ("compras.hogar", r"\beasy\b|sodimac|blaisten|ikea"),
    ("compras.varias", r"mercadolibre|mercado libre"),
    ("ocio.eventos", r"paseshow|alpogo|ticketek|passline|eventbrite|all ?access|tuentrada|cinemark|hoyts|showcase"),
    ("ocio.viajes", r"despegar|aerolineas|flybondi|jetsmart|booking|airbnb|hotel|\blatam\b|almundo"),
    ("personas.personas", r"transferencia enviada|transferencia inmediata|^transferencia\b"),
]]


def seed_category(description: str) -> str | None:
    text = norm(description)
    for category, pattern in SEED_RULES:
        if pattern.search(text):
            return category
    return None


def merchant_key(description: str) -> str:
    """The stable part of a description, without card codes or operation numbers:
    'ANTHROPIC* CLAUD in1UL4LeBjIQr' and 'ANTHROPIC* CLAUD in1UHja5BjIQr' -> 'anthropic claud'.
    A refund gets its payment's key, so both land in the same category and net out."""
    text = norm(description).replace("*", " ")
    text = re.sub(r"^(devolucion|reintegro|anulacion) (de )?", "", text)
    words = [w for w in text.split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words) or text

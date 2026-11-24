"""Reads what you dictate or type ("agregá leche, pan y 2 kg de papas") as separate things to buy,
each with the section of the store it's in. Plain rules, no AI: fast and always the same."""
import re
import unicodedata


def fold(s: str) -> str:
    """Lowercase, without accents: how texts are compared."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# id -> (label, emoji, phrases). The longest phrase found wins ("dulce de leche" before "leche").
SECTIONS = {
    "verduleria": ("Verdulería", "🥬", "papa, papas, tomate, tomates, cebolla, cebollas, lechuga, zanahoria, zanahorias, "
                   "banana, bananas, manzana, manzanas, naranja, naranjas, limon, limones, palta, paltas, zapallo, "
                   "zapallito, zapallitos, morron, morrones, ajo, frutilla, frutillas, fruta, frutas, verdura, verduras, "
                   "choclo, choclos, batata, batatas, acelga, espinaca, pera, peras, uva, uvas, mandarina, mandarinas, "
                   "pepino, berenjena, berenjenas, zucchini, rucula, perejil, albahaca, kiwi, durazno, duraznos, anana, "
                   "sandia, melon, champinones, hongos, puerro, apio, radicheta, brocoli, coliflor, repollo, remolacha"),
    "carniceria": ("Carnicería", "🥩", "carne, asado, pollo, milanesa, milanesas, carne picada, picada, bife, bifes, "
                   "chorizo, chorizos, morcilla, cerdo, vacio, matambre, nalga, peceto, hamburguesa, hamburguesas, "
                   "pescado, merluza, salmon, costillas, bondiola, suprema, supremas, pechuga, pechugas, osobuco, "
                   "cuadril, roast beef"),
    "lacteos": ("Lácteos y fiambres", "🧀", "leche, leche descremada, yogur, yogures, queso, quesos, queso rallado, "
                "manteca, crema, crema de leche, jamon, salame, huevos, huevo, ricota, muzzarella, mozzarella, fiambre, "
                "mortadela, queso crema"),
    "panaderia": ("Panadería", "🥖", "pan, pan lactal, pan rallado, facturas, medialunas, prepizza, prepizzas, "
                  "tapas de empanadas, tapas de tarta, bizcochitos, grisines"),
    "almacen": ("Almacén", "🥫", "arroz, fideos, harina, azucar, aceite, sal, yerba, cafe, te, galletitas, galletas, atun, "
                "pure de tomate, tomate triturado, mayonesa, mostaza, ketchup, lentejas, garbanzos, porotos, polenta, "
                "avena, cereal, cereales, mermelada, vinagre, especias, oregano, pimienta, caldo, caldos, dulce de leche, "
                "chocolate, cacao, edulcorante, sopa, arvejas, choclo en lata, durazno en almibar, snacks, papas fritas, "
                "mani, aceitunas, salsa, salsa de tomate"),
    "bebidas": ("Bebidas", "🥤", "agua, agua mineral, agua con gas, gaseosa, gaseosas, coca, coca cola, sprite, cerveza, "
                "cervezas, vino, vinos, jugo, jugos, soda, sifon, fernet, aperitivo"),
    "limpieza": ("Limpieza", "🧽", "lavandina, detergente, jabon en polvo, jabon liquido, jabon para la ropa, suavizante, "
                 "papel higienico, rollo de cocina, rollos de cocina, servilletas, esponja, esponjas, bolsas de residuo, "
                 "bolsas de basura, trapo, trapos, trapo de piso, desodorante de ambiente, limpiador, cif, lustramuebles, "
                 "escoba, secador, insecticida, raid, alcohol, alcohol en gel"),
    "higiene": ("Higiene y farmacia", "🧴", "shampoo, champu, acondicionador, jabon, jabones, pasta de dientes, "
                "cepillo de dientes, desodorante, toallitas, algodon, ibuprofeno, paracetamol, curitas, remedio, "
                "remedios, protector solar, repelente, afeitadora, maquinitas, toallas femeninas, panales, hilo dental, "
                "enjuague bucal, agua oxigenada, alcohol etilico"),
    "mascotas": ("Mascotas", "🐾", "alimento para perro, alimento para gato, comida de perro, comida de gato, "
                 "comida para el perro, comida para el gato, piedritas, alimento balanceado"),
    "otros": ("Otros", "🛒", ""),
}
ORDER = list(SECTIONS)
# Every phrase with its section, longest first (at the same position, "dulce de leche" over "leche").
PHRASES = sorted(((p.strip(), sid) for sid, (_, _, ps) in SECTIONS.items() for p in ps.split(",") if p.strip()),
                 key=lambda x: -len(x[0]))


def fold1(s: str) -> str:
    """Like fold, but always one character for one (½ -> 1, … -> .), so positions found in the
    folded text are the same in the text as written."""
    out = []
    for c in s.lower():
        base = "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x))
        out.append(base[:1] or c)
    return "".join(out)


def section_of(text: str) -> str:
    """Where in the store: the known phrase that comes first in it (the main noun: 'yogur de
    frutilla' is a yogurt, 'jugo de naranja' a drink), the longest one at that place."""
    words = re.sub(r"[^a-z0-9 ]", " ", fold(text)).split()
    # As said; else in singular ("2 leches" is milk, "3 panes" bread).
    for variant in (words, [w[:-1] if w.endswith("s") else w for w in words],
                    [w[:-2] if w.endswith("es") else w for w in words]):
        t = f" {' '.join(variant)} "
        best = None
        for phrase, sid in PHRASES:
            at = t.find(f" {phrase} ")
            if at >= 0 and (best is None or at < best[0]):
                best = (at, sid)
        if best:
            return best[1]
    return "otros"


_LIST = r"(?:a|en) la lista(?: de (?:compras|las compras|(?:el )?super(?:mercado)?))?"
# "agregá a la lista", "hay que comprar", "falta", "a la lista:"...: what's said before the things.
LEAD = re.compile(r"^(?:(?:por favor|che)[\s,]+)?(?:(?:agrega(?:me)?|agregar|suma(?:me)?|sumar|anota(?:me)?|anotar|"
                  r"pone(?:me)?|poner|compra|comprar|hay que comprar|tengo que comprar|necesito|nos faltan?|"
                  r"me faltan?|faltan?|tambien)\b\s*)?(?:" + _LIST + r"\s*)?[:\-]?\s*")
TAIL = re.compile(r"\s*(?:" + _LIST + r"|para (?:el super|comprar))\s*$")
_HALF = r"(?:y (?:medio|media|cuarto)\b\s*)?"  # "un kilo y medio de papas"
AMOUNT = re.compile(r"^(?:\d+/\d+|\d+(?:[.,]\d+)?|un par de|unas|unos|una|uno|un|dos|tres|cuatro|cinco|seis|media|medio)\b\s*"
                    + _HALF + r"(?:(?:kg|kilo|kilos|g|gr|gramos|l|lt|litro|litros|paquete|paquetes|caja|cajas|lata|latas|"
                    r"botella|botellas|docena|docenas|unidad|unidades|frasco|frascos|bolsa|bolsas|sachet|sachets|"
                    r"pack|packs|rollo|rollos|atado|atados|planta|plantas)\b\s*)?" + _HALF + r"(?:de\s+)?")
HALF = re.compile(r"^(?:medio|media|cuarto)\b")
# Between things: commas and semicolons (not a decimal comma: "1,5 kg"), "y", "e", "más"; any case.
SEPARATORS = re.compile(r"\s*(?:(?<!\d),|,(?!\d)|;|\s+y\s+|\s+e\s+|\s+m[aá]s\s+)\s*", re.I)


def key_of(text: str) -> str:
    """What it is, to tell the same thing from a new one: '2 kg de papas' -> 'papas'."""
    folded = " ".join(fold(text).split())
    return AMOUNT.sub("", folded).strip() or folded


def _cut(pattern: re.Pattern, text: str) -> str:
    """Removes what the pattern finds in the folded text from the text as written."""
    m = pattern.search(fold1(text))
    return (text[:m.start()] + text[m.end():]).strip() if m else text


def split(text: str) -> list[str]:
    """The separate things in one sentence, each starting with a capital."""
    text = " ".join(unicodedata.normalize("NFC", text).replace("\n", ", ").split())
    text = _cut(TAIL, _cut(LEAD, text))
    out = []
    for p in SEPARATORS.split(text):
        p = _cut(LEAD, p.strip(" .¡!¿?-:"))
        p = re.sub(r"^(?:[yeYE]|tambi[eé]n|m[aá]s)\s+", "", p, flags=re.I).strip()  # "comprar más leche"
        if out and HALF.match(fold(p)) and not AMOUNT.sub("", fold(out[-1])).strip():
            out[-1] += " y " + p  # "un kilo" + "medio de papas": one thing, split at its "y"
        elif p and fold(p) not in ("y", "e", "tambien", "nada", "eso", "algo"):
            out.append(p[:1].upper() + p[1:])
    return out

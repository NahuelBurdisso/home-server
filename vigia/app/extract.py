"""What a watch reads from a page, by rules: the visible text, a price, whether a phrase is there,
and what changed between two texts. Nothing here asks the model (decision 5)."""
import difflib
import json
import re
import unicodedata

from selectolax.lexbor import LexborHTMLParser

DROP = "script, style, noscript, template, svg, nav, header, footer, iframe"
BLOCKS = ("p, div, li, br, tr, td, th, h1, h2, h3, h4, h5, h6, section, article, aside, main, blockquote, pre, "
          "dt, dd, figcaption, table, ul, ol, form, label, option")
DIFF_MAX = 600


class NotFound(Exception):
    """The selector matched nothing, or there's no price: said to the person as is."""


def fold(s: str) -> str:
    """Lowercase, without accents, one space between words: "No hay TURNOS" ~ "no hay turnos"."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def _tree(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html or "")


def _lines(node) -> str:
    """A node's visible text, one line per block, whitespace collapsed, empty lines dropped."""
    for n in node.css(BLOCKS):
        n.insert_after("\n")
    raw = node.text(separator="", deep=True)
    return "\n".join(" ".join(line.split()) for line in raw.splitlines() if line.strip())


def page_text(html: str, selector: str = "") -> str:
    """The text of `selector` (every match, in order), or of the body without the page's chrome."""
    tree = _tree(html)
    if selector:
        try:
            nodes = tree.css(selector)
        except Exception as e:  # noqa: BLE001 - lexbor raises a bare error on a bad selector
            raise NotFound(f"El selector «{selector}» no es válido.") from e
        if not nodes:
            raise NotFound(f"No encontré «{selector}» en la página.")
        for n in nodes:
            for d in n.css("script, style, noscript, template"):
                d.decompose()
        return "\n".join(t for t in (_lines(n) for n in nodes) if t)
    for d in tree.css(DROP):
        d.decompose()
    body = tree.body or tree.root
    return _lines(body) if body is not None else ""


def title_of(html: str) -> str:
    tree = _tree(html)
    for sel, attr in (('meta[property="og:title"]', "content"), ("title", None), ("h1", None)):
        n = tree.css_first(sel)
        if n is not None:
            t = (n.attributes.get(attr) if attr else n.text()) or ""
            if t.strip():
                return " ".join(t.split())[:200]
    return ""


# --- prices -----------------------------------------------------------------------------------------

NUMBER = re.compile(r"\d[\d.,\s  ]*\d|\d")


def parse_price(s) -> float | None:
    """A price written the Argentine way or the plain way: "$ 1.234.567,89", "1234567.89",
    "1.234.567", "1,234,567.89", "19.990" (Argentine thousands), "1234,5". None when there's none."""
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s) if s > 0 else None
    m = NUMBER.search(str(s or ""))
    if not m:
        return None
    t = re.sub(r"[\s  ]", "", m.group())
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        t = t.replace(thou, "").replace(dec, ".")
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        groups = t.split(sep)
        if len(groups) > 2 or len(groups[-1]) == 3:  # 1.234.567 / 19.990 / 1,234: thousands
            if not all(len(g) == 3 for g in groups[1:]):
                return None
            t = "".join(groups)
        else:  # 1234,5 / 1234.89: decimals
            t = groups[0] + "." + groups[1]
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def _types(d: dict) -> set[str]:
    t = d.get("@type")
    return {x.split("/")[-1] for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def _offer_price(offers) -> float | None:
    for o in offers if isinstance(offers, list) else [offers]:
        if not isinstance(o, dict):
            continue
        for key in ("price", "lowPrice"):
            if (v := parse_price(o.get(key))) is not None:
                return v
        spec = o.get("priceSpecification")
        for p in spec if isinstance(spec, list) else [spec]:
            if isinstance(p, dict) and (v := parse_price(p.get("price"))) is not None:
                return v
        if "offers" in o and (v := _offer_price(o["offers"])) is not None:  # AggregateOffer → offers
            return v
    return None


def _walk_jsonld(data) -> float | None:
    """Product.offers.price / lowPrice, wherever the Product is: alone, in a list, in @graph, or
    nested (an ItemPage's mainEntity)."""
    if isinstance(data, list):
        for x in data:
            if (v := _walk_jsonld(x)) is not None:
                return v
        return None
    if not isinstance(data, dict):
        return None
    if _types(data) & {"Product", "ProductGroup", "IndividualProduct", "Car", "Offer", "AggregateOffer"}:
        if (v := _offer_price(data.get("offers"))) is not None:
            return v
        if _types(data) & {"Offer", "AggregateOffer"} and (v := _offer_price(data)) is not None:
            return v
    for key, value in data.items():
        if key != "offers" and isinstance(value, (dict, list)) and (v := _walk_jsonld(value)) is not None:
            return v
    return None


def price_of(html: str, selector: str = "") -> float:
    """The price, tried in order: the selector → JSON-LD → itemprop=price / product:price:amount /
    og:price:amount. Raises NotFound."""
    if selector:
        text = page_text(html, selector)
        if (v := parse_price(text)) is None:
            raise NotFound(f"En «{selector}» no hay un precio: dice «{text[:80]}».")
        return v
    tree = _tree(html)
    for s in tree.css('script[type="application/ld+json"]'):
        raw = (s.text(deep=True) or "").strip()
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)  # trailing commas
            except ValueError:
                continue
        if (v := _walk_jsonld(data)) is not None:
            return v
    for sel in ('meta[itemprop="price"]', '[itemprop="price"]', 'meta[property="product:price:amount"]',
                'meta[property="og:price:amount"]', 'meta[name="twitter:data1"]'):
        for n in tree.css(sel):
            raw = n.attributes.get("content") or ("" if n.tag == "meta" else n.text())
            if (v := parse_price(raw)) is not None:
                return v
    raise NotFound("No encontré un precio en la página (puede que lo arme con JavaScript, y eso no lo leo).")


def money(v: float | None) -> str:
    """$ 1.234.567 or $ 1.234,50, the Argentine way."""
    if v is None:
        return "—"
    whole = round(v, 2) == int(v)
    s = f"{v:,.0f}" if whole else f"{v:,.2f}"
    return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


# --- text ----------------------------------------------------------------------------------------

def has_needle(text: str, needle: str) -> bool:
    return bool(fold(needle)) and fold(needle) in fold(text)


def around(text: str, needle: str, width: int = 60) -> str:
    """The bit of the page where the needle is, for the card and the message."""
    flat = " ".join((text or "").split())
    i = fold(flat).find(fold(needle))  # fold keeps lengths for Spanish text, so positions match
    if i < 0:
        return ""
    a, b = max(0, i - width), min(len(flat), i + len(needle) + width)
    return ("…" if a else "") + flat[a:b] + ("…" if b < len(flat) else "")


def diff(old: str, new: str, limit: int = DIFF_MAX) -> str:
    """What was added and removed, line by line, at most `limit` characters."""
    out = []
    for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] == "+":
            out.append("➕ " + line[1:])
        elif line[:1] == "-":
            out.append("➖ " + line[1:])
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

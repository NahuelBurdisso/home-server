"""Your money as tools for the local AI: Open WebUI reads /ia/openapi.json and lets the model
(qwen3 on Ollama) call these to answer "¿cuánto gasté en delivery este mes contra el anterior?"
or to write down a cash expense. Nothing leaves the Mac.

Every answer is already added up and written the way the app shows it, so the model only reads
and compares; small models are bad at sums. Mounted at /ia in main.py, behind the same owner check."""
import re
from datetime import date

from fastapi import BackgroundTasks, FastAPI, Query, Request
from pydantic import BaseModel, Field

from . import categories, classifier, ledger, pagos, people, rapido, reports
from .db import ACCOUNTS, connect
from .importers.generic import norm
from .money import MONTHS, ars, month_name, usd

CATEGORY_LIST = "; ".join(
    f"{g} ({label}: {', '.join(f'{g}.{s} {sl}' for s, sl in subs)})" for g, _, label, subs in categories.GROUPS)
MONTH = "Mes YYYY-MM. Sin mes: el último con datos."

ia = FastAPI(
    title="Finanzas",
    version="1",
    description="Las finanzas personales del dueño (BBVA, Mercado Pago, efectivo), en pesos argentinos. "
                "Los montos ya vienen sumados y escritos: no los recalcules.",
    docs_url=None, redoc_url=None, root_path_in_servers=False,
)


def _month(mes: str | None, known: list[str]) -> str | None:
    """A month the model sent as '2026-10', '2026-10-01', 'octubre' or 'octubre 2026' -> 'YYYY-MM'
    (a bare month name: its latest one with data, else this year's). None if it isn't a month."""
    if not mes:
        return None
    m = re.match(r"\s*(\d{4})-(\d{1,2})", mes)
    if m and 1 <= int(m[2]) <= 12:
        return f"{m[1]}-{int(m[2]):02d}"
    words = norm(mes).split()
    for i, name in enumerate(MONTHS):
        if norm(name) in words:
            year = next((w for w in words if re.fullmatch(r"\d{4}", w)), None)
            if year:
                return f"{year}-{i + 1:02d}"
            same = [k for k in known if k.endswith(f"-{i + 1:02d}")]
            return same[-1] if same else f"{date.today().year}-{i + 1:02d}"
    return None


def _real(conn, month: str, s: dict) -> dict:
    """That month's inflation and what was left in today's pesos, when INDEC has the numbers."""
    factors, last = reports.inflation_factors(conn, [month])
    f = factors.get(month)
    pct = conn.execute("SELECT pct FROM inflation WHERE month = ?", (month,)).fetchone()
    out = {}
    if pct:
        out["inflacion_del_mes"] = f"{pct[0]:.1f}%".replace(".", ",")
    if f and last:
        out[f"quedo_en_pesos_de_{month_name(last).replace(' ', '_')}"] = ars(round(s["left"] * f))
    return out


def _months(conn, desde: str | None, hasta: str | None, default: int = 6) -> list[str]:
    known = reports.months(conn)
    if not known:
        return []
    desde, hasta = _month(desde, known), _month(hasta, known) or known[-1]
    if desde:
        return [m for m in known if desde <= m <= hasta]
    return [m for m in known if m <= hasta][-default:]


def _category(text: str) -> str | None:
    """A group or subcategory id from what the model sent: an id, or words of its name."""
    t = norm(text).replace(" ", "")
    if text in categories.GROUP or text in categories.CATEGORIES:
        return text
    for cid, label in categories.CATEGORIES.items():
        if t in (cid.split(".")[1], norm(label).replace(" ", "")):
            return cid
    for g, info in categories.GROUP.items():
        if t == norm(info["label"]).replace(" ", ""):
            return g
    for cid, label in categories.CATEGORIES.items():
        if len(t) >= 4 and t in norm(label).replace(" ", ""):
            return cid
    return None


def _spent(conn, month: str, cid: str) -> int:
    row = conn.execute(
        "SELECT -COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'expense' AND month = ?"
        " AND (COALESCE(category, ?) = ? OR COALESCE(category, ?) LIKE ?)",
        (month, categories.UNKNOWN, cid, categories.UNKNOWN, cid + ".%")).fetchone()
    return row[0]


def _pct(new: float, old: float) -> str | None:
    return f"{round((new - old) / old * 100):+d}%" if old else None


@ia.get("/resumen_del_mes", operation_id="resumen_del_mes",
        summary="Cuánto entró, cuánto se gastó y cuánto quedó en un mes, con los gastos por grupo")
def month_summary(mes: str | None = Query(None, description=MONTH)):
    conn = connect()
    ledger.apply_recurring(conn)
    known = reports.months(conn)
    if not known:
        return {"error": "Todavía no hay movimientos cargados."}
    month = _month(mes, known) or known[-1]
    if mes and not _month(mes, known):
        return {"error": f"No entendí el mes “{mes}”: mandalo como YYYY-MM.", "meses_con_datos": known}
    s = reports.summary(conn, month)
    mep = reports.mep_for(conn, month)
    groups = reports.spending_view(conn, month, 1, "group", None)["items"]
    return {
        "mes": month, "nombre": month_name(month),
        "ingresos": ars(s["income"]), "gastos": ars(s["spent"]), "quedo": ars(s["left"]),
        "quedo_en_dolares_mep": usd(s["left"] / 100 / mep) if mep else None,
        "dolar_mep": round(mep) if mep else None,
        **_real(conn, month, s),
        "presupuestos": [{"grupo": f"{b['emoji']} {b['label']}", "gastado": ars(b["spent"]), "tope": ars(b["limit"]),
                          "usado": f"{b['pct']}%"} for b in reports.budget_status(conn, month)],
        "compraste_dolares_por": ars(s["usd_bought"]) if s["usd_bought"] else None,
        "faltan_datos_de": s["missing"],
        "gastos_por_grupo": [{
            "grupo": f"{g['emoji']} {g['label']}", "id": g["id"], "total": ars(g["total"]), "porcentaje": g["share"],
            "vs_promedio_de_los_3_meses_anteriores": f"{g['delta']:+d}%" if g["delta"] is not None else None,
        } for g in groups],
        "gastos_mas_grandes": [{"fecha": t["date"], "descripcion": t["description"], "monto": ars(t["amount"]),
                                "cuenta": ACCOUNTS.get(t["account"], t["account"])} for t in s["biggest"][:5]],
        "para_revisar": len(ledger.pending_review(conn, month)),
        "meses_con_datos": known,
    }


@ia.get("/gastos_por_categoria", operation_id="gastos_por_categoria",
        summary="Lo gastado en una categoría mes por mes, con los comercios donde más se gastó")
def category_spending(
    categoria: str = Query(..., description=f"Id de grupo o subcategoría. Opciones: {CATEGORY_LIST}"),
    desde: str | None = Query(None, description="Primer mes YYYY-MM. Sin desde: los últimos 6 meses con datos."),
    hasta: str | None = Query(None, description="Último mes YYYY-MM. Sin hasta: el último con datos."),
):
    conn = connect()
    ledger.apply_recurring(conn)
    cid = _category(categoria)
    if cid is None:
        return {"error": f"No conozco la categoría '{categoria}'. Opciones: {CATEGORY_LIST}"}
    months = _months(conn, desde, hasta)
    per_month = [(m, _spent(conn, m, cid)) for m in months]
    total = sum(v for _, v in per_month)
    label = categories.label(cid, with_group=True) if "." in cid else \
        f"{categories.GROUP[cid]['emoji']} {categories.GROUP[cid]['label']}"
    return {
        "categoria": label,
        "por_mes": [{"mes": m, "nombre": month_name(m), "total": ars(v),
                     "vs_mes_anterior": _pct(v, per_month[i - 1][1]) if i else None}
                    for i, (m, v) in enumerate(per_month)],
        "total_del_periodo": ars(total),
        "promedio_por_mes": ars(total // len(months)) if months else ars(0),
        "donde_mas_se_gasto": [{"comercio": it["label"], "total": ars(it["total"]), "veces": it["count"]}
                               for it in reports.spending(conn, months, "merchant", cid)[:5]] if months else [],
    }


@ia.get("/buscar_movimientos", operation_id="buscar_movimientos",
        summary="Movimientos cuya descripción contiene un texto (un comercio, una persona, un servicio)")
def search(
    texto: str = Query(..., description="Parte de la descripción, por ejemplo: netflix, pedidosya, alquiler."),
    desde: str | None = Query(None, description="Primer mes YYYY-MM. Sin desde: los últimos 12 meses con datos."),
    hasta: str | None = Query(None, description="Último mes YYYY-MM."),
):
    conn = connect()
    months = _months(conn, desde, hasta, default=12)
    if not months:
        return {"cantidad": 0, "movimientos": []}
    marks = ",".join("?" * len(months))
    want = norm(texto)
    rows = [r for r in conn.execute(f"SELECT * FROM transactions WHERE month IN ({marks}) ORDER BY date DESC", months)
            if want in norm(r["description"])]
    out = sum(-r["amount"] for r in rows if r["amount"] < 0)
    got = sum(r["amount"] for r in rows if r["amount"] > 0)
    return {
        "buscado": texto, "meses": f"{months[0]} a {months[-1]}", "cantidad": len(rows),
        "total_salio": ars(out), "total_entro": ars(got) if got else None,
        "movimientos": [{"fecha": r["date"], "descripcion": r["description"], "monto": ars(r["amount"]),
                         "cuenta": ACCOUNTS.get(r["account"], r["account"]),
                         "categoria": categories.label(r["category"]) if r["kind"] == "expense" else None}
                        for r in rows[:25]],
    }


@ia.get("/pagos_del_mes", operation_id="pagos_del_mes",
        summary="Los pagos fijos de la casa en un mes (luz, gas, agua, impuestos, la casa): cuáles faltan, "
                "cuándo vencen, los datos para pagarlos y dónde se pagan")
def bills_of_month(mes: str | None = Query(None, description="Mes YYYY-MM. Sin mes: este mes.")):
    conn = connect()
    today = date.today()
    month = _month(mes, reports.months(conn)) or today.isoformat()[:7]
    items = pagos.month_status(conn, month, today)
    if not items:
        return {"mes": month, "nombre": month_name(month), "pagos": [], "respuesta": "No hay pagos fijos cargados para ese mes."}
    out = []
    for e in items:
        b = e["bill"]
        late = e["late"] or (not e["paid"] and e["due"] < today)
        prog = pagos.progress(conn, b)
        out.append({
            "pago": f"{b['emoji']} {b['name']}",
            "estado": "pagado" if e["paid"] else "atrasado" if late else "pendiente",
            "pagado_el": e["paid_on"].isoformat() if e["paid_on"] else None,
            "monto": pagos.money(b, e["amount"]),
            "monto_fijo": usd(b["amount_usd"] / 100) if b["amount_usd"] is not None else None,
            "vence": e["due"].isoformat(),
            "datos": pagos.detail_lines(b),
            "donde_se_paga": b["url"] or None,
            "notas": b["notes"] or None,
            "ultimo_monto": f"{pagos.money(b, e['last']['amount'])} ({month_name(e['last']['month'])})" if e["last"] else None,
            "lo_que_falta_pagar_de_la_deuda": prog["text"] if prog else None,
        })
    left = [o["pago"] for o in out if o["estado"] != "pagado"]
    return {"mes": month, "nombre": month_name(month), "pagados": len(out) - len(left), "total": len(out),
            "faltan": left, "pagos": out}


class Expense(BaseModel):
    texto: str = Field(..., description='Lo que dijo la persona, tal cual: "gasté 8500 en la verdulería ayer".')


@ia.post("/anotar_gasto", operation_id="anotar_gasto",
         summary="Anota un gasto o ingreso en efectivo. Solo efectivo: tarjeta y Mercado Pago llegan con su resumen")
def write_expense(body: Expense, background: BackgroundTasks, request: Request):
    conn = connect()
    try:
        text, unknown = rapido.write_down(conn, body.texto, date.today(), who=people.of_request(request))
    except ValueError as e:
        return {"anotado": False, "respuesta": str(e)}
    if unknown:
        background.add_task(classifier.suggest_pending)
    return {"anotado": True, "respuesta": text}

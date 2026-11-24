"""The two curves (Claude and the vara) as inline SVG in base 100, drawn by the server: no
JavaScript, nothing from outside. The SVG stretches to the card's width (preserveAspectRatio="none";
the lines keep their width with non-scaling-stroke) and the labels are HTML around it, so the phone
never squashes the text. Adapted from the previous Mesa's graficos.py.
"""
import math
from datetime import datetime

from markupsafe import Markup, escape

W, H = 1000, 100
PUNTOS = 600
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MARCAS = [900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400,
          30 * 86400, 91 * 86400, 182 * 86400, 365 * 86400]


def num(x: float, dec: int = 2) -> str:
    return f"{x:,.{dec}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def reducir(puntos: list, n: int = PUNTOS) -> list:
    """At most about n points, keeping the first and the last."""
    if len(puntos) <= n:
        return puntos
    paso = -(-len(puntos) // n)
    salida = puntos[::paso]
    if salida[-1] is not puntos[-1]:
        salida.append(puntos[-1])
    return salida


def marcas(t0: float, t1: float, cuantas: int = 5) -> list[tuple[float, str]]:
    """Labels for the time axis at round local times: (left %, text). t0, t1 in seconds."""
    span = t1 - t0
    cada = next((m for m in MARCAS if span / m <= cuantas), MARCAS[-1])
    salida = []
    if cada >= 30 * 86400:  # months: the first of each month
        d = datetime.fromtimestamp(t0)
        mes = d.month + (0 if d.day == 1 and d.hour == 0 else 1)
        anio = d.year + (mes - 1) // 12
        mes = (mes - 1) % 12 + 1
        salto = max(1, round(cada / (30 * 86400)))
        while True:
            t = datetime(anio, mes, 1).timestamp()
            if t >= t1:
                break
            texto = f"{MESES[mes - 1]} {str(anio)[2:]}" if mes == 1 or salto >= 3 else MESES[mes - 1]
            salida.append(((t - t0) / span * 100, texto))
            mes += salto
            anio, mes = anio + (mes - 1) // 12, (mes - 1) % 12 + 1
        return salida
    tz = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    t = math.ceil((t0 + tz) / cada) * cada - tz
    while t < t1:
        d = datetime.fromtimestamp(t)
        if cada < 86400:
            texto = d.strftime("%H:%M") if d.hour or d.minute else f"{DIAS[d.weekday()]} {d.day}"
        elif cada < 7 * 86400:
            texto = f"{DIAS[d.weekday()]} {d.day}"
        else:
            texto = f"{d.day} {MESES[d.month - 1]}"
        salida.append(((t - t0) / span * 100, texto))
        t += cada
    return salida



def dos_curvas(claude: list, vara: list, alto: int = 170,
               vacio: str = "Todavía no hay dos cierres para dibujar.") -> Markup:
    """claude, vara: [(ts UTC ms, valor)], oldest first. Each in base 100 from its first point."""
    def base100(pts):
        pts = [(int(t), float(v)) for t, v in pts if v is not None]
        if not pts or pts[0][1] <= 0:
            return []
        b = pts[0][1]
        return reducir([(t, v / b * 100) for t, v in pts])
    a, b = base100(claude), base100(vara)
    if len(a) < 2 and len(b) < 2:
        return Markup(f'<p class="note">{escape(vacio)}</p>')
    todos = a + b
    t0, t1 = min(t for t, _ in todos), max(t for t, _ in todos)
    if t1 <= t0:
        t1 = t0 + 1
    lo, hi = min(min(v for _, v in todos), 100), max(max(v for _, v in todos), 100)
    margen = (hi - lo) * 0.08 or 0.5
    abajo, arriba = lo - margen, hi + margen

    def x(t):
        return (t - t0) / (t1 - t0) * W

    def y(v):
        return H - (v - abajo) / (arriba - abajo) * H

    def camino(pts):
        return "M" + " L".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)

    svg = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">',
           f'<line class="base" x1="0" x2="{W}" y1="{y(100):.1f}" y2="{y(100):.1f}"/>']
    if len(b) >= 2:
        svg.append(f'<path class="vara" d="{camino(b)}"/>')
    if len(a) >= 2:
        svg.append(f'<path class="s0" d="{camino(a)}"/>')
    svg.append("</svg>")
    eje = "".join(f'<span style="left:{izq:.2f}%">{escape(texto)}</span>'
                  for izq, texto in marcas(t0 / 1000, t1 / 1000) if 3 < izq < 97)
    resumen = (f"Claude de 100 a {num(a[-1][1], 1)}" if a else "") + (f"; la vara de 100 a {num(b[-1][1], 1)}" if b else "")
    return Markup(f'<figure class="curva comparacion" style="--h:{alto}px" role="img" aria-label="{escape(resumen)}">'
                  f'<span class="ymax">{num(hi, 1)}</span><span class="ymin">{num(lo, 1)}</span>' + "".join(svg)
                  + f'<div class="xaxis">{eje}</div></figure>'
                  '<p class="leyenda"><span class="l-s0">Claude</span><span class="l-vara">la vara: SPY comprado y tenido</span></p>')

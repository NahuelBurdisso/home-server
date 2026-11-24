"""Graphs as inline SVG, drawn here: no JavaScript library, nothing from the internet.

The SVG stretches to any width (preserveAspectRatio="none", lines keep 1.5 px with
non-scaling-stroke) and the labels are HTML around it, so text never gets squashed on the
phone. static/pulso.js adds the crosshair with the values under the finger or the mouse."""
import json
import math
from datetime import datetime

from markupsafe import Markup, escape

W, H = 1000, 100
DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    if unit == "%":
        return f"{v:.0f}%" if v >= 10 or v == 0 else f"{v:.1f}%"
    if unit in ("B", "B/s"):
        suffix = "/s" if unit == "B/s" else ""
        for u in ("B", "KB", "MB", "GB", "TB"):
            if abs(v) < 1000 or u == "TB":
                return (f"{v:.0f} {u}" if abs(v) >= 100 or u == "B" else f"{v:.1f} {u}") + suffix
            v /= 1000
    if unit == "ms":
        return f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"
    if unit == "x":
        return f"{v:.2f}"
    if unit == " Mbit/s":
        return f"{v:.0f} Mbit/s"
    if unit == " dBm":
        return f"{v:.0f} dBm"
    if unit == "°":
        return f"{v:.0f}°"
    if unit == " TB":
        return f"{v:.2f} TB"
    return f"{v:.0f}{unit}"


def nice_max(v: float) -> float:
    """1, 2, 2.5 or 5 times a power of ten, at or above v."""
    if v <= 0:
        return 1
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p:
            return m * p
    return 10 * p


TICKS = [300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400]


def ticks(start: int, end: int, most: int = 6) -> list[tuple[float, str]]:
    """Labels for the time axis at round local times: (left %, text)."""
    span = end - start
    every = next((t for t in TICKS if span / t <= most), TICKS[-1])
    tz = datetime.fromtimestamp(start).astimezone().utcoffset().total_seconds()
    first = math.ceil((start + tz) / every) * every - tz
    out = []
    t = first
    while t < end:
        d = datetime.fromtimestamp(t)
        if every < 86400:
            label = d.strftime("%H:%M") if d.hour or d.minute else f"{DAYS[d.weekday()]} {d.day}"
        elif every < 7 * 86400:
            label = f"{DAYS[d.weekday()]} {d.day}"
        else:
            label = f"{d.day}/{d.month}"
        out.append(((t - start) / span * 100, label))
        t += every
    return out


def _path(values: list[float | None], n: int, ymax: float, close: bool, ymin: float = 0) -> str:
    """One SVG path; a gap in the data breaks the line instead of joining across it."""
    parts, run = [], []

    def flush():
        if not run:
            return
        if len(run) == 1:  # a lone point still shows as a short dash
            x, y = run[0]
            run.insert(0, (x - W / n / 2, y))
            run[-1] = (x + W / n / 2, y)
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in run)
        if close:
            d += f" L{run[-1][0]:.1f},{H} L{run[0][0]:.1f},{H} Z"
        parts.append(d)
        run.clear()

    for i, v in enumerate(values):
        if v is None:
            flush()
            continue
        x = (i + 0.5) / n * W
        y = H - min(max((v - ymin) / (ymax - ymin), 0), 1) * (H - 4)
        run.append((x, y))
    flush()
    return " ".join(parts)


def chart(series: list[tuple[str, str, list]], start: int, end: int, *, unit: str, ymax: float | None = None,
          height: int = 120, axes: bool = True, loss: list | None = None, field: str = "avg") -> Markup:
    """series: (name, css color, buckets from store.buckets). The first series is filled.
    loss: buckets whose failures (a ping with no answer) are drawn as red bars from the top."""
    n = max((len(b) for _, _, b in series), default=0) or 1
    values = [[(b[field] if b else None) for b in buckets] for _, _, buckets in series]
    peak = max((v for vs in values for v in vs if v is not None), default=0)
    low = min((v for vs in values for v in vs if v is not None), default=0)
    # Values below zero (Wi-Fi dBm): the scale runs from a round number under the lowest to 0.
    bottom = -nice_max(-low * 1.1) if low < 0 else 0
    top = ymax or (nice_max(peak * 1.1) if peak > 0 else 0)
    if top <= bottom:  # all zeros (an idle container): a 0..1 scale, the line along the bottom
        top = bottom + 1
    svg = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">',
           f'<line class="grid" x1="0" x2="{W}" y1="{H / 2}" y2="{H / 2}"/>']
    if loss:
        for i, b in enumerate(loss):
            if b and b["fails"]:
                h = max(b["fails"] / b["n"], 0.08) * H
                svg.append(f'<rect class="loss" x="{i / n * W:.1f}" y="0" width="{max(W / n, 2):.1f}" height="{h:.1f}"/>')
    for i, ((name, color, _), vs) in enumerate(zip(series, values)):
        if i == 0:
            svg.append(f'<path class="area" style="fill:{color}" d="{_path(vs, n, top, True, bottom)}"/>')
        svg.append(f'<path class="line" style="stroke:{color}" d="{_path(vs, n, top, False, bottom)}"/>')
    svg.append("</svg>")
    data = {"t0": start, "step": (end - start) / n, "unit": unit,
            "s": [{"n": name, "c": color, "v": [None if v is None else round(v, 2) for v in vs]}
                  for (name, color, _), vs in zip(series, values)]}
    if loss:
        data["loss"] = [round(b["fails"] / b["n"] * 100) if b and b["n"] else None for b in loss]
    html = [f'<figure class="chart{"" if axes else " spark"}" style="--h:{height}px" '
            f'data-chart="{escape(json.dumps(data, separators=(",", ":")))}">']
    if axes:
        html.append(f'<span class="ymax">{fmt(top if top or not bottom else bottom, unit)}</span>')
    html.append("".join(svg))
    if axes:
        html.append('<div class="xaxis">' + "".join(
            f'<span style="left:{left:.2f}%">{label}</span>' for left, label in ticks(start, end)) + "</div>")
        if len(series) > 1:
            html.append('<figcaption>' + "".join(
                f'<span><i style="background:{c}"></i>{escape(n)}</span>' for n, c, _ in series) + "</figcaption>")
    html.append("</figure>")
    return Markup("".join(html))


def strip(days: list[dict]) -> Markup:
    """A row of day cells, one per day, colored by uptime (like a status page)."""
    cells = []
    for d in days:
        up = d["up"]
        cls = "none" if up is None else "ok" if up >= 0.999 else "warn" if up >= 0.98 else "bad"
        when = datetime.fromtimestamp(d["day"])
        tip = f"{DAYS[when.weekday()]} {when.day}/{when.month}: " + (
            "sin datos" if up is None else f"{up * 100:.2f}%" + (f", {d['down_min']} min caído" if d["down_min"] else ""))
        cells.append(f'<i class="{cls}" title="{escape(tip)}"></i>')
    return Markup(f'<div class="strip">{"".join(cells)}</div>')


def heatmap(cells: dict[tuple[int, int], tuple[float, float]], unit: str = "ms") -> Markup:
    """Weekday x hour grid: cells[(weekday, hour)] = (average, share of failed samples).
    Color goes from green (the best quarter of hours) to red (the worst); a failed ping outlines it."""
    values = sorted(v for v, _ in cells.values() if v is not None)
    if not values:
        return Markup('<p class="dim">Hacen falta unos días de datos.</p>')
    lo, hi = values[len(values) // 10], values[min(len(values) - 1, len(values) * 9 // 10)]
    out = ['<div class="heat"><span></span>' + "".join(f"<b>{h:02d}</b>" if h % 3 == 0 else "<b></b>" for h in range(24))]
    for wd in range(7):
        out.append(f"<span>{DAYS[wd]}</span>")
        for h in range(24):
            v, fail = cells.get((wd, h), (None, 0))
            if v is None:
                out.append('<i class="none"></i>')
                continue
            t = 0 if hi <= lo else min(max((v - lo) / (hi - lo), 0), 1)
            hue = round(130 - 130 * t)  # 130 green .. 0 red
            tip = f"{DAYS[wd]} {h:02d} h: {fmt(v, unit)}" + (f", {fail * 100:.1f}% sin respuesta" if fail else "")
            out.append(f'<i style="background:hsl({hue} 55% 38%)" class="{"fail" if fail else ""}" title="{escape(tip)}"></i>')
    out.append("</div>")
    return Markup("".join(out))

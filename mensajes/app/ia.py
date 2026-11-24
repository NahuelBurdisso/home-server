"""What the rules don't recognize ("¿cuánto gasté en delivery este mes?", "¿dónde está la garantía
de la heladera?", "manteca") goes to the local AI (qwen3 on Ollama), with the apps' own tools: the
same /ia/openapi.json each app gives Open WebUI's "Asistente del server". The numbers and dates come
from the apps already worked out; the model only picks the tool and writes the answer. Nothing
leaves the Mac.

MENSAJES_IA=ollama (default) or none."""
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from .house import URLS

OLLAMA = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
TOOL_APPS = ("finanzas", "recordatorios", "pulso", "lista", "papeles", "vigia", "pedidos")
ROUNDS = 4  # tool calls before it has to answer
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

SYSTEM = """Sos el asistente del servidor casero de la casa, en Córdoba, Argentina, y respondés por Telegram.
Ahora es {now}.
Respondé en español rioplatense, corto y directo: dos o tres líneas, sin títulos ni tablas. Para resaltar usá *así*.
- Plata (ingresos, gastos, categorías, comercios, cuánto quedó, el dólar): herramientas de Finanzas. Para comparar meses, gastos_por_categoria con el rango.
- Fechas, eventos, cumpleaños, vencimientos: herramientas de Recordatorios.
- El servidor (internet, cortes de luz, servicios, alertas): herramientas de Pulso.
- La lista de compras: herramientas de Lista. Si el mensaje es solo una cosa para comprar ("manteca", "pilas y yerba"), agregala con agregar_a_la_lista pasando el mensaje tal cual.
- Documentos (garantías, facturas, contratos, recetas, pólizas): herramientas de Papeles; pasá el link.
- Páginas que el server vigila (precios, cambios, turnos) y lo guardado (links, videos y audios con resumen): herramientas de Vigía.
- Para anotar un gasto en efectivo, anotar_gasto con la frase tal cual. Para agendar, agendar_recordatorio con la frase tal cual, con el cuándo. No calcules fechas vos.
- Usá solo los números y fechas que devuelven las herramientas: ya vienen sumados, no los recalcules ni inventes.
- Nunca nombres las herramientas ni le pidas a la persona que escriba comandos: ella habla normal.
- Si te piden algo que ninguna herramienta hace, decí que eso no lo podés hacer y que con "ayuda" ve lo que sabés hacer. No inventes estados ni resultados."""


def enabled() -> bool:
    return os.environ.get("MENSAJES_IA", "ollama") == "ollama"


_tools: tuple[float, list, dict] = (0.0, [], {})


def _schema(spec: dict, s: dict) -> dict:
    """An OpenAPI schema with its $ref resolved, trimmed to what the model needs."""
    if "$ref" in s:
        s = spec["components"]["schemas"][s["$ref"].split("/")[-1]]
    out = {"type": s.get("type", "string")}
    if s.get("description"):
        out["description"] = s["description"]
    if "anyOf" in s:  # Optional[str]: the non-null one
        out["type"] = next((a.get("type") for a in s["anyOf"] if a.get("type") != "null"), "string")
    if s.get("enum"):
        out["enum"] = s["enum"]
    return out


def tools_from(app: str, spec: dict) -> list[tuple[dict, dict]]:
    """Ollama tool definitions from an app's OpenAPI, with how to call each: (tool, route)."""
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in ops.items():
            name = op.get("operationId")
            if not name:
                continue
            props, required, body = {}, [], False
            for p in op.get("parameters") or []:
                if p.get("in") != "query":
                    continue
                props[p["name"]] = {**_schema(spec, p.get("schema") or {}),
                                    **({"description": p["description"]} if p.get("description") else {})}
                if p.get("required"):
                    required.append(p["name"])
            content = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
            if content:
                body = True
                s = content["schema"]
                s = spec["components"]["schemas"][s["$ref"].split("/")[-1]] if "$ref" in s else s
                for k, v in (s.get("properties") or {}).items():
                    props[k] = _schema(spec, v)
                required += s.get("required") or []
            tool = {"type": "function", "function": {
                "name": name, "description": (op.get("summary") or op.get("description") or name)[:300],
                "parameters": {"type": "object", "properties": props, "required": required}}}
            out.append((tool, {"app": app, "method": method.upper(), "path": path, "body": body}))
    return out


def tools() -> tuple[list, dict]:
    """Every app's tools, asked again every 10 minutes (an app that was down comes back)."""
    global _tools
    at, defs, routes = _tools
    if defs and time.time() - at < 600:
        return defs, routes
    defs, routes = [], {}
    for app in TOOL_APPS:
        try:
            spec = httpx.get(f"{URLS[app]}/ia/openapi.json", timeout=5).json()
        except (httpx.HTTPError, ValueError):
            continue
        for tool, route in tools_from(app, spec):
            defs.append(tool)
            routes[tool["function"]["name"]] = route
    _tools = (time.time() if defs else 0.0, defs, routes)
    return defs, routes


def call(route: dict, args: dict, who: str = "") -> str:
    """who: the person's name, sent as X-Quien (Finanzas writes a cash expense down as theirs)."""
    url = f"{URLS[route['app']]}/ia{route['path']}"
    headers = {"X-Quien": quote(who)} if who else {}
    try:
        if route["body"]:
            r = httpx.request(route["method"], url, json=args, timeout=120, headers=headers)
        else:
            r = httpx.request(route["method"], url, params={k: v for k, v in args.items() if v not in (None, "")},
                              timeout=60, headers=headers)
        return r.text[:6000]
    except httpx.HTTPError as e:
        return json.dumps({"error": f"La app no respondió ({type(e).__name__})."})


def chat(messages: list[dict], defs: list) -> dict:
    body = {"model": MODEL, "messages": messages, "tools": defs, "stream": False, "think": False,
            "options": {"temperature": 0.2, "num_ctx": 16384}, "keep_alive": "30m"}
    r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=180)
    r.raise_for_status()
    return r.json()["message"]


def answer(text: str, history: list[tuple[bool, str]], now: datetime | None = None, chat_fn=chat,
           who: str = "") -> tuple[str, list[str]]:
    """(the reply, the tools it used). history: the last messages of this chat, oldest first,
    as (incoming, text); who: the person's name, passed on to the apps. Raises on Ollama errors:
    the caller says so."""
    now = now or datetime.now()
    defs, routes = tools()
    messages = [{"role": "system", "content": SYSTEM.format(now=f"{DAYS[now.weekday()]} {now:%d/%m/%Y %H:%M}")}]
    messages += [{"role": "user" if incoming else "assistant", "content": t} for incoming, t in history]
    messages.append({"role": "user", "content": text})
    used = []
    for _ in range(ROUNDS + 1):
        msg = chat_fn(messages, defs)
        calls = msg.get("tool_calls") or []
        if not calls:
            return _clean(msg.get("content") or ""), used
        messages.append(msg)
        for c in calls:
            fn = c.get("function") or {}
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            route = routes.get(name)
            result = call(route, args, who) if route else json.dumps({"error": f"No existe la herramienta {name}."})
            used.append(name)
            messages.append({"role": "tool", "content": result, "tool_name": name})
        if len(used) >= ROUNDS:
            defs = []  # last round: it answers with what it has
    return "No llegué a una respuesta. Probá preguntarlo de otra forma.", used


def wrote(used: list[str]) -> bool:
    """Whether any of those tools changed something (all but GETs write: a cash expense, the list...)."""
    routes = _tools[2]
    return any((routes.get(n) or {}).get("method", "GET") != "GET" for n in used)


def _clean(text: str) -> str:
    """Without qwen's <think> block and the markdown the chat doesn't show (# titles, **double**: telegram.py reads *single*)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return text.strip()

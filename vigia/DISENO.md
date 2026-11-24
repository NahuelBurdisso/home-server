# Vigía: the server watches the outside world

Port **3021**, container `vigia`, tailnet only (`tailscale serve`), house app (Nahuel and Sofía),
same shape as `lista/` (FastAPI + Jinja + SQLite in `/data/vigia.db`, tests run inside the image).
Two halves that share one database and one page:

- **Vigilancias**: pages checked on a schedule; a message when something changes.
- **Guardado**: send it a link (or a long audio, through the bot) and it keeps the text, writes a
  summary with the local model, and makes it searchable.

UI and messages in Spanish (rioplatense), code and comments in English, like the other apps.

## Decisions

1. **One app, not two.** Both are "something from outside comes in"; they share the fetcher, its
   safety rules, the notifier and the Telegram wiring.
2. **No headless browser.** Plain HTTP fetch (httpx, a normal browser User-Agent, 20 s timeout,
   2 MB cap, follows up to 5 redirects). A page that needs JavaScript fails **visibly**: adding a
   watch runs the first check at once and shows what it read, so the person knows right there.
3. **The fetcher never reaches inside.** The other apps trust requests that arrive without proxy
   headers, so a URL pointing at this Mac would be a way in. Before every request **and every
   redirect hop**: only `http`/`https`, resolve the host, refuse loopback, private (10/8,
   172.16/12, 192.168/16), link-local, CGNAT 100.64/10 (the tailnet), multicast, unspecified and
   their IPv6 equivalents, and the names `localhost`, `host.docker.internal`, `*.ts.net`, `*.lan`,
   `*.local`. Connect to the address that was checked. One module (`fetch.py`), one function, tested.
4. **The model only writes; it has no tools.** Page text is untrusted. The summarizer gets the text
   as data in a single call with no tool definitions, so the worst an injected page can do is a bad
   summary. Numbers (prices, dates of checks) never come from the model.
5. **Rules decide, never the model**: what changed, what the price is, whether to alert.
6. **Baseline first.** The first check of a watch stores the value and alerts nothing.
7. **Quiet on trouble.** Three failed checks in a row → one notice ("no puedo leer X"), then silence
   until it works again (no notice on recovery). Never a notice per failure.
8. **Polite.** Minimum interval 15 min, default 60; at most one request at a time per host; a
   random 0–60 s delay per check.

## Watches (`kind`)

| kind | Reads | Alerts when |
|---|---|---|
| `cambio` | the visible text of `selector` (CSS), or of the page body without script/style/nav/header/footer; whitespace collapsed | the text differs from the last one. The message carries what was added and removed (difflib over lines, 600 chars at most) |
| `precio` | a number, tried in order: `selector` if given → JSON-LD `Product.offers.price`/`lowPrice` (also inside `@graph` and lists) → `<meta itemprop="price">` / `product:price:amount` / `og:price:amount` | it drops ≥ `umbral` % (default 1) from the last value, it is the lowest ever seen, or it crosses under `objetivo` if one was given. Rises are stored, not announced |
| `texto` | whether `needle` is in the page text (accents and case folded) | it goes absent→present (`cuando=aparece`) or present→absent (`cuando=desaparece`, e.g. "No hay turnos disponibles") |

Price parsing accepts Argentine and plain formats: `$ 1.234.567,89`, `1234567.89`, `1.234.567`.
The price page shows: now, lowest, highest, the median of the last 30 days and how today compares
("12% abajo de su precio habitual" / "igual que siempre: no es oferta"), and an inline SVG line.

## Data

```
watches(id, name, url, kind, selector, needle, cuando, objetivo, umbral, every_min, active,
        added_by, chat,            -- who asked, and the Telegram chat to answer (may be NULL)
        created_at, last_check, last_ok, fails, last_error, value_num, value_text, last_change)
checks(id, watch_id, ts, value_num, changed, note)   -- one row per change, plus one per day
saved(id, url, title, kind,        -- articulo, video, audio, nota
      text, summary, status,       -- pendiente, listo, sin_resumen, error
      error, added_by, chat, created_at)
saved_fts(title, summary, text)    -- FTS5, kept in step with saved (unicode61 remove_diacritics 2)
meta(key, value)
```

## Guardado

`POST /api/guardar` (`{"url": ...}` or `{"texto": ..., "titulo": ...}`, plus optional `quien`,
`chat`) answers at once with the id; a single worker thread does the rest:

1. YouTube link → the captions (`youtube-transcript-api`, `es` then `en`, then any). No captions →
   `error` with a plain sentence. Never downloads video or audio.
2. Any other link → fetch (decision 3) → `trafilatura` for title and main text.
3. Summary with Ollama (`OLLAMA_URL`, `OLLAMA_MODEL=qwen3:8b`, `think: false`, `num_ctx 16384`,
   temperature 0.2): one line saying what it is, then 3 to 5 bullets. Texts over ~20,000 chars are
   summarized in up to 5 parts and then joined in one more call; beyond that it says the summary
   covers the first part. Ollama down → `sin_resumen`; the worker retries those every 15 min.
4. When done, if `chat` is set, send the summary and the link to the item through Mensajes
   (`POST {MENSAJES_URL}/api/enviar`).

Same URL sent twice → the existing item, not a second one.

## Notifications

A watch alert or a finished summary goes to the Telegram chat that asked for it (through Mensajes'
`/api/enviar`); when there is no chat (added on the page) or Mensajes fails, to ntfy on the house
topic the other house apps use. Every alert carries the link to the watch's page.

## Pages

`/` vigilancias (cards: name, what it read last, when, state; add form; pause, delete, "probar
ahora") · `/v/{id}` history, diff or price line · `/guardado` list and search · `/g/{id}` summary,
full text, the original link · `/ayuda` (what works, what does not, the phrases for the bot).
Look: `lista/app/static/style.css` as the base.

## API

- `POST /api/vigilar` — text in (`"seguí el precio de <url>"`, `"avisame si cambia <url>"`,
  `"avisame cuando aparezca \"turnos\" en <url>"`, `"… cuando baje de 150000"`), one sentence out
  that says **what it read on the first check**. `X-Ids` with the new id (the bot's "borrar").
- `GET /api/vigilancias` — one sentence listing them, for the bot and Siri.
- `POST /api/guardar` — above.  `DELETE`-style undo endpoints as the other apps do for `X-Ids`.
- `/ia` (OpenAPI for the assistants): `ver_vigilancias`, `buscar_en_guardado`, `guardar_link`.
- `/health`: database answers and the scheduler looped in the last 5 minutes.

## Mensajes (the bot)

New rules in `intents.py`, before the AI fallback:

- the message is just a URL, or `guardá|resumí|leé|resumime <url>` → Guardado.
- `vigilá|vigilar|seguí el precio de|avisame si cambia|avisame cuando … <url>` → a watch.
- `¿qué estoy vigilando?`, `vigilancias`, `/vigia` → the list.
- a voice note longer than the 300 s limit and up to 45 min: instead of refusing, answer "Es largo:
  lo transcribo y te mando el resumen", transcribe in one background thread (one at a time, its own
  DB connection, never blocking the poller) and send the text to Guardado as `audio`.
- `vigia` joins `URLS`, `NAMES`, `ICONS` and the AI's `TOOL_APPS`.

## Not in this version

Pages behind a login or built by JavaScript; screenshots; video without captions.

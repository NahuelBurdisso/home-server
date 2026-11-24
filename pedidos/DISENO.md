# Pedidos: the house orders its food delivered

Port **3022**, container `pedidos`, tailnet only (`tailscale serve`), house app (Nahuel and Sofía),
same shape as `lista/` and `vigia/` (FastAPI + Jinja + SQLite in `/data/pedidos.db`, tests run inside
the image). It turns the shopping list into one order per shop, with the shop's real products and
prices where it can read them, and leaves each order one tap away from being sent.

UI and messages in Spanish (rioplatense), code and comments in English, like the other apps.

## Decisions

1. **It prepares; a person sends and pays.** The server has no WhatsApp and never holds a card. An
   order ends as a link (`https://wa.me/<phone>?text=<order>`) that opens WhatsApp with the text
   written, or as a list next to the shop's page. Nothing leaves the house without a tap.
2. **Shops are data, not code.** A provider has a `kind`:
   - `pedix`: a shop on pedix.app. Its page carries the whole catalog (products, presentations,
     prices, minimum purchase, phone) as JSON in `<script id="ng-state">`, readable without
     JavaScript. The order goes by WhatsApp to the shop's phone; the page link is offered too.
   - `whatsapp`: a shop that takes orders by message. No catalog: the lines go as written.
   - `web`: a shop with its own site and cart (a supermarket's web shop). No catalog: the page shows the
     list to tick while filling the cart there, and a button to open the site.
3. **Seeded**: *Verdufresca* (`pedix`, `https://pedix.app/verdufresca`, section `verduleria`), *Carnes
   del Centro* (`whatsapp`, phone `5493515550123`, section `carniceria`) and *Almacén del
   Centro* (`whatsapp`, phone `5493515550789`, every other section). More can be added in
   `/proveedores`, including `web` shops. A provider not configured yet (no url and no phone) still
   builds its order, and says what it is missing instead of offering a send button.
4. **The fetcher only goes to pedix.app.** `kind=pedix` URLs must be `https://pedix.app/<slug>`
   (slug `[a-z0-9-]+`); nothing else is ever fetched, no redirects to other hosts. `web` URLs are
   only links (must be `https://`). So there is no way to point it at the Mac or the tailnet.
5. **Rules decide, never the model**: which product a line is, how many, what it costs. Catalog
   text is untrusted data and is never given to a model with tools.
6. **It learns the house's choices.** When a person changes the product or presentation of a line,
   that choice is remembered per provider and thing (`prefs`), and used next time.
7. **Stale is said, not hidden.** If the catalog can't be read, the last one is used and the page
   says from when. Prices are "estimados": the shop weighs and charges.

## Data

```
providers(id, name, kind, url, phone, sections,   -- sections: comma list of Lista section ids
          min_purchase, note, active, catalog_at, catalog_error)
products(provider_id, ext_id, name, key, category,  -- key: folded name, for matching
         presentation, pres_id, price, grams, units, active)  -- one row per product × presentation
prefs(provider_id, key, ext_id, pres_id)           -- key: the thing as Lista keys it ("papas")
orders(id, provider_id, status,                    -- borrador, enviado, recibido, cancelado, guardado
       name,                                       -- a guardado's name; '' for the rest
       created_by, created_at, sent_at, received_at, total_est, note)
lines(id, order_id, text, key, lista_id,           -- text as on the list; key as Lista keys it (for prefs);
      ext_id, pres_id, product, presentation, qty, unit_price)   -- lista_id may be NULL; ext_id NULL: as written
meta(key, value)                                   -- direccion, nombre, aclaracion (Ajustes page); seeded
```

A product without presentations is one row with `presentation = ''` and the product's own price.
Inactive products and inactive categories are not stored. A read fills the provider's phone and
minimum only while they are empty, so a hand-written one wins.
`orders.name` came later: `db.connect()` adds the column to a database that doesn't have it.

## Catalog (`catalogo.py`)

`ng-state` → `app-request-context-state.catalog`: `establishment` (`name`, `phone`,
`catalogConfiguration.minimumPurchase`) and `categories[].products[]` (`id`, `name`, `price`,
`active`, `presentations.items[]` with `id`, `name`, `price`). Presentation names are read into
grams or units: `1 KG`, `2 KG`, `500 GR`, `250 GR`, `1/2 KG`, `1/4 kg`, `UNIDAD`, `1 UNID`,
`1 UNIDAD`, `MEDIA`, `1 MITAD`, `1/2 UNIDAD`, `1/2 SANDÍA` (→ half a unit); unknown → neither.
Refreshed when a draft is built and the catalog is older than 3 h, and by a button. A page that
doesn't parse leaves the old catalog untouched and stores the error.

## Matching (`match.py`)

A Lista line ("2 kg de papas") gives an amount (grams, units or nothing) and a key ("papas").

1. A `prefs` row for (provider, key) whose product still exists wins.
2. Else candidates are products whose name contains every word of the key, singular or plural
   (the same rule as Lista's `_same`). Ranked: name equal to the key, then name starting with it,
   then fewest extra words, then name. The first is taken; the rest are the alternatives the
   draft page offers. No candidates → the line goes as written (`ext_id` NULL).
3. Amount → presentations: grams are covered greedily with the product's weight presentations,
   largest first (1.5 kg → 1 × `1 KG` + 1 × `500 GR`, two lines), a leftover rounding up with one
   more of the smallest (300 g → 2 × `250 GR`); units with unit presentations; whole units of a
   product without presentations are its quantity ("3 mangos" → 3 × Mango). No amount, or an amount the product can't express (kg asked, sold by unit) → 1 × the
   preferred presentation, else the first; the line keeps the text as written so the person sees it.

## Flow

1. **Armar** (`POST /armar`, `/api/armar`): reads Lista's pending items (`GET {LISTA_URL}/api/pendientes`),
   or the things in the request (read by Lista: `POST {LISTA_URL}/api/leer`), splits them by the
   providers' sections, and writes one `borrador` per provider that got something, replacing that
   provider's previous draft. Can be limited to one provider.
2. **Draft page** (`/p/{id}`): each line with its product, presentation, quantity (− / +), price
   and alternatives; lines not found marked "va como está escrito"; add a line by hand (searches the
   catalog); "lo de siempre" (products in ≥ 2 past orders to this provider, not in this one);
   estimated total and, under the provider's minimum, how much is missing.
3. **Send**: the WhatsApp button (pedix, whatsapp) or "Abrir la tienda" (pedix, web). The message:

   ```
   Hola! Quiero hacer un pedido a domicilio:
   • 2 × Papa blanca (1 KG)
   • 1 × Banana ecuatoriana (500 GR)
   • Rúcula
   Total estimado: $ 23.400
   Entrega: <direccion>
   <aclaracion>
   Gracias! <nombre>
   ```
   Lines for missing meta are left out.
4. **Ya lo pedí** (`POST /p/{id}/enviado`): status `enviado`, and the lines that came from Lista are
   ticked there (`POST {LISTA_URL}/api/tildar {"ids": [...]}`). **Llegó** → `recibido`.
   **Cancelar** → `cancelado` (a sent one too; nothing is unticked).

## The message to a pedix shop

A pedix shop gets the text its own page writes when you order there (`orders.pedix_message`), so whoever
reads it finds everything where they expect it: Tienda, Fecha, Nombre, Teléfono; Forma de pago, Total;
Entrega, Dirección, Referencia; then the lines under their category in capitals, each with its price
(`1x FRUTILLAS (1/2 KG): $4.999,50`), `TOTAL` and the closing line. Left out: pedix's order number and its
map link (the order doesn't exist in pedix, we only write to the shop), "Pago con", and whatever isn't
set in Ajustes (teléfono and forma de pago are only used here; the referencia also goes to WhatsApp-only shops, in parentheses after the address). Things not in the catalog go
last under `OTROS`, as written. WhatsApp-only shops keep the plain list.

## Guardados

An order saved to reuse it ("lo de siempre de la verdulería"). **A guardado is an order**: a row in
`orders` with status `guardado` and a `name`, its lines in `lines`. So its page is the order page and
it is edited with the same line routes as a draft (`EDITABLE = borrador, guardado`).

- **Guardar para repetir** (`POST /p/{id}/guardar`), from any order that isn't a guardado, past ones
  too: a copy of its lines. Empty name → the shop's name. The same name (folded) at the same shop
  replaces the old one. `lista_id` is dropped: a reused order must never tick the Lista.
- **Usar** (`POST /p/{id}/usar`): refreshes the catalog (like Armar), takes **today's** prices (a
  product that left the catalog goes as written), and **adds** the lines to the shop's draft, or
  makes one. It never replaces or deletes a draft: what was already there stays. A line already in the
  draft (same product and presentation, or same text when as written) is skipped.
- **Prices stay today's without anyone asking**: a background task in the app (`PEDIDOS_REFRESH_MINUTES`,
  180) reads again the catalogs of the shops that have a guardado and reprices them all; the guardado's
  page, the home list, `/api/guardados` and `/ia/guardados` also reprice from the stored catalog when
  asked (no network). Only shops with a catalog (pedix) have prices to keep.
- Rename (`POST /p/{id}/nombre`) and delete (`POST /p/{id}/borrar`, only a guardado).
- A guardado is never an open order, isn't in Historial, can't be sent, received or cancelled, isn't
  counted by "lo de siempre", and Armar doesn't touch it.

## Lista gains three endpoints (no behaviour change)

- `GET /api/pendientes` → `[{"id", "text", "key", "section"}]`
- `POST /api/leer {"texto"}` → the same shape without ids, nothing stored
- `POST /api/tildar {"ids": [...]}` → ticks those that are pending (idempotent) → `{"tildados": n}`

## API (plain text out, for Mensajes and Siri)

- `POST /api/armar {"texto"?, "proveedor"?, "quien"?}` → `texto`: things to order instead of the
  list; `proveedor`: a name, or a word like "verdura", "verdulería", "carne", "carnicería",
  "súper". Answers one block per order: provider, number of lines, lines not found, estimated total,
  the minimum if short, the link to the draft and, when it can be sent, the WhatsApp link.
  Header `X-Ids`: the order ids (Mensajes' "borrar" cancels them: `POST /p/{id}/cancelar`).
- `GET /api/pedidos` → open orders (drafts and sent, not received) in a sentence each.
- `GET /api/precio?q=banana` → what the catalogs have for that, with presentation and price.
- `GET /api/guardados` → the guardados: name, shop, things, total.
- `POST /api/usar {"nombre", "quien"?}` (or the name as a plain body) → finds the guardado by name, part
  of it, or its shop when it has only one; uses it and answers what it did plus the draft's block.
  `X-Ids`: the draft's id. A name that fits none → which ones there are.
- `POST /api/guardar {"id", "nombre"?}` → saves that order; `X-Ids`: the guardado's id.
- `GET /health`

`/ia` (Open WebUI tools): `ver_pedidos`, `armar_pedido`, `precio_de`, `ver_guardados`,
`usar_guardado`. Building a draft (also from a guardado) sends nothing, so the model may do it.

`GET /api/p/{id}` (Claude Code): one order as JSON, with `nombre` and each line's `opciones` while
it can be changed (borrador or guardado).

## Pages

`/` open orders, the guardados (each with "Usar") and a button per provider plus "Armar con la
lista" · `/p/{id}` the draft, or a guardado ("Usar este pedido", rename, delete); every other order
with lines offers "Guardar para repetir" ·
`/historial` · `/proveedores` (add: paste a pedix link, a phone, or a site; edit sections, minimum,
note; "Actualizar precios") · `/ajustes` (dirección, nombre, aclaración) · `/ayuda`.

## Wiring

Homepage, `tailscale-serve.sh` (3022), `tailscale-policy.hujson` (house app: Sofía too),
`healthcheck.sh`, `sqlite-dumps.sh`, `acceso-sofia.sh`, `accesos-escritorio.py`,
`open-webui-asistente.sh`, Pulso's map, README. Mensajes: «armá el pedido», «pedí la verdura»,
«pedí la carne», «pedí al súper», «pedí 2 kg de papas y bananas a la verdulería», «¿cuánto está la
banana?», «pedidos», `/pedidos`.

## Not in this version

Filling a supermarket's web cart, sending by itself, paying, price history, writing the expense
into Finanzas when the order arrives.

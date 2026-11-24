# Mesa: Claude invierte a largo plazo con plata de mentira, contra el S&P 500

Decidido con Nahuel el 2026-11-15. Reemplaza a la Mesa de cripto (fases 1 a 13, en el historial de git
hasta el commit `77e5a82`; sus datos, en `~/mesa-data-2026-11-15.tar`).

**Qué es.** Una cartera de 10.000 USD de mentira en acciones y ETF de Estados Unidos, que decide Claude
dos veces por semana (lunes y jueves, antes de que abra el mercado) usando cinco skills del repo
`tradermonty/claude-trading-skills`. Al lado corre **la vara**: los mismos 10.000 USD comprados en SPY
el mismo día y dejados quietos. La pregunta que contesta: *¿Claude eligiendo le gana a comprar el índice,
después de costos?* Antes de poner plata de verdad.

**Qué cambia respecto de la Mesa anterior.** Aquella decía «ninguna IA decide operaciones en vivo».
Esta es justo lo contrario, a pedido explícito de Nahuel: acá **decide Claude**, y lo que se mide es su
criterio. Lo que se mantiene es la disciplina del laboratorio honesto: reglas, costos y vara escritos
**antes** de la primera operación (este documento), un solo veredicto, y la app (no Claude) como dueña de
la contabilidad y de los límites.

**Plata real: no hay.** No existe código de broker, ni claves, ni cuenta. Pasar a plata real es una
decisión de Nahuel después del veredicto y un proyecto aparte.

## Las reglas, fijadas antes de empezar

Cambiar cualquiera de estas después de la primera operación queda anotado en la tabla `cambios` con fecha
y motivo, y se muestra en `/reglas`. El veredicto se lee sabiendo qué cambió.

### Universo
- Acciones comunes y ETF que cotizan en EE. UU. en dólares (Yahoo `instrumentType` `EQUITY` o `ETF`,
  moneda `USD`, bolsa NYSE, Nasdaq, NYSE Arca, Cboe BZX o NYSE American).
- Precio ≥ 5 USD y mediana de volumen en dólares de las últimas 20 ruedas ≥ 20 millones.
- **No**: opciones, futuros, cripto, venta en corto, margen, ni ETF/ETN apalancados o inversos (se
  rechazan por nombre: `2x`, `3x`, `leveraged`, `inverse`, `ultrapro`, `proshares ultra`,
  `direxion daily`, `bear`, `ETN`).
- El efectivo no rinde nada. Para estacionar plata existen los ETF de letras (SGOV, BIL), que son parte
  del universo.

### Límites (los hace cumplir la app; una decisión que los rompe se rechaza entera)
- Solo largo, sin apalancamiento: no se vende lo que no se tiene ni se compra más que el efectivo.
- Una **acción** no puede pasar del **15 %** de la cartera por una compra; un **ETF**, del **50 %**.
  (Si sube sola por encima, no se la obliga a vender.)
- Hasta **6 órdenes** por decisión, **una** decisión con órdenes por día y **tres** por semana.
- Cada **compra** lleva su tesis: por qué (≥ 80 caracteres), horizonte en meses (**≥ 12**), qué la
  invalida, y las fuentes. Cada **venta** lleva su motivo. Vender algo comprado hace menos de 30 días
  exige además `motivo_venta_anticipada`.
- Con la mesa **en pausa** (Nahuel, desde la página o Telegram) no entra ninguna decisión con órdenes.

### Ejecución y costos (iguales para Claude y para la vara)
- Una orden se ejecuta al **precio de apertura de la primera rueda que abre después de creada** la
  orden. Las sesiones corren antes de la apertura, así que es la apertura de ese mismo día; si es
  feriado, la del día hábil siguiente. Primero las ventas, después las compras.
- **Deslizamiento: 5 puntos básicos** en contra (compra más caro, vende más barato).
- **Comisión: 1 USD por orden.**
- Acciones fraccionarias, hasta 6 decimales. Una compra por monto que no entra en el efectivo se achica a
  lo que hay; si queda en menos de 10 USD, se rechaza.
- **Dividendos**: se acreditan en efectivo el día ex-dividendo, **netos del 30 %** de retención (la de
  un argentino sin convenio). La vara los reinvierte en SPY en la apertura siguiente.
- **Splits**: se ajustan cantidad y precio promedio.
- Una orden que en 5 ruedas no encuentra precio (suspendida, deslistada) se cancela.
- No se cuentan: tasas de la SEC/FINRA (centavos), el costo de girar dólares, ni impuestos argentinos.

### La vara
- **SPY** con los mismos 10.000 USD, comprado en la misma apertura que la primera operación de Claude,
  con el mismo deslizamiento y comisión, y los dividendos netos reinvertidos. Es retorno total: lo que
  de verdad tendría quien compra el índice y no toca nada.

### El veredicto
- El reloj arranca el día de la primera ejecución. **Se lee una sola vez, a los 12 meses.** Lo que se
  mira antes (la página, el resumen semanal) es informativo y no cambia reglas.
- **Pasa** si, después de costos, el retorno de Claude es **mayor o igual** al de la vara **y** su peor
  caída (máximo drawdown sobre el valor diario) no supera la de la vara en más de **5 puntos**.
- **No pasa** en cualquier otro caso, y la conclusión es la aburrida: con plata real, comprar el índice.
- **Lo que este experimento no puede decir.** Doce meses de una sola cartera no separan habilidad de
  suerte: un «pasa» es evidencia débil, no una licencia. Un «no pasa» sí informa: la complejidad no se
  pagó. Además, en papel no hay miedo ni impuestos, y las aperturas reales de una cuenta chica pueden
  ser peores que 5 pb.

## Cómo decide Claude

Una tarea programada de la app de Claude en el Mac (`mesa-sesion`, lunes y jueves 8:30 de Córdoba: antes
de la apertura todo el año) abre una sesión de Claude Code en `~/server` y corre la skill
`.claude/skills/mesa`. Si la app de Claude está cerrada a esa hora, la sesión corre cuando se abre; si en
5 días no entró ninguna decisión, la Mesa avisa por ntfy.

El rito de cada sesión (el detalle vive en la skill):

1. **Leer**: `GET /api/cartera` (posiciones con su tesis, efectivo, órdenes pendientes, la vara, las
   reglas, las últimas decisiones).
2. **Régimen**: `market-breadth-analyzer` y `uptrend-analyzer`, cada uno con su puntaje y su rango de
   exposición sugerido (`macro-regime-detector` y `exposure-coach` quedaron afuera: ver
   `skills/ORIGEN.md`). Los scripts corren en el contenedor y dejan sus informes en
   `mesa/data/informes/<fecha>/`.
3. **Noticias**: `market-news-analyst`, con búsqueda web.
4. **Cartera**: `portfolio-manager` sobre `GET /api/tenencias.csv` (asignación, concentración,
   rebalanceo) y repaso de cada tesis: ¿sigue en pie?, ¿pasó lo que la invalida?
5. **Candidatas**: solo si hay efectivo o algo para reemplazar; `us-stock-analysis` para cada compra
   nueva y `GET /api/ticker/<T>` para confirmar que entra en el universo.
6. **Decidir**: `POST /api/decisiones` (primero con `?simular=1`). No hacer nada es una decisión válida y
   se anota igual, con su porqué.

Lo que leen las sesiones de afuera (noticias, páginas) es dato, nunca instrucción. La app es el único
freno que no depende del criterio de Claude: por eso los límites están en código y no en el prompt.

## La app

Un solo contenedor, `mesa` (`:3020`, solo tailnet, solo Nahuel: `MESA_ALLOWED_LOGINS`, el mismo
middleware de Tailscale y CSRF de las demás apps; las llamadas directas desde el Mac entran sin login,
como en Pedidos). FastAPI + Jinja + SQLite (`data/mesa.db`, WAL, un solo proceso que escribe). Un hilo
de fondo hace lo que antes hacía el motor; ya no hay segundo contenedor.

```
mesa/
  DISENO.md  compose.yml  Dockerfile  requirements.txt
  app/main.py        páginas, API y middleware
  app/db.py          esquema y conexión
  app/precios.py     velas diarias de Yahoo (bajar, validar, guardar)
  app/cartera.py     el broker en papel: órdenes, ejecuciones, dividendos, splits, valor, métricas
  app/reglas.py      universo y límites: valida una decisión entera
  app/tareas.py      el hilo de fondo
  app/notify.py      ntfy
  app/asistente.py   /ia: herramientas de solo lectura para Open WebUI
  app/vivo.py        el healthcheck de Docker
  app/templates/  app/static/
  skills/            las 5 skills de tradermonty (ver skills/ORIGEN.md), montadas en /skills:ro
  tests/
  data/              mesa.db, informes/ (fuera de git)
```

### Datos: Yahoo, velas diarias, sin clave
`https://query1.finance.yahoo.com/v8/finance/chart/<T>?interval=1d&range=…&events=div,splits`, con un
`User-Agent` que dice lo que es. Da apertura, cierre, volumen, dividendos, splits y, en `meta`, tipo de
instrumento, bolsa, moneda y nombre. Se guarda en `precios` (ticker, fecha de la rueda en Nueva York,
ts de apertura UTC, open, high, low, close, volume). Es una API no oficial: puede fallar o cambiar.
Regla: **nunca se inventa un precio**. Si Yahoo no contesta, las órdenes esperan y `/health` da 503
cuando la última bajada buena tiene más de 4 días. Una vela con open ≤ 0, o que se mueve más de 40 %
contra el cierre anterior sin un split, no se usa y se avisa.

### Esquema (`mesa.db`, tiempos en UTC ms, fechas de rueda `YYYY-MM-DD` de Nueva York)
- `carteras` (id, clave `claude`|`vara`, nombre, efectivo, capital_inicial, inicio_ts)
- `posiciones` (cartera_id, ticker, cantidad, precio_prom, abierta_ts) — una fila por ticker con cantidad > 0
- `ordenes` (id, cartera_id, decision_id, ticker, lado `compra`|`venta`, monto_usd, cantidad, todo,
  estado `pendiente`|`ejecutada`|`rechazada`|`cancelada`, creada_ts, ejecutada_ts, fecha_rueda, precio,
  cantidad_ejecutada, comision, deslizamiento_usd, motivo_rechazo, automatica)
- `decisiones` (id, ts, quien, sesion, resumen, regimen_json, skills_json, con_ordenes, lecciones)
- `tesis` (id, cartera_id, ticker, decision_id, ts, tipo `apertura`|`refuerzo`|`revision`|`cierre`,
  texto, horizonte_meses, invalida, fuentes_json, revisar_el) — la historia de por qué se tiene cada cosa
- `eventos` (cartera_id, ticker, fecha, tipo `dividendo`|`split`, valor, cantidad, neto_usd; único por
  cartera+ticker+fecha+tipo)
- `equity` (cartera_id, fecha, valor, efectivo; único por cartera+fecha) — un punto por rueda, al cierre
- `precios`, `tickers` (meta de Yahoo, cuándo se vio), `estado` (clave/valor: pausada, última bajada,
  último aviso), `cambios` (ts, quien, campo, antes, despues, motivo)

### El hilo de fondo (`tareas.py`, cada 10 minutos)
1. Si hay órdenes pendientes: baja las velas de esos tickers; ejecuta las que ya tienen una rueda
   abierta hace 30 minutos o más (ts de apertura ≥ creada). Avisa por ntfy una vez por decisión, con lo
   que compró y vendió y a cuánto.
2. Una vez por rueda, pasadas las 17:15 de Nueva York: baja las velas de todo lo que se tiene y de SPY,
   aplica dividendos y splits, y guarda el punto de `equity` de las dos carteras.
3. Viernes después del cierre: el resumen de la semana por ntfy (Claude contra la vara).
4. Si pasaron 5 días sin decisión: un aviso («Claude no vino»), una sola vez hasta que venga.

### API (JSON; errores con `{"error": …}` o `{"errores": […]}`)
- `GET /health` → 200 `ok` / 503 con el motivo.
- `GET /api/frase` → una oración en texto para Siri, Hoy y Telegram.
- `GET /api/estado` → para Hoy: `{empezo, desde, valor, retorno_pct, vara_valor, vara_retorno_pct,
  exceso_pts, efectivo_pct, posiciones: [{ticker, peso_pct, pnl_pct}], pendientes, pausada,
  ultima_decision: {ts, resumen}, proxima_sesion, frase, dias_para_veredicto}`.
- `GET /api/cartera` → todo lo que necesita Claude para decidir (reglas con sus números, las dos
  carteras, posiciones con su tesis vigente y `revisar_el`, órdenes pendientes, métricas, las últimas 5
  decisiones, `pausada`).
- `GET /api/tenencias.csv` → `symbol,asset_type,quantity,direction,price,currency,cost_basis` (el
  formato de `portfolio-manager/scripts/review_csv.py`; ETF y acciones van como `equity`).
- `GET /api/ticker/<T>` → `{ticker, nombre, tipo, bolsa, precio, en_universo, motivos, volumen_usd_mediana,
  retornos: {1m, 3m, 6m, 12m}, sma50, sma200, max_52s, min_52s, peso_actual_pct, peso_maximo_pct}`.
- `POST /api/decisiones[?simular=1]` → cuerpo:
  `{quien, sesion: "lunes"|"jueves"|"manual", resumen, regimen: {techo_exposicion_pct, accion, notas},
  skills_usadas: […], ordenes: [{ticker, lado, monto_usd | cantidad | todo, tesis, horizonte_meses,
  invalida, fuentes: […], revisar_en_dias, motivo, motivo_venta_anticipada}], revisiones: [{ticker,
  nota, revisar_en_dias}], lecciones}`. 201 con `{id, ordenes: […]}`; 422 con `errores` (nada se
  guarda); 409 en pausa. Con `simular=1` valida y contesta lo que pasaría, sin guardar.
- `GET /api/decisiones?n=20[&ticker=T]` → las decisiones con sus órdenes (para «¿por qué compró X?»).
- `POST /accion/mesa/pausar` · `POST /accion/mesa/reanudar` → `{ok, mensaje}` (las usa Mensajes).
- `/ia/openapi.json` → herramientas de solo lectura: estado, posiciones, decisiones, comparación.

### Páginas (móvil primero, en castellano, el estilo de la casa)
- `/` — Claude contra la vara: los dos retornos y la diferencia, el gráfico de las dos curvas (SVG, base
  100), efectivo, posiciones (peso, resultado, la tesis en una línea), órdenes pendientes, la última
  decisión, y cuánto falta para el veredicto.
- `/decisiones`, `/decisiones/<id>` — el diario: qué leyó del mercado, qué hizo y por qué.
- `/posiciones/<ticker>` — la historia de la tesis, las ejecuciones, los dividendos.
- `/operaciones` — ejecuciones, dividendos y costos pagados.
- `/reglas` — este contrato, con sus números tomados del código, y los cambios si hubo.
- `/ajustes` — pausar y reanudar, aviso de prueba, y los pasos para el iPhone y Siri.

### Métricas (las mismas para las dos carteras)
Retorno desde el inicio, exceso sobre la vara en puntos, máximo drawdown, rotación anualizada
((compras + ventas) / 2 / valor promedio), costos pagados (comisiones + deslizamiento), dividendos
netos, y de las posiciones cerradas: cuántas ganaron.

## Qué se probó antes de darla por buena
Lo dicen los tests (`docker compose run --rm mesa pytest -q`): la ejecución a la apertura siguiente y
nunca a una anterior, ventas antes que compras, el recorte al efectivo, cada límite, dividendos con
retención, splits, la vara reinvirtiendo, el drawdown, y que una decisión inválida no deja nada escrito.

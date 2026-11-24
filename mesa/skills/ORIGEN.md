# De dónde salen estas skills

Copiadas de https://github.com/tradermonty/claude-trading-skills (licencia MIT, ver `LICENSE`),
commit `13079ab4456fada368f0f579643d2ef104efb8f0` (2026-11-15), sin cambios; solo se sacaron las
carpetas `tests/`.

Cinco de las 74, las que sirven para invertir a largo plazo **sin claves pagas**:

| Skill | Para qué la usa la Mesa | Datos |
|---|---|---|
| `market-breadth-analyzer` | salud de la amplitud del mercado (0-100) | CSV público |
| `uptrend-analyzer` | cuántas acciones están en tendencia, por sector | CSV público |
| `market-news-analyst` | qué movió al mercado en los últimos días | búsqueda web |
| `us-stock-analysis` | análisis de una acción antes de comprarla | búsqueda web |
| `portfolio-manager` | asignación, diversificación y rebalanceo de la cartera | CSV de la Mesa |

**No se actualizan solas.** Son instrucciones y scripts de un tercero: traer una versión nueva es
copiarla a mano desde un commit revisado y cambiar el hash de arriba. Los scripts corren **adentro
del contenedor `mesa`** (`docker exec mesa python /skills/...`), nunca en el Mac.

**Las dos que quedaron afuera a propósito.** `macro-regime-detector`: Sin clave paga baja sus datos con
`yfinance`, que se hace pasar por un navegador para que Yahoo no lo frene; la Mesa no hace eso. Además,
en la prueba del 2026-11-15 su ráfaga de pedidos dejó a Yahoo contestando 429 a toda la casa un rato,
justo la fuente de precios de la Mesa. Vuelve el día que haya una clave de FMP (la skill la usa sola).

`exposure-coach`: junta los informes de ocho skills en un techo de exposición, pero seis de sus
entradas piden claves pagas o la skill de arriba. Probada el 2026-11-15 con las dos que hay (amplitud
51, tendencia 38,6), contestó «techo 15 %, prioridad efectivo, confianza baja» solo porque le faltaban
entradas críticas: eso no es leer el mercado, y empujaría a Claude al efectivo por una razón falsa. El
régimen se lee directo de los dos informes.

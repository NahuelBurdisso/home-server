// Mac server on the iPhone's home screen (Scriptable app). Shows the server's light and why, battery,
// backups, last month's money and what's coming up, from https://mac-server.tailnet-ejemplo.ts.net/widget.json
// (setup/widget-data.py, every 30 minutes). With Tailscale off it shows the last copy, marked as old.
// Install: Scriptable → + → paste this → name it "Mac server"; then add a Scriptable widget (small or
// medium) to the home screen, long-press → Edit widget → Script: Mac server. Tapping it opens Hoy.
const URL = "https://mac-server.tailnet-ejemplo.ts.net/widget.json";
const HOME = "https://mac-server.tailnet-ejemplo.ts.net:3018/";  // Hoy: the morning on one screen

const fm = FileManager.local();
const cache = fm.joinPath(fm.documentsDirectory(), "mac-server-widget.json");

const C = {
  bg: Color.dynamic(new Color("#f6f7f9"), new Color("#15171c")),
  text: Color.dynamic(new Color("#16181d"), new Color("#eceef2")),
  dim: Color.dynamic(new Color("#6b7280"), new Color("#9aa1ad")),
  verde: new Color("#22c55e"),
  amarillo: new Color("#f59e0b"),
  rojo: new Color("#ef4444"),
};

async function load() {
  try {
    const req = new Request(URL);
    req.timeoutInterval = 8;
    const data = await req.loadJSON();
    fm.writeString(cache, JSON.stringify(data));
    return { data, offline: false };
  } catch (e) {
    if (fm.fileExists(cache)) return { data: JSON.parse(fm.readString(cache)), offline: true };
    return { data: null, offline: true };
  }
}

function line(stack, text, { size = 12, color = C.text, bold = false } = {}) {
  const t = stack.addText(text);
  t.font = bold ? Font.semiboldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.7;
  return t;
}

function header(w, d, offline) {
  const row = w.addStack();
  row.centerAlignContent();
  const dot = row.addText("●");
  dot.font = Font.systemFont(14);
  dot.textColor = offline ? C.dim : C[d.luz] || C.dim;
  row.addSpacer(5);
  line(row, "mac-server", { size: 14, bold: true });
  row.addSpacer();
  line(row, offline ? `sin conexión · ${d.hora}` : d.hora, { size: 11, color: C.dim });
}

function build(d, offline, family) {
  const w = new ListWidget();
  w.backgroundColor = C.bg;
  w.url = HOME;
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  w.setPadding(12, 14, 12, 14);
  if (!d) {
    line(w, "mac-server", { size: 14, bold: true });
    w.addSpacer(6);
    line(w, "Sin datos todavía: prendé Tailscale.", { size: 12, color: C.dim });
    return w;
  }
  const small = family === "small";
  header(w, d, offline);
  w.addSpacer(4);

  const warn = d.avisos.length ? d.avisos.join(" · ") : "Todo en orden";
  line(w, warn, { size: 11, color: d.avisos.length ? C[d.luz] : C.dim });
  w.addSpacer(6);

  const power = `${d.enchufado ? "⚡" : "🔋"} ${d.bateria}%`;
  const backup = d.backup_hace_h == null ? "sin backup" : `backup hace ${d.backup_hace_h} h`;
  if (small) {
    line(w, power);
    line(w, `🛟 ${backup}`);
  } else {
    line(w, `${power}  ·  💾 ${d.disco_libre_gb} GB  ·  📦 ${d.contenedores}  ·  🛟 ${backup}`);
    // Internet over the last 24 h, from Pulso (newer widget.json only).
    const n = d.internet;
    if (n && n.uptime_24h != null) {
      line(w, `🌐 ${n.ms != null ? Math.round(n.ms) + " ms" : "sin conexión"}  ·  ${n.uptime_24h}% en 24 h` +
        (n.cortes_24h ? `  ·  ${n.cortes_24h} corte${n.cortes_24h > 1 ? "s" : ""} (${n.min_caido_24h} min)` : ""),
        { size: 11, color: n.cortes_24h ? C.amarillo : C.dim });
    }
  }

  const f = d.finanzas;
  if (f && f.quedo) {
    w.addSpacer(4);
    const verbo = f.negativo ? "gastaste de más" : "quedó";
    line(w, small ? `💰 ${f.quedo.replace("−", "")}` :
      `💰 ${f.mes[0].toUpperCase() + f.mes.slice(1)}: ${verbo} ${f.quedo.replace("−", "")}` +
      (f.para_revisar ? `  ·  ${f.para_revisar} por revisar` : ""));
    if (!small && f.vence) line(w, `💳 Vence ${f.vence}`, { size: 11, color: C.dim });
  }

  w.addSpacer(4);
  const next = d.proximos.slice(0, small ? 1 : 2);
  if (next.length) for (const r of next) line(w, `📅 ${r.cuando}: ${r.titulo}`, { size: 11 });
  else if (!small) line(w, "📅 Nada en los próximos 7 días", { size: 11, color: C.dim });
  w.addSpacer();
  return w;
}

const { data, offline } = await load();
const family = config.widgetFamily || "medium";
const widget = build(data, offline, family);
if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentMedium();
Script.complete();

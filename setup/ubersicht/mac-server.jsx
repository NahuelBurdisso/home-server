// Mac server: live status of every service on the desktop.
// Data comes from ~/server/setup/desktop-status.py (about 1 s per run).
// Click a service (or the title, for Homepage) to open it in the default browser.

import { run } from "uebersicht";

const openUrl = (url) => run(`open '${url}'`);

export const command = "/usr/bin/python3 -I $HOME/server/setup/desktop-status.py";
export const refreshFrequency = 10000;

export const className = `
  top: 28px;
  right: 28px;
  width: 440px;
  font-family: -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif;
  font-variant-numeric: tabular-nums;
  color: #e8eaed;

  .panel {
    background: rgba(18, 20, 24, 0.72);
    -webkit-backdrop-filter: blur(24px) saturate(140%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 18px;
    padding: 18px 20px 16px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  }
  .head { display: flex; align-items: center; gap: 10px; }
  .title { font-size: 17px; font-weight: 600; letter-spacing: -0.2px; }
  .badge {
    margin-left: auto; font-size: 11px; font-weight: 600;
    padding: 3px 9px; border-radius: 999px;
  }
  .badge.ok { background: rgba(52, 199, 89, 0.18); color: #5fd97f; }
  .badge.bad { background: rgba(255, 69, 58, 0.2); color: #ff6b61; }
  .sub { font-size: 11.5px; color: #9aa0a6; margin-top: 4px; }
  .sub b { color: #c8ccd1; font-weight: 500; }

  h4 {
    font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;
    text-transform: uppercase; color: #7d848c; margin: 16px 0 8px;
  }
  .meters { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .meter .label { display: flex; justify-content: space-between; font-size: 11.5px; color: #9aa0a6; }
  .meter .label span:last-child { color: #e8eaed; font-weight: 600; }
  .bar { height: 5px; border-radius: 3px; background: rgba(255,255,255,0.08); margin-top: 5px; overflow: hidden; }
  .bar i { display: block; height: 100%; border-radius: 3px; }
  .facts { display: flex; gap: 16px; font-size: 11.5px; color: #9aa0a6; margin-top: 10px; }
  .facts b { color: #e8eaed; font-weight: 500; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; }
  .grid { margin: 0 -8px; }
  .svc {
    display: flex; align-items: center; gap: 8px; font-size: 12.5px;
    padding: 4px 8px; border-radius: 8px; cursor: pointer;
    transition: background 0.15s;
  }
  .svc:hover { background: rgba(255, 255, 255, 0.08); }
  .svc .role { color: #7d848c; font-size: 11px; margin-left: auto; }
  .svc .go { color: #5b6168; font-size: 11px; width: 0; overflow: hidden; transition: width 0.15s; }
  .svc:hover .go { width: 10px; color: #c8ccd1; }
  .title.link { cursor: pointer; }
  .title.link:hover { color: #fff; text-decoration: underline; text-underline-offset: 3px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .dot.up { background: #34c759; box-shadow: 0 0 6px rgba(52,199,89,0.6); }
  .dot.down { background: #ff453a; box-shadow: 0 0 6px rgba(255,69,58,0.7); }

  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .card {
    background: rgba(255,255,255,0.045); border-radius: 11px; padding: 9px 11px;
  }
  .card .k { font-size: 10.5px; color: #7d848c; }
  .card .v { font-size: 15px; font-weight: 600; margin-top: 2px; }
  .card .d { font-size: 11px; color: #9aa0a6; margin-top: 1px; }
  .foot { font-size: 10.5px; color: #6b7178; margin-top: 12px; display: flex; justify-content: space-between; }
  .err { font-size: 12px; color: #ff6b61; }
`;

const color = (pct) => (pct >= 85 ? "#ff453a" : pct >= 65 ? "#ff9f0a" : "#34c759");

const Meter = ({ label, pct, text }) => (
  <div className="meter">
    <div className="label"><span>{label}</span><span>{text ?? `${pct}%`}</span></div>
    <div className="bar"><i style={{ width: `${Math.min(pct, 100)}%`, background: color(pct) }} /></div>
  </div>
);

const duration = (s) => {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

const ago = (iso) => {
  if (!iso) return "nunca";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 3600 ? `hace ${Math.round(s / 60)} min` : s < 86400 ? `hace ${Math.round(s / 3600)} h` : `hace ${Math.round(s / 86400)} d`;
};

const speed = (kbs) => (kbs >= 1024 ? `${(kbs / 1024).toFixed(1)} MB/s` : `${kbs} KB/s`);

export const render = ({ output, error }) => {
  let d;
  try { d = JSON.parse(output); } catch (e) { d = null; }
  if (!d) {
    return <div className="panel"><div className="title">mac-server</div>
      <div className="err">Esperando datos… {error ? String(error).slice(0, 120) : ""}</div></div>;
  }

  const { system: s, services, tailscale: ts, adguard: ag, ai, containers: c, backup: b } = d;
  const downSvcs = services.filter((x) => !x.up);
  const healthy = downSvcs.length === 0 && c.down.length === 0 && s.internet && ts.state === "Running";
  const peersOn = ts.peers.filter((p) => p.online).map((p) => p.name);

  return (
    <div className="panel">
      <div className="head">
        <div className="title link" title="Abrir Homepage" onClick={() => openUrl("http://localhost:3010")}>mac-server</div>
        <div className={`badge ${healthy ? "ok" : "bad"}`}>
          {healthy ? "Todo OK" : `${downSvcs.length + c.down.length + (s.internet ? 0 : 1)} problema(s)`}
        </div>
      </div>
      <div className="sub">
        <b>{s.ip}</b> · Tailscale <b>{ts.ip || ts.state}</b> · activa {duration(s.uptime_s)} ·{" "}
        {s.on_ac ? "⚡ " : "🔋 "}{s.battery}%{s.internet ? "" : " · sin internet"}
      </div>

      <h4>Sistema</h4>
      <div className="meters">
        <Meter label="CPU" pct={s.cpu} />
        <Meter label="Memoria" pct={s.mem} />
        <Meter label="Disco" pct={s.disk_used_pct} text={`${s.disk_free_gb} GB libres`} />
      </div>
      <div className="facts">
        <span>Carga <b>{s.load}</b></span>
        <span>↓ <b>{speed(s.net_down_kbs)}</b></span>
        <span>↑ <b>{speed(s.net_up_kbs)}</b></span>
      </div>

      <h4>Servicios</h4>
      <div className="grid">
        {services.map((x) => (
          <div className="svc" key={x.name} title={x.url} onClick={() => openUrl(x.url)}>
            <span className={`dot ${x.up ? "up" : "down"}`} />
            <span>{x.name}</span>
            <span className="role">{x.role}</span>
            <span className="go">↗</span>
          </div>
        ))}
      </div>

      <h4>Actividad</h4>
      <div className="cards">
        <div className="card">
          <div className="k">Anuncios bloqueados (24 h)</div>
          <div className="v">{(ag.blocked ?? 0).toLocaleString()}</div>
          <div className="d">{ag.blocked_pct ?? 0}% de {(ag.queries ?? 0).toLocaleString()} consultas DNS</div>
        </div>
        <div className="card">
          <div className="k">IA local</div>
          <div className="v">{ai.loaded.length ? ai.loaded.join(", ") : "En reposo"}</div>
          <div className="d">{ai.loaded.length ? "cargado en memoria" : `${ai.installed.length} modelo(s) listo(s)`}</div>
        </div>
        <div className="card">
          <div className="k">Contenedores</div>
          <div className="v">{c.running} / {c.total} activos</div>
          <div className="d">{c.down.length ? `Caídos: ${c.down.join(", ")}` : "OrbStack"}</div>
        </div>
        <div className="card">
          <div className="k">Último backup</div>
          <div className="v">{ago(b.last)}</div>
          <div className="d">{b.where === "usb" ? "disco USB" : "SSD interno (sin USB aún)"}</div>
        </div>
      </div>

      <div className="foot">
        <span>Equipos: {peersOn.length ? peersOn.join(", ") : "ninguno conectado"}</span>
        <span>{d.time}</span>
      </div>
    </div>
  );
};

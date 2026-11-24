// Crosshair and values for the graphs drawn by app/charts.py (their data is in data-chart).
(() => {
  const units = {
    "%": (v) => (v >= 10 || v === 0 ? v.toFixed(0) : v.toFixed(1)) + "%",
    ms: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " ms",
    x: (v) => v.toFixed(2),
    " Mbit/s": (v) => v.toFixed(0) + " Mbit/s",
    " dBm": (v) => v.toFixed(0) + " dBm",
    " TB": (v) => v.toFixed(2) + " TB",
    "°": (v) => v.toFixed(0) + "°",
    B: (v) => bytes(v, ""),
    "B/s": (v) => bytes(v, "/s"),
  };
  function bytes(v, suffix) {
    for (const u of ["B", "KB", "MB", "GB", "TB"]) {
      if (Math.abs(v) < 1000 || u === "TB") return (Math.abs(v) >= 100 || u === "B" ? v.toFixed(0) : v.toFixed(1)) + " " + u + suffix;
      v /= 1000;
    }
  }
  // Times as the server sees them (the container's TZ, Córdoba), whatever the device's zone is.
  const tz = document.body.dataset.tz || undefined;
  const parts = (t) => Object.fromEntries(new Intl.DateTimeFormat("es-AR", {
    timeZone: tz, weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(t * 1000)).map((p) => [p.type, p.value]));
  function stamp(t, step) {
    const d = parts(t), today = parts(Date.now() / 1000);
    const day = d.weekday.replace(".", "") + " " + d.day + "/" + d.month;
    const time = d.hour + ":" + d.minute;
    if (step >= 86400) return day;
    return d.day === today.day && d.month === today.month ? time : day + " " + time;
  }
  // The map: a node lights up its connections (hover, or tap on the phone).
  const map = document.querySelector("svg.map");
  if (map) {
    const light = (id) => {
      map.querySelectorAll(".hot").forEach((e) => e.classList.remove("hot"));
      if (!id) return;
      map.querySelectorAll(`.edge[data-a="${id}"], .edge[data-b="${id}"]`).forEach((e) => {
        e.classList.add("hot");
        map.querySelector(`.node[data-id="${e.dataset.a === id ? e.dataset.b : e.dataset.a}"]`)?.classList.add("hot");
      });
      map.querySelector(`.node[data-id="${id}"]`).classList.add("hot");
    };
    map.querySelectorAll(".node").forEach((n) => {
      n.addEventListener("pointerenter", () => light(n.dataset.id));
      n.addEventListener("pointerdown", () => light(n.dataset.id));
    });
    map.addEventListener("pointerleave", () => light(null));
  }

  document.querySelectorAll("figure.chart[data-chart]").forEach((fig) => {
    const data = JSON.parse(fig.dataset.chart);
    const n = Math.max(...data.s.map((s) => s.v.length));
    const fmt = units[data.unit] || ((v) => v.toFixed(0) + data.unit);
    const cross = document.createElement("div");
    const tip = document.createElement("div");
    cross.className = "cross";
    tip.className = "tip";
    const hide = () => { cross.remove(); tip.remove(); };
    const show = (clientX) => {
      const box = fig.getBoundingClientRect();
      const x = Math.min(Math.max(clientX - box.left, 0), box.width - 1);
      const i = Math.min(n - 1, Math.floor((x / box.width) * n));
      const rows = data.s.map((s) => `<div><i style="background:${s.c}"></i>${s.v[i] == null ? "—" : fmt(s.v[i])}${data.s.length > 1 ? ` <span class="t">${s.n}</span>` : ""}</div>`);
      if (data.loss && data.loss[i]) rows.push(`<div style="color:var(--red)">${data.loss[i]}% sin respuesta</div>`);
      tip.innerHTML = `<div class="t">${stamp(data.t0 + i * data.step, data.step)}</div>` + rows.join("");
      cross.style.left = ((i + 0.5) / n) * box.width + "px";
      fig.append(cross, tip);
      const w = tip.offsetWidth;
      tip.style.left = Math.min(Math.max(((i + 0.5) / n) * box.width - w / 2, 0), box.width - w) + "px";
    };
    fig.addEventListener("pointermove", (e) => show(e.clientX));
    fig.addEventListener("pointerleave", hide);
    // A tap on a graph shows its values, except on a card: that tap opens the card's page.
    if (!fig.closest("a")) fig.addEventListener("pointerdown", (e) => show(e.clientX));
  });
})();

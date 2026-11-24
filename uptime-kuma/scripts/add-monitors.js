// Adds the HTTP monitors below to Uptime Kuma if no monitor has that name yet, each alerting
// through the ntfy notification (id 1). Kuma has no REST API for this, only the socket its web
// page uses; login is off (Tailscale is the login), so the socket is signed in on connect.
//   docker cp ~/server/uptime-kuma/scripts/add-monitors.js uptime-kuma:/tmp/ &&
//   docker exec -w /app uptime-kuma node /tmp/add-monitors.js
const { io } = require("/app/node_modules/socket.io-client");

const WANTED = [
  { name: "Pulso", url: "http://host.docker.internal:3015/health" },
  { name: "Dozzle", url: "http://host.docker.internal:8080/healthcheck" },
  { name: "socket-proxy", url: "http://socket-proxy:2375/_ping" },
  { name: "Lista", url: "http://host.docker.internal:3016/health" },
  { name: "Papeles", url: "http://host.docker.internal:3017/health" },
  { name: "Hoy", url: "http://host.docker.internal:3018/health" },
  { name: "Mensajes", url: "http://host.docker.internal:3019/health" },
];

const base = {
  type: "http", method: "GET", interval: 60, retryInterval: 60, maxretries: 2, timeout: 48,
  resendInterval: 0, maxredirects: 10, accepted_statuscodes: ["200-299"], active: true,
  notificationIDList: { 1: true }, kafkaProducerBrokers: [], kafkaProducerSaslOptions: { mechanism: "None" },
  conditions: [], rabbitmqNodes: [], httpBodyEncoding: "json", ignoreTls: false, upsideDown: false,
};

const socket = io("http://127.0.0.1:3001", { transports: ["websocket"], reconnection: false });
const fail = (msg) => { console.error(msg); process.exit(1); };
setTimeout(() => fail("timeout"), 20000);
socket.on("connect_error", (e) => fail("connect: " + e.message));
socket.on("monitorList", async (list) => {
  const have = new Set(Object.values(list).map((m) => m.name));
  for (const w of WANTED) {
    if (have.has(w.name)) { console.log("exists:", w.name); continue; }
    const res = await socket.emitWithAck("add", { ...base, ...w });
    console.log(res.ok ? "added:" : "FAILED:", w.name, res.ok ? res.monitorID : res.msg);
  }
  socket.close();
  process.exit(0);
});

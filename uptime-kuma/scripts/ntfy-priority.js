// Uptime Kuma's ntfy notification: "down" is urgent (5: sounds even on Focus), "up" is a normal
// notice (3). Before, both were 5, so a service coming back woke the phone like an outage.
// Kuma keeps this in its database; run again only after rebuilding it:
//   docker cp ~/server/uptime-kuma/scripts/ntfy-priority.js uptime-kuma:/tmp/ &&
//   docker exec -w /app uptime-kuma node /tmp/ntfy-priority.js
const { io } = require("/app/node_modules/socket.io-client");
const socket = io("http://127.0.0.1:3001", { transports: ["websocket"], reconnection: false });
setTimeout(() => { console.error("timeout"); process.exit(1); }, 20000);
socket.on("notificationList", async (list) => {
  const n = list.find((x) => x.name === "ntfy (Mac server)");
  if (!n) { console.log("not found"); process.exit(1); }
  const cfg = { ...JSON.parse(n.config), ntfyPriority: 3, ntfyPriorityDown: 5 };
  const res = await socket.emitWithAck("addNotification", cfg, n.id);
  console.log(res.ok ? `updated: up ${cfg.ntfyPriority}, down ${cfg.ntfyPriorityDown}` : "FAILED " + res.msg);
  process.exit(0);
});

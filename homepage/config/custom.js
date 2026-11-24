// Under each service, the address it opens (fotos.lan, ts:3012 for tailscale serve).
// Homepage renders with React, so re-apply whenever the list changes.
(() => {
  const short = (href) => {
    const u = new URL(href);
    if (u.hostname.endsWith(".ts.net")) return "ts" + (u.port ? ":" + u.port : "");
    return u.host;
  };
  const tag = () => {
    document.querySelectorAll(".service-title-text").forEach((a) => {
      const name = a.querySelector(".service-name");
      if (!name || name.querySelector(".service-host")) return;
      const host = document.createElement("span");
      host.className = "service-host";
      host.textContent = "→ " + short(a.href);
      name.appendChild(host);
    });
  };
  new MutationObserver(tag).observe(document.body, { childList: true, subtree: true });
  tag();
})();

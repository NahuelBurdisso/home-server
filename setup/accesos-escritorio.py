#!/usr/bin/python3 -I
"""Desktop shortcuts (.webloc) for every self-hosted service that has a web page, each with its own
icon. Safe to re-run: it rewrites them.
    python3 ~/server/setup/accesos-escritorio.py

The links are the tailnet HTTPS ones, the same as on the iPhone: that's where tailscale serve says who
is asking, which Open WebUI, Dozzle and the apps' login checks rely on. This Mac doesn't use MagicDNS,
so its own name needs one line in /etc/hosts; the script prints it when it's missing.
Immich and Jellyfin aren't on the tailnet (home Wi-Fi only): those open on localhost.
No page, so no shortcut: Ollama (API), socket-proxy, diun."""
import os
import plistlib
import socket
import subprocess
import tempfile
from html.parser import HTMLParser
from urllib.parse import urljoin

HOST = "mac-server.tailnet-ejemplo.ts.net"
TS = f"https://{HOST}"
ROOT = os.path.expanduser("~/server")
DEST = os.environ.get("DEST", os.path.expanduser("~/Desktop"))

# One desktop column each, from the left (the Übersicht panel takes the top right):
# (name, url, own app folder whose app/static/icon.png is the icon, or None to take the site's own)
COLUMNS = [
    [  # the house
        ("Hoy", f"{TS}:3018/", "hoy"),
        ("Homepage", f"{TS}/", None),
        ("Finanzas", f"{TS}:3013/", "finanzas"),
        ("Recordatorios", f"{TS}:3014/", "recordatorios"),
        ("Lista", f"{TS}:3016/", "lista"),
        ("Papeles", f"{TS}:3017/", "papeles"),
        ("Vigía", f"{TS}:3021/", "papeles"),  # no icon of its own yet: borrows Papeles'
        ("Pedidos", f"{TS}:3022/", "lista"),  # no icon of its own yet: borrows Lista's
        ("Mensajes", f"{TS}:3019/", "mensajes"),
    ],
    [  # media, AI, Nahuel's own
        ("Immich", "http://localhost:2283/", None),
        ("Jellyfin", "http://localhost:8096/", None),
        ("Open WebUI", f"{TS}:3012/", None),
        ("Mesa", f"{TS}:3020/", "mesa"),
        ("Pulso", f"{TS}:3015/", "pulso"),
    ],
    [  # the server
        ("n8n", f"{TS}:5678/", None),
        ("Uptime Kuma", f"{TS}:3011/", None),
        ("Dozzle", f"{TS}:8080/", None),
        ("Glances", f"{TS}:61208/", None),
        ("ntfy", f"{TS}:8443/", None),
        ("AdGuard Home", f"{TS}:3000/", None),
    ],
]

# Sets the Finder icon and hides ".webloc" in the name (plain AppKit, nothing to install).
SET_ICON_JS = """
ObjC.import('AppKit');
function run(argv) {
  const img = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
  if (img.isNil()) return 'no image';
  const ok = $.NSWorkspace.sharedWorkspace.setIconForFileOptions(img, argv[1], 0);
  $.NSFileManager.defaultManager.setAttributesOfItemAtPathError($({NSFileExtensionHidden: true}), argv[1], null);
  return ok ? 'ok' : 'failed';
}
"""


def own_ip():
    try:
        return subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def curl(url, out=None):
    """Fetch with curl so the tailnet name can be pointed at this Mac's own IP (--resolve), like
    healthcheck.sh does. Returns (final url, body) or None."""
    args = ["curl", "-fsSL", "-m", "10", "-w", "\n%{url_effective}"]
    ip = own_ip() if HOST in url else None
    if ip:
        port = url.split(HOST, 1)[1].split("/", 1)[0].lstrip(":") or "443"
        args += ["--resolve", f"{HOST}:{port}:{ip}"]
    if out:
        args += ["-o", out]
    r = subprocess.run(args + [url], capture_output=True, timeout=20)
    if r.returncode:
        return None
    body, _, final = r.stdout.rpartition(b"\n")
    return final.decode(), body


class Icons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        rel = (a.get("rel") or "").lower()
        if tag != "link" or "icon" not in rel or "mask" in rel or not a.get("href"):
            return
        href = a["href"]
        size = max((int(s.split("x")[0]) for s in (a.get("sizes") or "").split() if s.split("x")[0].isdigit()), default=0)
        # Best first: apple-touch-icon (big square PNG), then the largest PNG, then .ico; SVG last.
        score = (0 if "apple" in rel else 1, href.split("?")[0].endswith(".svg"), href.split("?")[0].endswith(".ico"), -size)
        self.found.append((score, href))


def site_icon(url, tmp):
    page = curl(url)
    if not page:
        return None
    final, html = page
    parser = Icons()
    parser.feed(html.decode("utf-8", "replace"))
    for _, href in sorted(parser.found) + [((), "/favicon.ico")]:
        path = os.path.join(tmp, "icon" + os.path.splitext(href.split("?")[0])[1])
        if curl(urljoin(final, href), path) and os.path.getsize(path) > 0:
            return path
    return None


def arrange():
    """Place the icons in their columns. Finder asks once for permission (Automation); without it
    they stay where Finder put them."""
    lines = ['tell application "Finder"']
    for c, column in enumerate(COLUMNS):
        for r, (name, _, _) in enumerate(column):
            lines.append(f'set desktop position of item "{name}.webloc" of desktop to {{{60 + 100 * c}, {70 + 100 * r}}}')
    lines.append("end tell")
    args = ["osascript"]
    for line in ["with timeout of 30 seconds", *lines, "end timeout"]:
        args += ["-e", line]
    r = subprocess.run(args, capture_output=True, text=True)
    print("arranged on the left" if r.returncode == 0 else
          f"not arranged ({r.stderr.strip()}): allow Terminal/Claude to control Finder and run again")


def main():
    tmp = tempfile.mkdtemp()
    for name, url, app in (s for column in COLUMNS for s in column):
        path = os.path.join(DEST, f"{name}.webloc")
        with open(path, "wb") as f:
            plistlib.dump({"URL": url}, f)
        icon = os.path.join(ROOT, app, "app/static/icon.png") if app else site_icon(url, tmp)
        result = "no icon"
        if icon and os.path.exists(icon):
            result = subprocess.run(["osascript", "-l", "JavaScript", "-e", SET_ICON_JS, icon, path],
                                    capture_output=True, text=True).stdout.strip() or "failed"
        print(f"{name:14} {url:43} icon: {result}")
    if DEST == os.path.expanduser("~/Desktop"):
        arrange()
    try:
        socket.gethostbyname(HOST)
    except OSError:
        print(f"\n{HOST} doesn't resolve on this Mac (no MagicDNS here), so the tailnet links won't open.")
        print(f"Add it once:  echo '{own_ip() or '<tailscale ip -4>'} {HOST}' | sudo tee -a /etc/hosts")


if __name__ == "__main__":
    main()

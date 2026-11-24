"""The one way Vigía reads the outside world: a plain HTTP GET with a browser's User-Agent, 20 s,
2 MB at most, up to 5 redirects. No JavaScript: a page that is built by it comes back empty, and
the caller says so.

The fetcher never reaches inside. The other apps of the house trust a request that arrives without
proxy headers (it means "from this Mac"), so a link pointing at the Mac, the LAN or the tailnet
would be a way in. Before every request and every redirect hop, check() resolves the host and
refuses anything that isn't a public address; the connection then goes to that same checked address
(the name only travels in Host and in TLS' SNI), so a DNS answer that changes between the check and
the connect (DNS rebinding) can't slip through either."""
import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT = 20.0
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9,en;q=0.6",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Names that mean "inside" whatever they resolve to (the tailnet's MagicDNS, the LAN's mDNS...).
BLOCKED_NAMES = {"localhost", "host.docker.internal", "gateway.docker.internal", "ts.net"}
BLOCKED_SUFFIXES = (".localhost", ".ts.net", ".lan", ".local", ".internal", ".home.arpa")
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's addresses
NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Tests swap these: the resolver (no DNS) and the transport (no network).
transport: httpx.BaseTransport | None = None


class Refused(Exception):
    """The link points somewhere Vigía must not go. The message is for the person."""


class FetchError(Exception):
    """The page couldn't be read (timeout, 404, too big...). The message is for the person."""


@dataclass
class Page:
    url: str           # where it ended up, after redirects
    status: int
    content_type: str
    body: bytes
    text: str          # body decoded


def resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise FetchError(f"No encuentro {host} (¿está bien escrito?).") from e
    return list(dict.fromkeys(i[4][0] for i in infos))


def bad_address(addr: str) -> bool:
    """True for anything that isn't a public internet address: loopback, private, link-local, CGNAT
    (the tailnet), multicast, unspecified, reserved, and their IPv6 forms (IPv4 written inside IPv6
    is unwrapped and judged as IPv4)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return True
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif ip.teredo:
            return True
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or (ip.version == 4 and ip in CGNAT) or not ip.is_global)


def check(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, the address to connect to), or Refused / FetchError."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise Refused("Ese link está mal formado.") from e
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise Refused("Solo abro links http:// o https://.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise Refused("A ese link le falta el sitio.")
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES) or "." not in host and ":" not in host:
        raise Refused("Ese link es de la red de casa o del tailnet: no lo abro.")
    port = port or (443 if scheme == "https" else 80)
    addrs = resolve(host, port)
    if not addrs:
        raise FetchError(f"No encuentro {host}.")
    if any(bad_address(a) for a in addrs):  # every answer, not just the first: no mixed tricks
        raise Refused("Ese link apunta a una dirección interna (la Mac, la red de casa o el tailnet): no lo abro.")
    return scheme, host, port, addrs[0]


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def host_lock(host: str) -> threading.Lock:
    """At most one request at a time to each site."""
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m[1] if m else None
    if not enc and (m2 := CHARSET.search(body[:4096])):
        enc = m2[1].decode("ascii", "replace")
    try:
        return body.decode(enc or "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _one(url: str, scheme: str, host: str, port: int, addr: str, deadline: float) -> tuple[int, dict, bytes]:
    """One GET to the checked address, with the name in Host and SNI. Doesn't follow redirects."""
    parts = urlsplit(url)
    ip_host = f"[{addr}]" if ":" in addr else addr
    default = port == (443 if scheme == "https" else 80)
    pinned = parts._replace(netloc=f"{ip_host}:{port}", fragment="").geturl()
    headers = {**HEADERS, "Host": host if default else f"{host}:{port}"}
    extensions = {"sni_hostname": host} if scheme == "https" else {}
    with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
        try:
            with client.stream("GET", pinned, headers=headers, extensions=extensions) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    return r.status_code, dict(r.headers), b""
                if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                    raise FetchError("La página es demasiado grande (más de 2 MB).")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError("La página es demasiado grande (más de 2 MB).")
                    if time.monotonic() > deadline:
                        raise FetchError("La página tardó demasiado en responder.")
                    chunks.append(chunk)
                return r.status_code, dict(r.headers), b"".join(chunks)
        except httpx.TimeoutException as e:
            raise FetchError("La página tardó demasiado en responder.") from e
        except httpx.HTTPError as e:
            raise FetchError(f"No pude conectarme con {host}.") from e


def get(url: str) -> Page:
    """The page at url, after checking it and every redirect. Raises Refused or FetchError."""
    deadline = time.monotonic() + TIMEOUT
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, addr = check(url)
        with host_lock(host):
            status, headers, body = _one(url, scheme, host, port, addr, deadline)
        headers = {k.lower(): v for k, v in headers.items()}
        if status in (301, 302, 303, 307, 308):
            if not headers.get("location"):
                raise FetchError("La página redirige a ningún lado.")
            url = urljoin(url, headers["location"])
            continue
        if status >= 400:
            raise FetchError(f"La página respondió con un error ({status}).")
        ctype = headers.get("content-type", "")
        return Page(url, status, ctype, body, decode(body, ctype))
    raise FetchError("La página redirige demasiadas veces.")

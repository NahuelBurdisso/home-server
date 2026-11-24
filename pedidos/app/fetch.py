"""The one way Pedidos reads the outside world: a GET to https://pedix.app/<slug>, and nothing else.

The other apps of the house trust a request that arrives without proxy headers (it means "from this
Mac"), so a fetcher that could be pointed anywhere would be a way in. This one can't: the URL must be
exactly a pedix shop page (checked again on every redirect hop, so a redirect can't take it to
another host), with a browser's User-Agent, 20 s and 3 MB at most."""
import re
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT = 20.0
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SLUG = re.compile(r"[a-z0-9-]+")

# Tests swap this for an httpx.MockTransport (no network).
transport: httpx.BaseTransport | None = None


class FetchError(Exception):
    """The page couldn't be read, or the link isn't a pedix shop. The message is for the person."""


def pedix_url(url: str) -> str | None:
    """The canonical https://pedix.app/<slug> for a pasted link ("pedix.app/verdufresca",
    "https://pedix.app/verdufresca/?utm=x"), or None if it isn't one. Only https, only that host,
    no port, no user, one path segment."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or (parts.hostname or "").lower() != "pedix.app" or port is not None:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    slug = parts.path.strip("/")
    if not SLUG.fullmatch(slug):
        return None
    return f"https://pedix.app/{slug}"


def get(url: str) -> str:
    """The page's HTML. Raises FetchError."""
    for _ in range(MAX_REDIRECTS + 1):
        safe = pedix_url(url)
        if safe is None:  # a redirect off pedix.app (or to a non-shop path) ends here
            raise FetchError("Solo leo páginas de tiendas en pedix.app (https://pedix.app/<tienda>).")
        url = safe
        try:
            with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False, headers=HEADERS) as client:
                with client.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    if r.status_code >= 400:
                        raise FetchError(f"pedix respondió con un error ({r.status_code}).")
                    if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                        raise FetchError("La página es demasiado grande.")
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise FetchError("La página es demasiado grande.")
                        chunks.append(chunk)
                    return b"".join(chunks).decode(r.encoding or "utf-8", "replace")
        except httpx.TimeoutException as e:
            raise FetchError("pedix tardó demasiado en responder.") from e
        except httpx.HTTPError as e:
            raise FetchError("No pude conectarme con pedix.") from e
    raise FetchError("pedix redirige demasiadas veces.")

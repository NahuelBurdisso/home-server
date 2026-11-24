"""Who each movement is for: Nahuel, Sofía, or both of them (the house's rent, the supermarket).

Imported and synced movements belong to the accounts' holder (FINANZAS_TITULAR). What someone
writes down by hand, by Siri or through the bot is theirs: tailscale serve says who is asking
(FINANZAS_PERSONAS maps each login to a person), and the bot says it by name in X-Quien."""
import os
import unicodedata
from urllib.parse import unquote

PEOPLE = {"nahuel": "Nahuel", "sofia": "Sofía", "casa": "Los dos"}
HOLDER = os.environ.get("FINANZAS_TITULAR", "nahuel")


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def logins() -> dict[str, str]:
    """{tailscale login (lowercase): person}, from "login=person,login=person"."""
    out = {}
    for pair in os.environ.get("FINANZAS_PERSONAS", "").split(","):
        login, _, who = pair.partition("=")
        if login.strip() and _fold(who) in PEOPLE:
            out[login.strip().lower()] = _fold(who)
    return out


def from_name(name: str) -> str | None:
    """'Sofi', 'Sofía Herrero' or 'sofia' -> 'sofia': the first word, folded, against each person
    (equal, or one the start of the other, at least 3 letters)."""
    words = _fold(name).split()
    if not words or len(words[0]) < 3:
        return None
    first = words[0]
    for key, label in PEOPLE.items():
        for known in (key, _fold(label)):
            if first == known or known.startswith(first) or first.startswith(known):
                return key
    return None


def of_request(request) -> str:
    """Who wrote this down: the bot's X-Quien, else the Tailscale login (Open WebUI passes on
    its user's, which is the same), else the holder."""
    by_name = from_name(unquote(request.headers.get("x-quien", "")))  # percent-encoded: headers are ASCII
    if by_name:
        return by_name
    login = request.headers.get("tailscale-user-login") or request.headers.get("x-openwebui-user-email") or ""
    return logins().get(login.lower(), HOLDER)


def valid(who: str | None) -> str | None:
    return who if who in PEOPLE else None

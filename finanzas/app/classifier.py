"""Suggests categories for merchants the rules don't know. Swappable backend:

  FINANZAS_CLASSIFIER=ollama  (default) local model on this Mac; private, slower, so every
                              suggestion waits for your OK in "Revisar".
  FINANZAS_CLASSIFIER=jev     TypeSafe's Jev (hosted). Calibrated confidence, so sure answers
                              can apply directly. Needs FINANZAS_JEV_KEY and its API docs.
  FINANZAS_CLASSIFIER=none    rules and what you teach it only.

Only merchant names leave the ledger: never amounts, dates, accounts, or transfers to people
(those are categorized by rules before anything is asked).

Compare backends on your own confirmed categories:  python -m app.classifier eval"""
import json
import os
import sys
import threading
import urllib.request

from . import categories, db

BATCH = 15


class Classifier:
    name = "none"
    calibrated = False   # True when confidence can be trusted to apply answers without review
    auto_accept = 1.01   # confidence at or above which an answer needs no review

    def available(self) -> bool:
        return False

    def classify(self, merchants: list[str]) -> list[tuple[str, float]]:
        """One (category id, confidence 0..1) per merchant, in order."""
        raise NotImplementedError


class Ollama(Classifier):
    name = "IA local (Ollama)"
    CONFIDENCE = {"alta": 0.8, "media": 0.6, "baja": 0.3}

    def __init__(self):
        self.url = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
        self.model = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

    def available(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.url}/api/version", timeout=3)
            return True
        except Exception:
            return False

    def classify(self, merchants):
        cats = categories.CATEGORIES
        listing = "\n".join(f"- {cid}: {categories.label(cid, with_group=True)}" for cid in cats)
        prompt = (
            "Sos un asistente que ordena gastos personales de alguien en Córdoba, Argentina.\n"
            "Para cada descripción de un resumen bancario o de Mercado Pago:\n"
            '1. En "guess" escribí en pocas palabras qué comercio o servicio creés que es.\n'
            '2. En "category" elegí la categoría que mejor encaje.\n'
            '3. En "confidence" decí qué tan seguro estás (alta, media o baja).\n'
            "Usá tu conocimiento de marcas y comercios argentinos. \"Pago con QR <nombre>\" es un pago en un "
            "local llamado <nombre>; \"merpago*<nombre>\" es una compra con Mercado Pago en <nombre>.\n"
            f"Usá {categories.UNKNOWN} solo si la descripción no dice nada del comercio (por ejemplo \"debito directo\").\n\n"
            f"Categorías:\n{listing}\n\n"
            'Ejemplos: "pago con qr farmacity" -> salud.farmacia; "netflix.com" -> digital.streaming; '
            '"macowens" -> compras.ropa.\n\nGastos:\n'
            + "\n".join(f"{i}: {m}" for i, m in enumerate(merchants)))
        schema = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
            "type": "object", "required": ["i", "guess", "category", "confidence"], "properties": {
                "i": {"type": "integer"}, "guess": {"type": "string"},
                "category": {"type": "string", "enum": list(cats)},
                "confidence": {"type": "string", "enum": list(self.CONFIDENCE)}}}}}}
        body = {"model": self.model, "stream": False, "think": False, "format": schema,
                "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(f"{self.url}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            items = json.loads(json.load(resp)["message"]["content"])["items"]
        out = [(categories.UNKNOWN, 0.0)] * len(merchants)
        for item in items:
            if 0 <= item["i"] < len(merchants) and item["category"] in cats:
                out[item["i"]] = (item["category"], self.CONFIDENCE[item["confidence"]])
        return out


class Jev(Classifier):
    """TypeSafe's Jev: typed choices with calibrated probabilities. The request itself is
    still to be written: the API is in early access and its docs aren't public yet. Once
    they are, classify() sends the merchants with CATEGORIES as the allowed choices and
    returns the chosen id with its probability."""
    name = "Jev (TypeSafe)"
    calibrated = True
    auto_accept = 0.9

    def __init__(self):
        self.key = os.environ.get("FINANZAS_JEV_KEY", "")

    def available(self) -> bool:
        return False  # flip to bool(self.key) once classify() is implemented

    def classify(self, merchants):
        raise NotImplementedError("Jev todavía no está conectado: falta la documentación de su API.")


def get() -> Classifier:
    choice = os.environ.get("FINANZAS_CLASSIFIER", "ollama")
    return {"ollama": Ollama, "jev": Jev}.get(choice, Classifier)()


# --- background run -------------------------------------------------------------------

_lock = threading.Lock()


def status(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'ai_status'").fetchone()
    return row[0] if row else ""


def _set_status(conn, text: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ai_status', ?)", (text,))
    conn.commit()


def suggest_pending(classifier: Classifier | None = None) -> None:
    """Asks the classifier about every merchant that still has no category. Meant to run in
    the background after an import; a second call while one is running does nothing."""
    if not _lock.acquire(blocking=False):
        return
    conn = db.connect()
    try:
        clf = classifier or get()
        rows = conn.execute("SELECT DISTINCT merchant FROM transactions"
                            " WHERE kind = 'expense' AND category IS NULL AND merchant IS NOT NULL").fetchall()
        merchants = [r[0] for r in rows]
        if not merchants:
            _set_status(conn, "")
            return
        if not clf.available():
            _set_status(conn, f"error:{clf.name} no está disponible.")
            return
        for start in range(0, len(merchants), BATCH):
            _set_status(conn, f"running:{start}/{len(merchants)}")
            batch = merchants[start:start + BATCH]
            for merchant, (category, conf) in zip(batch, clf.classify(batch)):
                source = "ai_sure" if clf.calibrated and conf >= clf.auto_accept else "ai"
                conn.execute("UPDATE transactions SET category = ?, category_source = ?, category_conf = ?"
                             " WHERE merchant = ? AND kind = 'expense' AND category IS NULL",
                             (category, source, conf, merchant))
            conn.commit()
        _set_status(conn, "")
    except Exception as e:
        _set_status(conn, f"error:{clf.name if 'clf' in locals() else 'La IA'} falló ({type(e).__name__}).")
    finally:
        conn.close()
        _lock.release()


def evaluate(clf: Classifier, conn) -> dict:
    """How often the classifier agrees with categories you set or confirmed."""
    truth = conn.execute(
        "SELECT merchant, category FROM rules UNION"
        " SELECT merchant, category FROM transactions WHERE category_source = 'user'").fetchall()
    merchants = [r[0] for r in truth]
    guesses = []
    for start in range(0, len(merchants), BATCH):
        guesses += clf.classify(merchants[start:start + BATCH])
    exact = sum(g[0] == r[1] for g, r in zip(guesses, truth))
    group = sum(g[0].split(".")[0] == r[1].split(".")[0] for g, r in zip(guesses, truth))
    return {"classifier": clf.name, "n": len(truth), "exact": exact, "same_group": group}


if __name__ == "__main__":
    if sys.argv[1:] == ["eval"]:
        print(evaluate(get(), db.connect()))

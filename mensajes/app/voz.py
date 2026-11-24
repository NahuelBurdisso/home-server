"""Voice notes to text, on this Mac: Whisper "small" (faster-whisper), the same model Open WebUI
already downloaded for the Asistente (its cache is mounted read-only at /whisper). If it isn't there,
it downloads once to /data/whisper. Loaded on the first voice note and kept."""
import os
import tempfile
import threading

WHISPER_DIR = os.environ.get("WHISPER_DIR", "/whisper")
SIZE = os.environ.get("WHISPER_MODEL", "small")
# Words it should expect: amounts and the house's vocabulary come out right more often.
HINT = "Lista de compras, gasté 8.500 pesos en la verdulería, 15 mil, recordame el jueves, manteca, yerba."

_model = None
_lock = threading.Lock()


def _load():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            try:
                _model = WhisperModel(SIZE, device="cpu", compute_type="int8", download_root=WHISPER_DIR,
                                      local_files_only=True)
            except Exception:
                _model = WhisperModel(SIZE, device="cpu", compute_type="int8", download_root="/data/whisper")
    return _model


def transcribe(data: bytes, suffix: str = ".ogg") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        segments, _ = _load().transcribe(f.name, language="es", vad_filter=True, initial_prompt=HINT, beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()

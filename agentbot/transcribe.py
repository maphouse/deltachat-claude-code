import logging
from pathlib import Path

log = logging.getLogger("agentbot.transcribe")

_available = None
NOT_INSTALLED = "__not_installed__"


def _check_available() -> bool:
    global _available
    if _available is None:
        try:
            import faster_whisper  # noqa: F401
            _available = True
        except ImportError:
            log.warning("faster-whisper not installed — voice memos won't be transcribed")
            _available = False
    return _available


def is_available() -> bool:
    return _check_available()


def transcribe(audio_path: Path) -> str | None:
    if not _check_available():
        return NOT_INSTALLED
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio_path), beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        if text:
            log.info("transcribed %.1fs audio → %d chars", info.duration, len(text))
        return text or None
    except Exception:
        log.exception("transcription failed for %s", audio_path)
        return None

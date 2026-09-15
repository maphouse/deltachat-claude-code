import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger("agentbot.tts")

VOICES_DIR = Path(__file__).parent.parent / "voices"
DEFAULT_VOICE = "en_US-lessac-medium"

_available = None
NOT_INSTALLED = "__tts_not_installed__"


def _check_available() -> bool:
    global _available
    if _available is None:
        try:
            from piper import PiperVoice  # noqa: F401
            _available = True
        except ImportError:
            log.warning("piper-tts not installed — /listen won't work")
            _available = False
    return _available


def is_available() -> bool:
    return _check_available()


def _find_or_download_model() -> Path | None:
    VOICES_DIR.mkdir(exist_ok=True)
    model_path = VOICES_DIR / f"{DEFAULT_VOICE}.onnx"
    if model_path.exists():
        return model_path
    try:
        from piper.download_voices import download_voice
        log.info("downloading voice model %s …", DEFAULT_VOICE)
        download_voice(DEFAULT_VOICE, VOICES_DIR)
        if model_path.exists():
            return model_path
        return None
    except Exception:
        log.exception("voice model download failed")
        return None


def _clean_for_speech(text: str) -> str:
    text = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = text.replace("*", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def synthesize(text: str) -> Path | None:
    if not _check_available():
        return None
    text = _clean_for_speech(text)
    if not text:
        return None
    try:
        import wave
        import numpy as np
        from piper import PiperVoice

        model_path = _find_or_download_model()
        if not model_path:
            return None

        voice = PiperVoice.load(str(model_path))
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        out = Path(tmp)

        with wave.open(str(out), "wb") as wav_file:
            first = True
            for chunk in voice.synthesize(text):
                if first:
                    wav_file.setnchannels(chunk.sample_channels)
                    wav_file.setsampwidth(chunk.sample_width)
                    wav_file.setframerate(chunk.sample_rate)
                    first = False
                audio_int16 = (chunk.audio_float_array * 32767).astype(np.int16)
                wav_file.writeframes(audio_int16.tobytes())

        log.info("synthesized %d chars → %.1f KB", len(text), out.stat().st_size / 1024)
        return out
    except Exception:
        log.exception("TTS synthesis failed")
        return None

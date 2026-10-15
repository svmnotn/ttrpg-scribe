"""GPU transcription wrapper around faster-whisper.

Runs fully locally. The 5070 Ti (16 GB) comfortably handles
``large-v3-turbo`` in float16 with plenty of headroom for voice chat + browser.
Audio is transcribed from RAM buffers and never saved to disk.

English-only by design: the language is locked to English, which is faster
and more accurate than auto-detect when the language is known up front.
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np


def _wire_cuda_dlls() -> None:
    """Make pip-installed CUDA 12 libs loadable by ctranslate2 (Windows).

    ctranslate2 resolves ``cublas64_12.dll``/``cudart64_12.dll`` through the
    OS loader, which does not look inside ``site-packages/nvidia/*/bin``.
    Register those dirs before faster-whisper is imported. Without this,
    model load succeeds but the first GPU encode dies with
    ``RuntimeError: Library cublas64_12.dll is not found``.
    """
    import os

    if os.name != "nt":
        return
    import glob
    import site

    dirs: set[str] = set()
    try:
        site_dirs = list(site.getsitepackages()) + [site.getusersitepackages()]
    except AttributeError:
        site_dirs = []
    for sp in site_dirs:
        dirs.update(glob.glob(os.path.join(sp, "nvidia", "*", "bin")))
    for d in sorted(dirs):
        try:
            os.add_dll_directory(d)
        except (OSError, FileNotFoundError):
            continue
    if dirs:
        os.environ["PATH"] = os.pathsep.join(sorted(dirs)) + os.pathsep + os.environ.get("PATH", "")


def _prompt_worthy(text: str) -> bool:
    """Whether decoded text is safe to chain as Whisper's next prompt.

    Rejects hallucination signatures (a phrase looping 3+ times, e.g.
    "the hangle the hangle the hangle") so one bad utterance cannot poison
    the rest of the session. Everything else passes — even mediocre lines
    help continuity, and short interjections are harmless as prompts.
    """
    words = [w.strip(".,!?;:\"'()").lower() for w in text.split()]
    words = [w for w in words if w]
    if len(words) < 6:
        return True
    for n in (2, 3):
        counts: dict[tuple[str, ...], int] = {}
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            counts[gram] = counts.get(gram, 0) + 1
            if counts[gram] >= 3:
                return False
    return True


class StreamingTranscriber:
    model: Any  # faster-whisper ships no types; the WhisperModel instance
    language: str
    _lock: threading.Lock  # ctranslate2: one GPU inference at a time

    def __init__(
        self,
        model_size: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
    ) -> None:
        _wire_cuda_dlls()
        from faster_whisper import WhisperModel

        print(f"[asr] loading faster-whisper {model_size} on {device}/{compute_type}…", flush=True)
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language = language
        self._lock = threading.Lock()
        # Real warm-up: consume the generator so a genuine GPU encode runs
        # here. (list() on the returned tuple would NOT iterate segments and
        # would hide a broken CUDA setup until the first live utterance.)
        silence = np.zeros(16000, dtype=np.float32)
        segments, _info = self.model.transcribe(silence, language=language, beam_size=1)
        for _ in segments:
            pass
        print("[asr] model ready (GPU encode verified)", flush=True)

    def transcribe(self, audio_16k: np.ndarray, prompt: str | None = None) -> str:
        """Transcribe one mono float32 16 kHz utterance (0.5–10 s). Returns text.

        ``prompt`` (previous utterance) is passed as Whisper's initial prompt
        for conversational continuity — names and terms carry over.
        """
        if audio_16k.size < 8000:  # < 0.5 s: nothing worth decoding
            return ""
        if float(np.sqrt(np.mean(audio_16k**2))) < 0.005:  # near-silence
            return ""
        with self._lock:  # serialized: concurrent encodes contend and can fail
            segments, _info = self.model.transcribe(
                audio_16k,
                language=self.language,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            texts = [s.text.strip() for s in segments]
        return " ".join(texts).strip()

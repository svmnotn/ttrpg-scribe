"""Loopback capture (no voice-bot setup needed) → VAD utterances.

Captures a speaker/output device via WASAPI loopback plus your mic, mixes
them, and splits speech into 0.5–10 s utterances with WebRTC VAD.
Only PCM in RAM — transcribed, labeled SPEAKER_XX, then freed.

GoXLR: pass --device "Stream Mix" to capture the Stream Mix output instead of
the default speaker. Use --list-devices to see exact device names; matching is
a case-insensitive substring.

Typical delay: utterance length + ~1–2 s GPU decode.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import numpy as np

from ttrpg_scribe.transcribe import _prompt_worthy, _rms, is_silence_echo

if TYPE_CHECKING:
    from ttrpg_scribe.diarizer import SpeakerCluster
    from ttrpg_scribe.transcribe import StreamingTranscriber
    from ttrpg_scribe.writer import NotesWriter

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480


class VadChunker:
    """Greedy VAD segmenter: emits utterances on ~0.8 s trailing silence,
    force-splitting continuous speech at ~10 s so live captions stay fresh.

    Backstop: even audio the VAD never flags as speech is emitted at the
    ~10 s cap when it is clearly not silence (energy gate) — Whisper's own
    VAD decides whether it is transcribable. Without this, distorted or
    noisy-but-loud speech vanishes without a trace. Windows with zero VAD-
    voiced frames need a much louder gate: bare line/comfort noise over a
    quiet call otherwise arrives as a 10 s "utterance" that Whisper
    hallucinates into an echo of the previous line.
    """

    vad: Any  # webrtcvad ships no types
    max_samples: int
    min_energy: float
    # Gate for windows the VAD never voiced at all (silence/comfort noise).
    # Speech telemetry sits ~0.05+ RMS; this keeps loud-but-unvoiced audio
    # (distorted speech) while dropping quiet-room hiss.
    voiceless_min_energy: float
    _buf: bytearray
    _speech: bytearray
    _window: bytearray  # every fed sample since last emission (any voicing)
    _in_speech: bool
    _voiced_in_window: bool
    _silence_frames: int

    def __init__(self, aggressiveness: int = 1, max_seconds: float = 10.0,
                 min_energy: float = 0.008, voiceless_min_energy: float = 0.025) -> None:
        import webrtcvad

        self.vad = webrtcvad.Vad(aggressiveness)
        self.max_samples = int(max_seconds * SAMPLE_RATE)
        self.min_energy = min_energy
        self.voiceless_min_energy = voiceless_min_energy
        self._buf = bytearray()
        self._speech = bytearray()
        self._window = bytearray()
        self._in_speech = False
        self._voiced_in_window = False
        self._silence_frames = 0

    def _clear(self) -> None:
        self._buf = bytearray()
        self._speech = bytearray()
        self._window = bytearray()
        self._in_speech = False
        self._voiced_in_window = False
        self._silence_frames = 0

    @staticmethod
    def _rms(audio: np.ndarray) -> float:
        return float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0

    def feed(self, pcm16_mono: bytes) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        self._buf += pcm16_mono
        self._window += pcm16_mono
        frame_bytes = FRAME_SAMPLES * 2
        while len(self._buf) >= frame_bytes:
            frame = bytes(self._buf[:frame_bytes])
            del self._buf[:frame_bytes]
            voiced = self.vad.is_speech(frame, SAMPLE_RATE)
            if voiced:
                self._speech += frame
                self._in_speech = True
                self._voiced_in_window = True
                self._silence_frames = 0
                if len(self._speech) // 2 >= self.max_samples:  # force-split long monologues
                    out.append(self._to_float(self._speech))
                    self._clear()
            elif self._in_speech:
                self._speech += frame
                self._silence_frames += 1
                if self._silence_frames * FRAME_MS >= 800:
                    audio = self._to_float(self._speech)
                    if audio.size >= SAMPLE_RATE // 2:  # keep >= 0.5 s
                        out.append(audio)
                    self._clear()
        if len(self._window) // 2 >= self.max_samples:
            # Backstop: ~10 s of audio with no VAD emission. Emit it whole if
            # it is clearly not silence; Whisper filters the rest. A window
            # the VAD never voiced at all must be LOUD to qualify — quiet
            # line hiss over a silent call is not an utterance, and feeding
            # it to Whisper with the previous line as prompt hallucinates an
            # echo of that line into the notes.
            gate = self.min_energy if self._voiced_in_window else self.voiceless_min_energy
            audio = self._to_float(self._window)
            if audio.size >= SAMPLE_RATE // 2 and self._rms(audio) >= gate:
                out.append(audio)
            self._clear()
        return out

    @staticmethod
    def _to_float(raw: bytearray) -> np.ndarray:
        return np.frombuffer(bytes(raw), dtype=np.int16).astype(np.float32) / 32768.0

    def flush(self, min_seconds: float = 0.25) -> np.ndarray | None:
        """Emit buffered in-progress speech (trailing words with no trailing
        silence yet, e.g. at shutdown). Falls back to the whole recent
        window when it is clearly not silence. Resets the segmenter."""
        voiced = self._to_float(bytearray(bytes(self._buf) + bytes(self._speech)))
        window = self._to_float(self._window)
        gate = self.min_energy if self._voiced_in_window else self.voiceless_min_energy
        self._clear()
        if voiced.size >= int(min_seconds * SAMPLE_RATE):
            return voiced
        if window.size >= SAMPLE_RATE // 2 and self._rms(window) >= gate:
            return window
        return None


def _match(devices: Iterable[Any], wanted: str, kind: str) -> Any:
    for d in devices:
        if wanted.lower() in d.name.lower():
            return d
    names = "\n".join(f"  {d.name}" for d in devices)
    raise SystemExit(f"No {kind} device matching {wanted!r}.\nAvailable:\n{names}")


def list_devices() -> None:
    import soundcard as sc

    print("--- speakers/outputs (loopback sources) ---")
    for s in sc.all_speakers():
        print(f"  {s.name}")
    print("--- microphones ---")
    for m in sc.all_microphones(include_loopback=False):
        print(f"  {m.name}")


def _emit_utterance(
    writer: NotesWriter,
    cluster: SpeakerCluster,
    transcriber: StreamingTranscriber,
    speaker_fn: Callable[[float, float], str | None] | None,
    utterance: np.ndarray,
    prompt: str | None,
    end: float | None = None,
) -> str:
    """Attribute (server name or diarized label), transcribe, write to
    console + file. Returns the transcribed text (for prompt chaining).

    ``end`` is the monotonic timestamp when this utterance finished being
    captured. It must be taken at capture time, BEFORE the (slow) GPU
    transcribe call — otherwise a transcription backlog shifts the
    timeline lookup window past the speaking event and every line falls
    back to SPEAKER_XX.
    """
    import time
    label: str | None = None
    if speaker_fn is not None:
        try:
            label = speaker_fn(end if end is not None else time.monotonic(),
                               float(utterance.size) / SAMPLE_RATE)
        except Exception:
            label = None
    if label is None:
        label = cluster.identify(utterance)
    text = transcriber.transcribe(utterance, prompt=prompt)
    if text and prompt and is_silence_echo(text, prompt, _rms(utterance)):
        return ""  # quiet-period echo of the previous line, not new speech
    writer.write(label, text)
    return text


def run_loopback(
    writer: NotesWriter,
    transcriber: StreamingTranscriber,
    cluster: SpeakerCluster,
    device: str | None = None,
    mic: str | None = None,
    input_dev: str | None = None,
    speaker_fn: Callable[[float, float], str | None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Blocking capture loop. Ctrl+C (or ``stop``) ends it.

    ``speaker_fn(end, duration)`` maps an utterance ending at monotonic
    time ``end`` to a server display name (hybrid mode); without it every
    line gets a diarized SPEAKER_XX label.
    """
    import time
    import warnings

    import soundcard as sc

    # Buffer-overrun notices when GPU inference blocks between 1 s reads.
    # The audio itself transcribes fine; silence the spam.
    warnings.filterwarnings("ignore", message="data discontinuity in recording")

    chunker = VadChunker()
    last_text = ""
    prompt = lambda: (last_text[-224:] or None)
    if input_dev:
        # Direct input capture (e.g. GoXLR "Broadcast Stream Mix", which already
        # contains the full mix). No loopback, no mic mixing — no doubles.
        source = _match(sc.all_microphones(include_loopback=True), input_dev, "input")
        print(f"[cap] direct input from '{source.name}'", flush=True)
        with source.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
            print("[cap] listening… Ctrl+C to stop", flush=True)
            try:
                while stop is None or not stop.is_set():
                    frames = rec.record(numframes=SAMPLE_RATE)
                    batch_end = time.monotonic()
                    pcm16 = (np.clip(frames[:, 0], -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    for utterance in chunker.feed(pcm16):
                        text = _emit_utterance(writer, cluster, transcriber, speaker_fn,
                                               utterance, prompt(), batch_end)
                        if text and _prompt_worthy(text):
                            last_text = text
            finally:
                tail = chunker.flush()  # don't drop trailing words on stop
                if tail is not None:
                    _emit_utterance(writer, cluster, transcriber, speaker_fn,
                                    tail, prompt(), time.monotonic())
        return
    speaker = _match(sc.all_speakers(), device, "speaker/output") if device else sc.default_speaker()
    microphone = _match(sc.all_microphones(include_loopback=False), mic, "microphone") if mic else sc.default_microphone()
    print(f"[cap] loopback from '{speaker.name}' + mic '{microphone.name}'", flush=True)
    chunker = VadChunker()
    # 1 s blocking reads; soundcard returns float64 in [-1, 1]
    with speaker.recorder(samplerate=SAMPLE_RATE, channels=1, include_loopback=True) as spk, microphone.recorder(
        samplerate=SAMPLE_RATE, channels=1
    ) as m:
        print("[cap] listening… Ctrl+C to stop", flush=True)
        try:
            while stop is None or not stop.is_set():
                heard = spk.record(numframes=SAMPLE_RATE)
                said = m.record(numframes=SAMPLE_RATE)
                batch_end = time.monotonic()
                mixed = np.clip(heard[:, 0] + said[:, 0], -1.0, 1.0)
                pcm16 = (mixed * 32767).astype(np.int16).tobytes()
                for utterance in chunker.feed(pcm16):
                    text = _emit_utterance(writer, cluster, transcriber, speaker_fn,
                                           utterance, prompt(), batch_end)
                    if text and _prompt_worthy(text):
                        last_text = text
        finally:
            tail = chunker.flush()  # don't drop trailing words on stop
            if tail is not None:
                _emit_utterance(writer, cluster, transcriber, speaker_fn,
                                tail, prompt(), time.monotonic())

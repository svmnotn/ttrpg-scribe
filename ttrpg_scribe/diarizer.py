"""Streaming speaker labels without any paid service or login.

Each VAD utterance is assumed to be (mostly) one voice — true for call audio
with light overlap. We embed it with SpeechBrain ECAPA (public model, runs on
your 5070 Ti) and greedily cluster embeddings by cosine similarity, yielding
stable SPEAKER_00, SPEAKER_01, … labels within a session.

If the embedding model can't load, everything falls back to SPEAKER_00 so
transcription still works.
"""
from __future__ import annotations

from typing import Any

import numpy as np


class SpeakerCluster:
    threshold: float
    _encoder: Any | None  # SpeechBrain EncoderClassifier (ships no types)
    _centroids: list[np.ndarray]
    _counts: list[int]

    def __init__(self, threshold: float = 0.35) -> None:
        self.threshold = threshold
        self._encoder = None
        self._centroids = []
        self._counts = []
        try:
            from speechbrain.inference import EncoderClassifier

            print("[diar] loading speechbrain/spkrec-ecapa-voxceleb…", flush=True)
            self._encoder = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cuda"},
            )
            print("[diar] speaker embeddings ready", flush=True)
        except Exception as e:  # pragma: no cover - hardware dependent
            print(f"[diar] WARNING: embedding model unavailable ({e}); using SPEAKER_00", flush=True)

    def _embed(self, audio_16k: np.ndarray) -> np.ndarray | None:
        if self._encoder is None:
            return None
        import torch

        with torch.no_grad():
            wav = torch.from_numpy(audio_16k).unsqueeze(0).cuda()
            emb = self._encoder.encode_batch(wav).squeeze().float().cpu().numpy()
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 1e-9 else None

    def identify(self, audio_16k: np.ndarray) -> str:
        """Return SPEAKER_XX for an utterance, updating clusters incrementally."""
        emb = self._embed(audio_16k)
        if emb is None:
            return "SPEAKER_00"
        best_idx, best_sim = -1, -2.0
        for i, c in enumerate(self._centroids):
            sim = float(np.dot(emb, c))
            if sim > best_sim:
                best_idx, best_sim = i, sim
        if best_idx >= 0 and best_sim >= self.threshold:
            n = self._counts[best_idx]
            self._centroids[best_idx] = (self._centroids[best_idx] * n + emb) / (n + 1)
            self._counts[best_idx] += 1
            return f"SPEAKER_{best_idx:02d}"
        self._centroids.append(emb)
        self._counts.append(1)
        return f"SPEAKER_{len(self._centroids) - 1:02d}"

"""Stage A: FunASR in-process engine.

Runs in the conda `sensevoice` env (py3.8, funasr 1.1.9, torch cu118).
Pipeline per call:
  fsmn-vad  -> speech intervals (ms)
  SenseVoice (per interval, temp wav slices) -> tagged raw text, tags stripped
  ct-punc   -> restore punctuation
  CAM++     -> per-interval speaker embedding
  greedy 2-means -> local speaker labels (me/other decided later in identity.py)
Qwen3-ASR + ForcedAligner refine text & character timestamps later (qwen_bridge).
"""
from __future__ import annotations
import os, re, tempfile, wave
import numpy as np
from pathlib import Path
from typing import Optional

TAG_RE = re.compile(r"<\|[^|>]*\|>")


def strip_tags(t: str) -> str:
    t = TAG_RE.sub("", t or "")
    return re.sub(r"\s{2,}", " ", t).strip()


def load_pcm16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000:
            raise ValueError(f"need 16k mono wav, got {w.getframerate()}Hz/{w.getnchannels()}ch: {path}")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def slice_ms(pcm: np.ndarray, start_ms: int, end_ms: int) -> np.ndarray:
    a, b = int(start_ms * 16), int(end_ms * 16)
    return pcm[max(0, a):max(a + 1, b)]


def _write_tmp_wav(pcm: np.ndarray, tag: str = "seg") -> str:
    fd, p = tempfile.mkstemp(prefix=f"ca_{tag}_", suffix=".wav")
    os.close(fd)
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    return p


class FunAsrEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg["asr"]
        self._m = {}

    def _get(self, name):
        if name not in self._m:
            from funasr import AutoModel
            key = {"asr": "model", "vad": "vad_model", "punc": "punc_model", "spk": "spk_model"}[name]
            self._m[name] = AutoModel(model=self.cfg[key], device=self.cfg.get("device", "cuda"),
                                      disable_update=True)
        return self._m[name]

    def vad_intervals(self, wav_path: str, min_ms: int = 500, merge_gap_ms: int = 350) -> list:
        vad = self._get("vad")
        res = vad.generate(input=str(wav_path), chunks_overlap=100)
        ivs = []
        for item in res or []:
            for s, e in item.get("value", []) or []:
                s, e = int(s), int(e)
                if e - s < min_ms:
                    continue
                if ivs and s - ivs[-1][1] < merge_gap_ms:
                    ivs[-1][1] = max(ivs[-1][1], e)
                else:
                    ivs.append([s, e])
        return ivs

    def transcribe_intervals(self, pcm: np.ndarray, intervals: list) -> list:
        asr = self._get("asr")
        punc = None
        try:
            punc = self._get("punc")
        except Exception:
            punc = None
        utts = []
        for s, e in intervals:
            tmp = _write_tmp_wav(slice_ms(pcm, s, e), "u")
            try:
                r = asr.generate(input=tmp, language="auto", use_itn=True)
                text = strip_tags(r[0]["text"]) if r and r[0].get("text") else ""
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not text:
                continue
            if punc is not None:
                try:
                    pr = punc.generate(input=text)
                    if pr and pr[0].get("text"):
                        text = strip_tags(pr[0]["text"])
                except Exception:
                    pass
            utts.append({"start_ms": int(s), "end_ms": int(e), "text": text})
        return utts

    def embed_utterance(self, pcm_slice: np.ndarray) -> Optional[np.ndarray]:
        if pcm_slice.size < 16000 * 0.8:  # <0.8s too short for voiceprint
            return None
        spk = self._get("spk")
        tmp = _write_tmp_wav(pcm_slice, "spk")
        try:
            res = spk.generate(input=tmp)
            for item in res or []:
                for k in ("spk_embedding", "embedding"):
                    if isinstance(item, dict) and k in item:
                        v = item[k]
                        if hasattr(v, "detach"):
                            v = v.detach().cpu().numpy()
                        return np.asarray(v, dtype=np.float32).reshape(-1)
        except Exception:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None

    def transcribe_wav(self, wav_path: str) -> list:
        """Full stage-A transcription: returns [{start_ms,end_ms,text}] (no spk yet)."""
        pcm = load_pcm16k(str(wav_path))
        return self.transcribe_intervals(pcm, self.vad_intervals(str(wav_path)))


def cluster_speakers(embs: list, max_spk: int = 2, cut_dist: float = 0.62) -> list:
    """Hierarchical average-linkage clustering on cosine distance.

    Phone-codec audio gives wide within-speaker similarity (0.42~0.9), so
    single-linkage/greedy over-splits; average linkage with cut ~0.62 works
    better. Returns int labels (None embeddings -> -1), relabelled by first
    appearance.
    """
    idx = [i for i, e in enumerate(embs) if e is not None]
    labels = [-1] * len(embs)
    if not idx:
        return labels
    X = np.stack([embs[i] / (np.linalg.norm(embs[i]) + 1e-9) for i in idx])
    if len(idx) == 1:
        labels[idx[0]] = 0
        return labels
    D = 1.0 - X @ X.T
    np.fill_diagonal(D, 0.0)
    D = (D + D.T) / 2
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform
    D = 1.0 - X @ X.T
    np.fill_diagonal(D, 0.0)
    D = (D + D.T) / 2
    Z = linkage(squareform(D, checks=False), method="average")
    if len(idx) < 3:
        lab = np.zeros(len(idx), dtype=int)
    else:
        # two-party phone calls: force the diarization split, then collapse
        # back to one speaker if the two centroids are barely distinguishable
        lab = fcluster(Z, t=max_spk, criterion="maxclust") - 1
        c0 = X[lab == 0].mean(axis=0)
        c1 = X[lab == 1].mean(axis=0)
        sim = float(c0 @ c1) / (np.linalg.norm(c0) * np.linalg.norm(c1) + 1e-9)
        if sim > 0.8:
            lab = np.zeros(len(idx), dtype=int)
    order = {}
    for l in lab:
        if l not in order:
            order[l] = len(order)
    for pos, i in enumerate(idx):
        labels[i] = order[int(lab[pos])]
    return labels

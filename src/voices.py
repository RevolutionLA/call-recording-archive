"""Export per-person voice reference files for Qwen3-TTS voice cloning.

For every contact we collect their clean 'other' utterances across all
calls, rank by length/energy stability, stitch 1~3 reference clips
(6-15 s each) and write:
  data/voices/<contact>/ref_01.wav (24k mono) + ref_01.txt + meta.json
My own voice is exported the same way into data/voices/_me/.
"""
from __future__ import annotations
import json, sqlite3, wave
from pathlib import Path
from . import db


def _read16(path, start_ms, end_ms):
    import numpy as np
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        w.setpos(int(start_ms * sr / 1000))
        raw = w.readframes(max(int((end_ms - start_ms) * sr / 1000), 1600))
    x = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    if w.getnchannels() > 1:
        x = x.reshape(-1, w.getnchannels()).mean(axis=1).astype("float32")
    return x, sr


def _energy(x):
    import numpy as np
    return float((x ** 2).mean() + 1e-9)


def export_all(conn: sqlite3.Connection, cfg: dict,
               min_dur: float = 1.5, max_dur: float = 18.0,
               per_contact_total: float = 40.0):
    out_root = Path(cfg["voices_dir"])
    out_root.mkdir(parents=True, exist_ok=True)
    groups = []
    me = conn.execute("SELECT 1").fetchone() and conn.execute(
        "SELECT 1 FROM me_profile WHERE id=1").fetchone()
    if me:
        groups.append(("me", None, "_me"))
    for c in conn.execute("SELECT id,name FROM contacts ORDER BY n_calls DESC"):
        groups.append(("other", c["id"], c["name"]))

    for who, contact_id, label in groups:
        q = ("SELECT s.start_ms, s.end_ms, s.text_zh, c.wav_path FROM segments s "
             "JOIN calls c ON c.id=s.call_id WHERE s.who=? "
             + ("AND s.contact_id=?" if contact_id else "")
             + " AND c.wav_path IS NOT NULL AND length(s.text_zh) >= 3")
        rows = conn.execute(q, (who, contact_id) if contact_id else (who,)).fetchall()
        cands = []
        for r in rows:
            d = (r["end_ms"] - r["start_ms"]) / 1000.0
            if d < min_dur or d > max_dur:
                continue
            try:
                x, sr = _read16(r["wav_path"], r["start_ms"], r["end_ms"])
            except Exception:
                continue
            e = _energy(x)
            if e < 1e-5:  # silence
                continue
            # clip risk / very quiet: penalize; long+clean preferred
            score = min(d, 12) * (0.15 < e and 1.0 or 0.4)
            cands.append((score, r["wav_path"], r["start_ms"], r["end_ms"], r["text_zh"]))
        cands.sort(reverse=True)
        if not cands:
            print(f"  {label}: 没有合格片段")
            continue
        safe = "".join(ch for ch in label if ch not in '\\/:*?"<>|').strip() or "unnamed"
        out_dir = out_root / safe
        out_dir.mkdir(exist_ok=True)
        meta, used = [], 0.0
        clip_id = 0
        while cands and used < per_contact_total:
            score, wav, s, e, text = cands.pop(0)
            clip_id += 1
            tmp = out_dir / f"ref_{clip_id:02d}.tmp.wav"
            _write_slice(wav, s, e, tmp, target_sr=24000)
            final = out_dir / f"ref_{clip_id:02d}.wav"
            final.write_bytes(tmp.read_bytes()); tmp.unlink()
            (out_dir / f"ref_{clip_id:02d}.txt").write_text(text, encoding="utf-8")
            dur = (e - s) / 1000.0
            used += dur
            meta.append({"file": final.name, "text": text, "duration_sec": round(dur, 2),
                         "source": str(wav), "start_ms": s, "end_ms": e,
                         "model_hint": "qwen3-tts: 用 wav+对应文本作为参考音色 (voice clone)"})
        (out_dir / "meta.json").write_text(json.dumps(
            {"speaker": label, "clips": meta, "total_sec": round(used, 1)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {label}: {len(meta)} 段 / {used:.1f}s -> {out_dir}")


def _write_slice(wav_path, s_ms, e_ms, out, target_sr=24000):
    from . import audio
    audio.slice_wav(wav_path, out, s_ms, e_ms, target_sr=target_sr)

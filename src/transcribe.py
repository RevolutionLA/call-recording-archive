"""Stage A runner: normalize -> VAD+SenseVoice -> CAM++ embed -> 2-spk cluster.

Resumable: per-call status in `calls.status`, one call failing never stops
the batch. Long runs are expected (thousands of files): progress is printed
per call and flushed to stdout so a background run can be watched.
"""
from __future__ import annotations
import sqlite3, time, json
import numpy as np
from . import db, audio
from .funasr_engine import FunAsrEngine, load_pcm16k, slice_ms, cluster_speakers


def _log(msg):
    print(msg, flush=True)


def run_pending(conn: sqlite3.Connection, cfg: dict, limit: int = 0, verbose: bool = True):
    asrcfg = cfg["asr"]
    eng = FunAsrEngine(cfg)
    q = "SELECT id,path FROM calls WHERE status IN ('pending','normalized') ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    _log(f"待处理通话: {len(rows)}")
    t0 = time.time()
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        cid = r["id"]
        try:
            dt = process_one(conn, eng, cfg, r["id"], r["path"])
            ok += 1
            if verbose:
                el = time.time() - t0
                _log(f"[{i}/{len(rows)}] call#{cid} ok {dt}s audio, {el:.0f}s elapsed")
        except Exception as e:
            fail += 1
            conn.execute("UPDATE calls SET status='error',error=?,updated_at=? WHERE id=?",
                         (f"{type(e).__name__}: {e}"[:500], db.now(), cid))
            conn.commit()
            _log(f"[{i}/{len(rows)}] call#{cid} FAILED: {e}")
    _log(f"完成 {ok}，失败 {fail}，用时 {time.time()-t0:.0f}s")


def process_one(conn, eng: FunAsrEngine, cfg, cid: int, path: str) -> float:
    work_dir = cfg["work_dir"]
    wav = audio.normalize(path, work_dir)
    dur = audio.probe_duration(path) or 0.0
    conn.execute("UPDATE calls SET wav_path=?,duration_sec=?,status='normalized',updated_at=? WHERE id=?",
                 (str(wav), dur, db.now(), cid))
    conn.commit()

    pcm = load_pcm16k(str(wav))
    intervals = eng.vad_intervals(str(wav))
    utts = eng.transcribe_intervals(pcm, intervals)
    min_len = cfg["asr"].get("min_embed_ms", 1500)
    embs = []
    for u in utts:
        if (u["end_ms"] - u["start_ms"]) >= min_len:
            embs.append(eng.embed_utterance(slice_ms(pcm, u["start_ms"], u["end_ms"])))
        else:
            embs.append(None)
    labels = cluster_speakers(embs, max_spk=cfg["asr"].get("num_speakers", 2),
                              cut_dist=cfg["asr"].get("cluster_cut_dist", 0.62))

    conn.execute("DELETE FROM segments WHERE call_id=?", (cid,))
    for idx, (u, lab, e) in enumerate(zip(utts, labels, embs)):
        conn.execute(
            "INSERT INTO segments(call_id,idx,spk_local,who,start_ms,end_ms,text_zh,embedding) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (cid, idx, str(lab), "unknown", u["start_ms"], u["end_ms"], u["text"],
             np.asarray(e, dtype=np.float32).tobytes() if e is not None else None))
    conn.execute("UPDATE calls SET status='transcribed',updated_at=? WHERE id=?", (db.now(), cid))
    conn.commit()
    return dur

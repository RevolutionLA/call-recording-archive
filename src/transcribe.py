"""Stage A runner: normalize -> VAD+SenseVoice -> CAM++ embed -> 2-spk cluster.

Resumable: per-call status in `calls.status`, one call failing never stops
the batch. Long runs are expected (thousands of files): progress is printed
per call and flushed to stdout so a background run can be watched.
"""
from __future__ import annotations
import sqlite3, time, json, gc
import numpy as np
from . import db, audio
from .funasr_engine import FunAsrEngine, load_pcm16k, slice_ms, cluster_speakers


def _log(msg):
    print(msg, flush=True)


def _is_mem_error(msg: str) -> bool:
    return any(k in msg for k in ("1455", "MemoryError", "out of memory", "enforce fail"))


def _process_rows(conn, eng, cfg, rows, t0, verbose=True):
    """Process rows; if memory failures come back-to-back the machine is
    exhausted — un-mark those calls and exit 75 so the keepalive retries later
    instead of burning the whole queue into 'error'."""
    ok = fail = 0
    mem_streak = 0
    mem_failed_ids = []
    for i, r in enumerate(rows, 1):
        cid = r["id"]
        try:
            dt = process_one(conn, eng, cfg, cid, r["path"])
            ok += 1
            mem_streak = 0
            if verbose:
                el = time.time() - t0
                _log(f"[{i}/{len(rows)}] call#{cid} ok {dt}s audio, {el:.0f}s elapsed")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            fail += 1
            conn.execute("UPDATE calls SET status='error',error=?,updated_at=? WHERE id=?",
                         (msg[:500], db.now(), cid))
            conn.commit()
            _log(f"[{i}/{len(rows)}] call#{cid} FAILED: {e}")
            if _is_mem_error(msg):
                mem_streak += 1
                mem_failed_ids.append(cid)
                if mem_streak >= 6:
                    conn.execute(
                        f"UPDATE calls SET status='pending',error=NULL WHERE id IN "
                        f"({','.join('?' * len(mem_failed_ids))})", mem_failed_ids)
                    conn.commit()
                    _log(f"内存连续失败 {mem_streak} 通，已退回 pending；退出等待内存释放 (exit 75)")
                    raise SystemExit(75)
            else:
                mem_streak = 0
    return ok, fail


def run_pending(conn: sqlite3.Connection, cfg: dict, limit: int = 0, verbose: bool = True):
    eng = FunAsrEngine(cfg)
    q = "SELECT id,path FROM calls WHERE status IN ('pending','normalized') ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    _log(f"待处理通话: {len(rows)}")
    t0 = time.time()
    ok, fail = _process_rows(conn, eng, cfg, rows, t0, verbose)
    _log(f"完成 {ok}，失败 {fail}，用时 {time.time()-t0:.0f}s")


def run_ids(conn: sqlite3.Connection, cfg: dict, ids: list):
    """Worker mode: process an explicit list of call ids (spawned by run_parallel)."""
    rows = conn.execute(
        f"SELECT id,path FROM calls WHERE id IN ({','.join('?' * len(ids))}) "
        "AND status IN ('pending','normalized')", ids).fetchall()
    eng = FunAsrEngine(cfg)
    t0 = time.time()
    _log(f"worker {len(ids)} ids -> {len(rows)} to process")
    ok, fail = _process_rows(conn, eng, cfg, rows, t0)
    _log(f"worker done {ok}, failed {fail}, {time.time()-t0:.0f}s")


def pending_ids(conn, limit: int = 0) -> list:
    q = "SELECT id FROM calls WHERE status IN ('pending','normalized') ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r["id"] for r in conn.execute(q).fetchall()]


def run_parallel(cfg: dict, workers: int, limit: int = 0):
    """Parent: split pending calls round-robin into per-worker job files and
    spawn `workers` subprocesses (each loads its own model copy on GPU)."""
    import subprocess, sys, os
    from pathlib import Path
    conn = sqlite3.connect(cfg["db_path"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    ids = pending_ids(conn, limit)
    if not ids:
        _log("无待处理通话")
        return
    workers = max(1, min(workers, len(ids)))
    chunks = [ids[i::workers] for i in range(workers)]
    work_dir = Path(cfg["work_dir"]); work_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = Path("logs"); logs_dir.mkdir(exist_ok=True)
    procs = []
    t0 = time.time()
    for wi, ch in enumerate(chunks):
        jf = work_dir / f"run_job_{wi}.json"
        jf.write_text(json.dumps(ch), encoding="utf-8")
        logf = open(logs_dir / f"worker_{wi}.log", "w", encoding="utf-8")
        p = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "pipeline.py"),
             "run", "--jobs", str(jf)],
            stdout=logf, stderr=subprocess.STDOUT, cwd=str(Path(__file__).resolve().parent.parent))
        procs.append((p, logf))
        _log(f"worker#{wi} pid={p.pid} calls={len(ch)} log=logs/worker_{wi}.log")
    rc = 0
    for p, logf in procs:
        rc |= p.wait()
        logf.close()
    _log(f"全部 worker 结束，用时 {time.time()-t0:.0f}s，exit={rc}")
    st = conn.execute("SELECT status, COUNT(*) FROM calls GROUP BY status").fetchall()
    _log("状态分布: " + str({r[0]: r[1] for r in st}))


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
    del pcm, embs, utts, labels
    gc.collect()
    return dur

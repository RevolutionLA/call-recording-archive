"""Bridge: drive src/worker_qwen.py (Qwen3-ASR + ForcedAligner) as subprocess.

The main pipeline runs in the `sensevoice` env (py3.8) which cannot import
qwen_asr; the worker runs in the `vllm` env (py3.10, tf 4.57.6). We pass
per-utterance jobs through JSONL files and fold refined text + character
timestamps back into `segments` (text_zh / words via asr_outputs).
"""
from __future__ import annotations
import json, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
from . import db

HERE = Path(__file__).resolve().parent
MODELS = Path.home() / ".cache/modelscope/hub/Qwen"
ASR_PATH = str(MODELS / "Qwen3-ASR-1___7B")
ALIGNER_PATH = str(MODELS / "Qwen3-ForcedAligner-0___6B")


def worker_python() -> str:
    import os
    cand = [
        r"C:\ProgramData\anaconda3\envs\qwen3_asr\python.exe",
        r"C:\ProgramData\anaconda3\envs\vllm\python.exe",
    ]
    for c in cand:
        if Path(c).exists():
            return c
    raise SystemExit("找不到 qwen_asr conda 环境（请检查 config 或环境名）")


def refine_pending(conn: sqlite3.Connection, cfg: dict, limit: int = 0,
                   batch_calls: int = 40):
    """Refine Stage-A utterances whose text still lacks Qwen punctuation/timestamps."""
    q = ("SELECT id,path FROM calls WHERE status='transcribed' AND id NOT IN "
         "(SELECT DISTINCT call_id FROM asr_outputs WHERE engine='qwen3-asr') ORDER BY id")
    if limit:
        q += f" LIMIT {int(limit)}"
    calls = [dict(r) for r in conn.execute(q).fetchall()]
    print(f"待 Qwen3 精修通话: {len(calls)}")
    if not calls:
        return
    py = worker_python()
    t0 = time.time()
    for i0 in range(0, len(calls), batch_calls):
        chunk = calls[i0:i0 + batch_calls]
        jobs, meta = [], []
        for c in chunk:
            if not c.get("path"):
                continue
            row = conn.execute("SELECT wav_path FROM calls WHERE id=?", (c["id"],)).fetchone()
            if not row or not row["wav_path"]:
                continue
            segs = conn.execute(
                "SELECT id,start_ms,end_ms FROM segments WHERE call_id=? ORDER BY idx",
                (c["id"],)).fetchall()
            for s in segs:
                jobs.append({"call_id": c["id"], "seg_id": s["id"], "wav": row["wav_path"],
                             "start_ms": s["start_ms"], "end_ms": s["end_ms"]})
        if not jobs:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="qwenjob_"))
        jf, of = tmp / "jobs.jsonl", tmp / "out.jsonl"
        jf.write_text("\n".join(json.dumps(j, ensure_ascii=False) for j in jobs), encoding="utf-8")
        log = open(tmp / "worker.log", "wb")
        print(f"批次 {i0//batch_calls+1}: {len(jobs)} 段 -> worker({py})", flush=True)
        r = subprocess.run([py, str(HERE / "worker_qwen.py"), str(jf), str(of),
                            ASR_PATH, ALIGNER_PATH], stdout=log, stderr=subprocess.STDOUT)
        log.close()
        if not of.exists():
            print("  worker 失败，见", tmp / "worker.log")
            tail = (tmp / "worker.log").read_text(encoding="utf-8", errors="replace")[-800:] \
                if (tmp / "worker.log").exists() else ""
            print(tail)
            continue
        n = 0
        for line in of.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("error"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO asr_outputs(call_id,engine,lang,text,words_json,model_path,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (d["call_id"], "qwen3-asr", d.get("lang", ""), d.get("text", ""),
                 json.dumps(d.get("words", []), ensure_ascii=False), ASR_PATH, db.now()))
            if d.get("text"):
                conn.execute("UPDATE segments SET text_zh=? WHERE id=?", (d["text"], d["seg_id"]))
            if d.get("words"):
                ws = d["words"]
                conn.execute(
                    "UPDATE segments SET start_ms=?,end_ms=?,align_source=?,ts_confidence=0.9 "
                    "WHERE id=?", (ws[0]["s"], ws[-1]["e"], "qwen3-aligner", d["seg_id"]))
            n += 1
        conn.commit()
        print(f"  合并 {n} 段，累计 {time.time()-t0:.0f}s", flush=True)

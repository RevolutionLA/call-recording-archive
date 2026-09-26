"""Bridge: drive src/worker_qwen.py (Qwen3-ASR + ForcedAligner) as subprocess.

The main pipeline runs in the `sensevoice` env (py3.8) which cannot import
qwen_asr; the worker runs in the `vllm` env (py3.10, tf 4.57.6). We pass
per-utterance jobs through JSONL files and fold refined text + character
timestamps back into `segments` (text_zh / words via asr_outputs).
"""
from __future__ import annotations
import json, os, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
from . import db

HERE = Path(__file__).resolve().parent
DEFAULT_MODELS = Path.home() / ".cache/modelscope/hub/Qwen"


def _qwen_cfg(cfg: dict) -> dict:
    q = (cfg or {}).get("qwen") or {}
    return {
        "python": q.get("python"),
        "asr": q.get("asr_model_path") or str(DEFAULT_MODELS / "Qwen3-ASR-1___7B"),
        "aligner": q.get("aligner_model_path") or str(DEFAULT_MODELS / "Qwen3-ForcedAligner-0___6B"),
    }


def worker_python(cfg: dict = None) -> str:
    cand = []
    custom = (cfg or {}).get("qwen", {}).get("python") if cfg else None
    if custom:
        cand.append(custom)
    cand += [
        r"C:\ProgramData\anaconda3\envs\qwen3_asr\python.exe",
        r"C:\ProgramData\anaconda3\envs\vllm\python.exe",
    ]
    for c in cand:
        if Path(c).exists():
            return c
    raise SystemExit("找不到 qwen_asr Python 环境（在 config.yaml 的 qwen.python 里指定）")


def free_commit_gb() -> float:
    """GB of currently committable memory (page file included). Windows only;
    returns 999 elsewhere so the guard is a no-op off Windows."""
    import ctypes
    if os.name != "nt":
        return 999.0

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return 0.0
    return st.ullAvailPageFile / 1024 ** 3


def refine_pending(conn: sqlite3.Connection, cfg: dict, limit: int = 0,
                   batch_calls: int = 0):
    """Stage A+: re-listen to each already-segmented utterance with Qwen3-ASR.

    Overwrites segments.text_zh with the better text and re-places
    start/end from the forced aligner, keeping the SenseVoice text in
    text_sv. Results are recorded once per call in asr_outputs, which is also
    the 'already refined' marker, so this is resumable.
    """
    q = (cfg.get("qwen") or {})
    if not q.get("enabled", True):
        print("精修跳过：config.yaml 里 qwen.enabled 未开启")
        return
    # each batch costs one model load (~1min), so keep it coarse
    batch_calls = int(batch_calls or q.get("batch_calls") or 60)
    need = float(q.get("min_avail_gb", 6))
    avail = free_commit_gb()
    if avail < need:
        print(f"精修跳过：可提交内存 {avail:.1f}GB < {need:.0f}GB（下一轮再试）")
        return
    rows = conn.execute(
        "SELECT id, wav_path, contact_hint, phone FROM calls WHERE "
        "status IN ('transcribed','analyzed') AND wav_path IS NOT NULL AND id NOT IN "
        "(SELECT call_id FROM asr_outputs WHERE engine='qwen3-asr') ORDER BY id").fetchall()
    if limit:
        rows = rows[:int(limit)]
    print(f"待 Qwen3 精修通话: {len(rows)}（可用内存 {avail:.1f}GB）")
    if not rows:
        return
    qc = _qwen_cfg(cfg)
    py = worker_python(cfg)
    lang = q.get("language", "Chinese")
    t0 = time.time()
    done = 0
    for i0 in range(0, len(rows), batch_calls):
        chunk = rows[i0:i0 + batch_calls]
        jobs, order = [], []
        for c in chunk:
            segs = conn.execute(
                "SELECT id, start_ms, end_ms FROM segments WHERE call_id=? ORDER BY idx",
                (c["id"],)).fetchall()
            if not segs:
                continue
            ctx = " ".join(x for x in (c["contact_hint"], c["phone"]) if x)[:80]
            order.append(c["id"])
            for s in segs:
                jobs.append({"call_id": c["id"], "seg_id": s["id"], "wav": c["wav_path"],
                             "start_ms": s["start_ms"], "end_ms": s["end_ms"], "context": ctx})
        if not jobs:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="qwenjob_"))
        jf, of = tmp / "jobs.jsonl", tmp / "out.jsonl"
        jf.write_text("\n".join(json.dumps(j, ensure_ascii=False) for j in jobs), encoding="utf-8")
        log = open(tmp / "worker.log", "wb")
        print(f"批次 {i0//batch_calls+1}: {len(chunk)} 通 / {len(jobs)} 段 -> {py}", flush=True)
        r = subprocess.run([py, str(HERE / "worker_qwen.py"), str(jf), str(of),
                            qc["asr"], qc["aligner"], lang],
                           stdout=log, stderr=subprocess.STDOUT)
        log.close()
        if not of.exists():
            tail = ""
            lf = tmp / "worker.log"
            if lf.exists():
                tail = lf.read_text(encoding="utf-8", errors="replace")[-600:]
            print(f"  worker 失败 exit={r.returncode}，见 {lf}\n{tail}", flush=True)
            return
        per_call = {}
        for line in of.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not d.get("error"):
                per_call.setdefault(d["call_id"], []).append(d)
        for cid in order:
            done += _fold_call(conn, cid, per_call.get(cid, []), qc["asr"])
        conn.commit()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)   # keep %TEMP% from filling with job dirs
        print(f"  已精修 {done} 通，累计 {time.time()-t0:.0f}s，"
              f"可用内存 {free_commit_gb():.1f}GB", flush=True)


def _fold_call(conn, cid: int, results: list, model_path: str) -> int:
    """Write one refined pass back: per-segment text/timestamps + asr_outputs marker."""
    if not results:
        return 0
    seg_order = {r["seg_id"]: i for i, r in enumerate(results)}
    words_all, texts = [], []
    for r in sorted(results, key=lambda x: seg_order[x["seg_id"]]):
        if r.get("text"):
            conn.execute("UPDATE segments SET text_sv=COALESCE(text_sv,text_zh), text_zh=? "
                         "WHERE id=?", (r["text"], r["seg_id"]))
            texts.append(r["text"])
        ws = r.get("words") or []
        if ws:
            conn.execute("UPDATE segments SET start_ms=?,end_ms=?,align_source=?,"
                         "ts_confidence=0.9 WHERE id=?",
                         (ws[0]["s"], ws[-1]["e"], "qwen3-aligner", r["seg_id"]))
            words_all += ws
    lang = results[0].get("lang") or ""
    conn.execute(
        "INSERT OR REPLACE INTO asr_outputs(call_id,engine,lang,text,words_json,model_path,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, "qwen3-asr", lang, " ".join(texts), json.dumps(words_all, ensure_ascii=False),
         model_path, db.now()))
    # search text follows the refined transcript; summary itself is left alone
    ft = conn.execute("SELECT GROUP_CONCAT(text_zh,' ') t FROM segments WHERE call_id=?",
                      (cid,)).fetchone()["t"] or ""
    conn.execute("UPDATE calls SET fulltext_zh=?,updated_at=? WHERE id=?", (ft, db.now(), cid))
    return 1

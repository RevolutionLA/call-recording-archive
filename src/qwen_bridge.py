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
        avail = free_commit_gb()
        if avail < need:      # the backlog pass must not fight WSL/Ollama for the page file
            print(f"精修中止：剩余批次待下轮，可提交内存 {avail:.1f}GB < {need:.0f}GB", flush=True)
            return
        # 每轮限时：精修一整批 1900+ 通要十几小时，全占着 GPU 会让摘要饿死，
        # 到点就让出锁，自动循环下一轮继续续跑（asr_outputs 是断点标记）
        cap = float(q.get("max_minutes", 45))
        if time.time() - t0 > cap * 60:
            print(f"精修让位：本轮已到 {cap:.0f} 分钟上限，剩余待下轮", flush=True)
            return
        chunk = rows[i0:i0 + batch_calls]
        jobs, order, want = [], [], {}
        for c in chunk:
            segs = conn.execute(
                "SELECT id, start_ms, end_ms FROM segments WHERE call_id=? ORDER BY idx",
                (c["id"],)).fetchall()
            if not segs:
                continue
            ctx = " ".join(x for x in (c["contact_hint"], c["phone"]) if x)[:80]
            order.append(c["id"])
            want[c["id"]] = len(segs)
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
        # measured ~3s/segment; 10x that is the hang watchdog, so a swapped-out worker
        # cannot freeze the whole keepalive cycle
        budget = 180 + 30 * len(jobs)
        try:
            r = subprocess.run([py, str(HERE / "worker_qwen.py"), str(jf), str(of),
                                qc["asr"], qc["aligner"], lang],
                               stdout=log, stderr=subprocess.STDOUT, timeout=budget)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = f"超时终止(>{budget}s)"
        log.close()
        lf = tmp / "worker.log"
        tail = ""
        if lf.exists():
            tail = lf.read_text(encoding="utf-8", errors="replace")[-1200:]
        if not of.exists():
            print(f"  worker 未产出结果 exit={rc}，日志尾部：\n{tail}", flush=True)
        else:
            per_call, failed = {}, 0
            for line in of.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("error"):
                    failed += 1
                    continue
                # a retried segment wins over the earlier row
                bucket = per_call.setdefault(d["call_id"], {})
                bucket[d["seg_id"]] = d
            skipped = []
            for cid in order:
                got = list(per_call.get(cid, {}).values())
                if len(got) < want[cid]:
                    skipped.append((cid, len(got), want[cid]))
                    continue          # leave it pending: next round re-listens the whole call
                done += _fold_call(conn, cid, got, qc["asr"])
            conn.commit()
            print(f"  本批完成 {done} 通（段失败 {failed}，段数不齐跳过 "
                  f"{len(skipped)}：{skipped[:5]}），exit={rc}", flush=True)
        import shutil
        keep = Path("logs/qwen_worker.log")   # batch-level timings live here, not in %TEMP%
        if lf.exists():
            keep.write_text(f"=== batch {i0//batch_calls+1} calls={len(chunk)} segs={len(jobs)} "
                            f"rc={rc}\n" + tail, encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"  累计精修 {done} 通 / {time.time()-t0:.0f}s，"
              f"可用内存 {free_commit_gb():.1f}GB", flush=True)
        if rc != 0:
            return


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

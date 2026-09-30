"""Click-driven job runner behind the cockpit's 「工作台」 page.

`pipeline.py <stage>` stays the single source of truth: this module only spawns
it as a hidden subprocess, tails its log, reports how many calls are still
waiting for that stage, and can stop it. The point is that nobody has to
remember a command line — 双击启动，页面上点按钮。

Kills go through ``taskkill /T`` so a ``run --workers N`` child tree dies with
the parent; per-call status in the database makes any interrupted stage resume
from where it stopped, which is what makes a 停止 button safe rather than scary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import config, db as dbm

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipeline.py"
LOG_DIR = ROOT / "logs"

# 一键更新 / 整夜挂机按这个顺序走：查重排在转写之前（先把重复备份摘掉才省显卡），
# 专线分人排在归并之后（要先有声纹归属才拆得出坐席）。
# scripts/auto_keepalive.bat 是同一套流程的保守版：它刻意不含 dedup 和 lines，
# 因为第一次查重要由机主自己发起（全自动循环不该半夜改联系人档案）。
CHAIN_STAGES = ["scan", "dedup", "run", "refine", "align", "lines", "summarize", "graph"]
AUTO_SLEEP_SEC = 300


def _n(cond: str, params=()) -> str:
    return "SELECT COUNT(*) FROM " + cond


# ---------- 待处理量：每个数字都直接来自数据库，UI 只负责翻译成人话 ----------
# 三个排队口径都必须和真正干活的那条 SQL 一致（都排除 dup_of 非空的重复备份），
# 否则按钮上写着"50 通待转写"，点下去却一通都不跑。
def _pending_transcribe(conn, cfg):
    return conn.execute(_n("calls WHERE status IN ('pending','normalized')"
                           " AND dup_of IS NULL")).fetchone()[0]


def _pending_refine(conn, cfg):
    from . import qwen_bridge
    return qwen_bridge.pending_count(conn)


def _pending_align(conn, cfg):
    # 这个数必须等于「点下去真的会动的通话」，而旧的口径（没有 me/other 段的通话）
    # 在本机把 263 通报成排队，实际一步都不跑：其中 251 通早就判过「对面听不出是谁」
    # （speaker_map 写死了全是 unknown），另一些根本没有声纹，归并阶段永远动不了它们
    # ——那是转写阶段的活。数字永远归不了零，比显示 0 更糟。
    # 只统计两条真会执行的路径：待做你我标注的（有声纹可判）+ 标成 other 还没归属的。
    return conn.execute(
        "SELECT COUNT(DISTINCT cid) FROM ("
        "  SELECT c.id cid FROM calls c WHERE c.status IN ('transcribed','analyzed')"
        "   AND c.dup_of IS NULL AND c.speaker_map IS NULL"
        "   AND EXISTS (SELECT 1 FROM segments s WHERE s.call_id=c.id"
        "     AND s.embedding IS NOT NULL)"
        "  UNION ALL"
        "  SELECT s.call_id cid FROM segments s JOIN calls c ON c.id=s.call_id"
        "   WHERE s.who='other' AND s.embedding IS NOT NULL"
        "     AND s.contact_id IS NULL AND c.dup_of IS NULL"
        ")").fetchone()[0]


def _pending_dedup(conn, cfg):
    # 还没算过指纹的通话数。指纹只是「文件大小+时长」两个已入库字段的拼接，
    # 不读音频，所以这个数只是告诉你「有多少条要补账」，不代表要干活。
    return conn.execute("SELECT COUNT(*) FROM calls WHERE audio_key IS NULL").fetchone()[0]


def _pending_summarize(conn, cfg):
    return conn.execute(
        _n("calls WHERE status='transcribed' AND summary IS NULL AND dup_of IS NULL")).fetchone()[0]


def _qwen_ready(cfg) -> bool:
    q = (cfg or {}).get("qwen") or {}
    if not q.get("enabled", True):
        return False
    py = q.get("python")
    return bool(py) and Path(py).exists()


STAGES = [
    {
        "key": "scan", "label": "扫描新录音", "icon": "📥",
        "hint": "把录音目录里新增、改名过的文件登记进库（只读文件名和时长，不碰音频、不用显卡）。"
                "新拷进来的录音，第一步点这个。",
        "args": ["scan"],
    },
    {
        "key": "dedup", "label": "查重（同一录音的备份）", "icon": "🧽",
        "hint": "同一通电话被拷到几个文件夹时，只留一份进后续流程，其余标成副本并从排队里摘掉。"
                "先用库里的「大小+时长+转写文本」找候选，再对候选抽读文件头尾各 64KB 核验内容——"
                "不整文件哈希、不删文件，随时可解除（常量码率的录音同大小≠同内容，不核验会误判）。",
        "args": ["dedup"],
        "pending": _pending_dedup,
        "unit": "条待算指纹",
    },
    {
        "key": "run", "label": "转写 + 分出你我", "icon": "🎧",
        "hint": "SenseVoice 逐段听写，再用声纹把每通电话分成「我」和「对方」。"
                "最花时间的一步，可以随时停，下次接着跑。",
        "args": ["run"],
        "pending": _pending_transcribe,
        "unit": "通待转写",
    },
    {
        "key": "refine", "label": "精修文字（更准）", "icon": "✨",
        "hint": "用 Qwen3-ASR 把已切好的语音段重听一遍：中文专名、数字、方言口音明显更准，"
                "原文仍保留可回溯。算力最贵（约 1–2 秒显卡/秒录音），内存不够会自动跳过。",
        "args": ["refine"],
        "pending": _pending_refine,
        "unit": "通待精修",
        "needs": "qwen",
    },
    {
        "key": "align", "label": "归并联系人", "icon": "🧬",
        "hint": "跨通话认出「你本人的声音」，把同一个人不同号码并成一份档案。"
                "只算声纹不重跑音频，几十秒完成。",
        "args": ["align"],
        "pending": _pending_align,
        "unit": "通待归并",
    },
    {
        "key": "lines", "label": "专线分人（一个号码多个人）", "icon": "🎟",
        "hint": "大厂总机、银行、快递这种号码，每次接话的人不同。按对方声纹把同一总机下的不同坐席"
                "拆成子档案，并用他们自报的工号/姓名自动改名；拆错了可在「查重与专线」页并回总机。"
                "只算已有声纹，不重跑音频。",
        "args": ["lines"],
    },
    {
        "key": "summarize", "label": "本机大模型摘要", "icon": "📝",
        "hint": "离线 Ollama / LM Studio 产出摘要、待办、事件、情绪、重要度。"
                "需要 LLM 后端已启动；配了「摘要让位精修」时，精修没跑完这一步会自动跳过。",
        "args": ["summarize"],
        "pending": _pending_summarize,
        "unit": "通待摘要",
    },
    {
        "key": "graph", "label": "更新关系图谱", "icon": "🕸",
        "hint": "重算图谱和事件时间线，几秒钟。转写、摘要跑完后点一下，图谱页就是新的。",
        "args": ["graph"],
    },
    {
        "key": "voices", "label": "导出音色片段", "icon": "🎙",
        "hint": "给每个联系人剪出 6–18 秒干净人声 + 对应文本，直接喂给 Qwen3-TTS 做声音克隆。"
                "结果在 data/voices/，音色库页可试听。",
        "args": ["voices"],
    },
    {
        "key": "refresh", "label": "按新规则回填姓名/号码", "icon": "🔧",
        "hint": "改过 config.yaml 的文件名解析规则后点一下，把历史通话的姓名/号码/时间重新解析一遍。"
                "不重跑音频。",
        "args": ["refresh"],
    },
]

STAGE_BY_KEY = {s["key"]: s for s in STAGES}

_lock = threading.RLock()
_jobs: dict = {}          # key -> job dict（含已结束的上一次结果）
_chain_job: Optional[dict] = None


# ---------- 小工具 ----------
def _python_exe(cfg) -> str:
    py = cfg.get("python_env")
    if py and Path(py).exists():
        return str(py)
    return sys.executable


def _hide_flags() -> int:
    # 不弹任何黑色控制台窗口（Windows）
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # 否则重定向到日志文件时中文会变 GBK 乱码
    env["PYTHONUNBUFFERED"] = "1"          # 实时刷新，网页上才看得到进度
    return env


def _log_path(key: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"ui_{key}.log"


def _write(line: str, f) -> None:
    try:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
        f.flush()
    except OSError:
        pass


def tail(key: str, max_bytes: int = 6000) -> str:
    p = _log_path(key)
    if not p.exists():
        return "（还没有跑过这一步）"
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    txt = data.decode("utf-8", "replace")
    if size > max_bytes:
        txt = txt.split("\n", 1)[-1]        # 丢掉可能被截断的首行
    return txt.rstrip() or "（日志为空）"


def _last_line(key: str) -> str:
    lines = [l for l in tail(key, 2500).splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def _rc_note(rc) -> str:
    if rc is None:
        return ""
    if rc == 0:
        return "已完成"
    if rc == 76:
        return "让位：另一个实例（或挂机循环/守护脚本）正占着这一步"
    if rc in (75,):
        return "内存不足，本轮跳过（稍后会自动续跑）"
    if rc < 0 or rc > 128:
        return f"被终止（rc={rc}），进度已保存，可续跑"
    return f"异常退出（rc={rc}），详见日志"


def _kill_tree(proc) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_hide_flags(), timeout=20)
            return
        except Exception:
            pass
    proc.terminate()


def _wait_with_stop(job) -> int:
    proc = job["proc"]
    while proc.poll() is None:
        if job["stop"].is_set():
            _kill_tree(proc)
            break
        time.sleep(0.4)
    try:
        return proc.wait(timeout=30)
    except Exception:
        return -1


# ---------- 跑一个阶段（阻塞；调用方放在线程里） ----------
def _run_stage(key: str, limit: int = 0, origin: str = "manual") -> Optional[int]:
    cfg = config.load()
    with _lock:
        prev = _jobs.get(key)
        if prev and prev.get("running"):
            return None                      # 同一步只允许一个在跑（跨进程还有 pipeline 自己的锁）
        args = list(STAGE_BY_KEY[key]["args"])
        if limit:
            args += ["--limit", str(int(limit))]
        logf = open(_log_path(key), "a", encoding="utf-8", errors="replace")
        q0 = _queue_size(key, cfg)
        if limit and q0:
            # 「先跑 50 通试试」的分母是 50，不是整个队列，否则进度条永远停在 2.6%
            q0 = min(q0, int(limit))
        job = {"key": key, "running": True, "origin": origin, "started": dbm.now(),
               "finished": "", "rc": None, "stopped": False, "stop": threading.Event(),
               "pid": 0, "logf": logf, "label": STAGE_BY_KEY[key]["label"],
               "t0": time.time(), "t1": None, "queue0": q0, "limit": int(limit or 0)}
        _jobs[key] = job
        _write(f"开始（{origin}）：pipeline.py {' '.join(args)}", logf)
        try:
            proc = subprocess.Popen([_python_exe(cfg), str(PIPELINE)] + args,
                                    cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=_child_env(),
                                    creationflags=_hide_flags())
        except OSError as e:                  # python 路径没了等：别让线程静默消失
            _write(f"启动失败：{e}", logf)
            job.update(running=False, rc=127, finished=dbm.now(), t1=time.time(), proc=None)
            logf.close()
            return 127
        job["proc"] = proc
        job["pid"] = proc.pid

    rc = _wait_with_stop(job)
    with _lock:
        stopped = job["stop"].is_set()
        note = "（已手动停止，进度已保存）" if stopped else _rc_note(rc)
        _write(f"结束 rc={rc}：{note}", logf)
        logf.close()
        job.update(running=False, rc=rc, stopped=stopped, finished=dbm.now(), t1=time.time())
    return rc


def _stage_available(key: str, cfg) -> tuple:
    st = STAGE_BY_KEY[key]
    if st.get("needs") == "qwen" and not _qwen_ready(cfg):
        return False, "本机未启用（config.yaml 的 qwen 关闭或精修环境不存在）"
    if not Path(cfg.get("python_env", "")).exists():
        return False, "config.yaml 里的 python_env 环境不存在"
    return True, ""


# ---------- 进度：分母是开跑前的排队数，分子是已经被这一步做掉的 ----------
def _queue_size(key: str, cfg):
    fn = STAGE_BY_KEY[key].get("pending")
    if not fn:
        return None
    conn = None
    try:
        conn = dbm.connect(cfg["db_path"])
        return fn(conn, cfg)
    except Exception:
        return None                       # 口径查不出来就不画条，别画一条假的
    finally:
        if conn is not None:
            conn.close()


def _progress(job: dict, st: dict, pending_now, now: float) -> dict:
    """跑动中的进度都从「排队数在往下掉」推出来，不依赖子进程配合打印。

    排队数在跑的过程中可能变大（扫描又进了新录音），所以 done 夹在 [0, 总数]，
    且在跑时最多只报 99%——真 100% 意味着这一步结束了，那必须由结束本身来说。
    """
    counted = bool(st.get("pending"))
    t0 = job.get("t0")
    out = {"kind": "counted" if counted else ("blind" if t0 else "none"),
           "elapsed_sec": round((job.get("t1") or now) - t0, 1) if t0 else None}
    q0 = job.get("queue0")
    if counted and q0:
        done = max(0, min(q0, q0 - (pending_now if pending_now is not None else q0)))
        out.update(total=q0, done=done,
                   pct=round(min(99.0, done * 100.0 / q0), 1) if job.get("running") else 100.0)
        el = out["elapsed_sec"]
        if job.get("running") and done and el:
            per = el / done
            out["sec_per_item"] = round(per, 2)
            out["eta_sec"] = round((q0 - done) * per)
    elif counted and pending_now is not None:
        out.update(total=None, done=0, pct=100.0 if pending_now == 0 else 0.0)
    return out


def _step(job: dict, stages: list, at: str, states: dict) -> list:
    """把一键/挂机的 8 步摊成页面上的阶梯：每步只有 待办/在跑/已完成/让位/跳过。

    整夜挂机每轮重画一遍，所以轮次也要报出去，否则页面上永远停在「第 3 步」。
    """
    return [{"key": k, "label": STAGE_BY_KEY[k]["label"], "icon": STAGE_BY_KEY[k]["icon"],
             "state": ("running" if k == at and job.get("running")
                       else states.get(k, "waiting"))} for k in stages]


def _step_state(rc, stopped: bool) -> str:
    if rc is None:
        return "yielded"              # 已有实例在跑，这一步被让开了
    if rc == 0:
        return "done"
    if rc == 76:
        return "yielded"
    if rc == 75:
        return "mem"
    if stopped or rc < 0 or rc > 128:
        return "stopped"
    return "failed"


# ---------- 一键更新 / 整夜挂机 ----------
def _run_chain(job, stages, loop: bool) -> None:
    cfg = config.load()
    states: dict = {}
    at = None
    job["rounds"] = 0
    while not job["stop"].is_set():
        job["rounds"] += 1
        states = {}
        job["steps"] = _step(job, stages, None, states)
        for key in stages:
            if job["stop"].is_set():
                break
            at = key
            job["at"] = at
            job["steps"] = _step(job, stages, at, states)
            ok, why = _stage_available(key, cfg)
            if not ok:
                states[key] = "skipped"
                _write(f"跳过 {STAGE_BY_KEY[key]['label']}：{why}", job["logf"])
                job["steps"] = _step(job, stages, at, states)
                continue
            _write(f"— 阶段：{STAGE_BY_KEY[key]['label']}", job["logf"])
            rc = _run_stage(key, origin=job["key"])
            states[key] = _step_state(rc, job["stop"].is_set())
            _write(f"— 阶段结束：{STAGE_BY_KEY[key]['label']} · {_rc_note(rc) if rc is not None else '已有实例在跑，让开'}",
                   job["logf"])
            job["steps"] = _step(job, stages, at, states)
            if job["stop"].is_set():
                break
        if not loop:
            break
        _write(f"本轮完成，{AUTO_SLEEP_SEC} 秒后自动开始下一轮（可随时停止）", job["logf"])
        if job["stop"].wait(AUTO_SLEEP_SEC):
            break
    with _lock:
        if loop:
            _write("整夜挂机已结束", job["logf"])
        job.update(running=False, finished=dbm.now(), at=None,
                   steps=_step(job, stages, None, states))
        job["logf"].close()


def _start_chain(key: str, loop: bool) -> dict:
    global _chain_job
    with _lock:
        if _chain_job and _chain_job.get("running"):
            raise RuntimeError(f"已有「{_chain_job['label']}」在跑，先停止它")
        # 一键 / 整夜共用一个总日志，否则页面的「跟随运行中」看不到是谁起的头
        logf = open(_log_path("chain"), "a", encoding="utf-8", errors="replace")
        job = {"key": key, "label": "一键更新全部" if not loop else "整夜自动摄取",
               "running": True, "stop": threading.Event(), "started": dbm.now(),
               "finished": "", "rc": None, "logf": logf, "loop": loop,
               "t0": time.time(), "t1": None, "at": None, "rounds": 0}
        job["steps"] = _step(job, CHAIN_STAGES, None, {})
        _chain_job = job
        _write(f"开始：{'整夜循环' if loop else '单轮全跑'} -> {' → '.join(CHAIN_STAGES)}", logf)
    threading.Thread(target=_run_chain, args=(job, CHAIN_STAGES, loop), daemon=True).start()
    return job


# ---------- 对外接口 ----------
def start(key: str, limit: int = 0) -> dict:
    """Start a stage, or the whole chain (key='chain' 单轮 / 'auto' 整夜循环)."""
    if key in ("chain", "auto"):
        _start_chain(key, loop=(key == "auto"))
        return {"ok": True}
    if key not in STAGE_BY_KEY:
        raise KeyError(f"未知任务：{key}")
    with _lock:
        if _jobs.get(key, {}).get("running"):
            raise RuntimeError(f"「{STAGE_BY_KEY[key]['label']}」已经在跑了")
    threading.Thread(target=_run_stage, args=(key, limit, "manual"), daemon=True).start()
    return {"ok": True}


def stop(key: str) -> dict:
    with _lock:
        if key in ("chain", "auto"):
            job = _chain_job
        else:
            job = _jobs.get(key)
        if not job or not job.get("running"):
            raise RuntimeError("这一步现在没在跑")
        job["stop"].set()                      # 循环据此不再开下一阶段
        if job.get("proc") and job["proc"].poll() is None:
            _write("收到停止请求：正在终止当前阶段（进度已保存，可续跑）", job["logf"])
            _kill_tree(job["proc"])
        # 一键/整夜任务本身不持有进程：把当下正在跑的阶段一起停掉
        if key in ("chain", "auto"):
            for jb in _jobs.values():
                if jb.get("running"):
                    jb["stop"].set()
                    if jb.get("proc") and jb["proc"].poll() is None:
                        _kill_tree(jb["proc"])
    return {"ok": True}


def status(conn, cfg) -> dict:
    now = time.time()
    stages = []
    for st in STAGES:
        key = st["key"]
        job = _jobs.get(key) or {}
        pending = None
        if st.get("pending"):
            try:
                pending = st["pending"](conn, cfg)
            except Exception:
                pending = None                 # 统计口径不影响按钮本身
        ok, why = _stage_available(key, cfg)
        rc = job.get("rc")
        note = "" if rc is None else (
            "已手动停止（进度已保存，可续跑）" if job.get("stopped") else _rc_note(rc))
        stages.append({
            "key": key, "label": st["label"], "icon": st["icon"], "hint": st["hint"],
            "pending": pending, "unit": st.get("unit", ""),
            "running": bool(job.get("running")),
            "rc": rc, "note": note,
            "started": job.get("started", ""), "finished": job.get("finished", ""),
            "pid": job.get("pid", 0),
            "progress": _progress(job, st, pending, now),
            "last_line": _last_line(key) if (job or _log_path(key).exists()) else "",
            "available": ok, "unavailable": why,
        })
    chain = _chain_job or {}
    return {
        "stages": stages,
        "chain": {"running": bool(chain.get("running")), "label": chain.get("label", ""),
                  "started": chain.get("started", ""), "finished": chain.get("finished", ""),
                  "loop": bool(chain.get("loop")),
                  "steps": chain.get("steps", []), "at": chain.get("at", ""),
                  "rounds": chain.get("rounds", 0),
                  "elapsed_sec": round((chain.get("t1") or now) - chain["t0"], 1)
                  if chain.get("t0") else None,
                  "last_line": tail("chain", 1200).splitlines()[-1:] or [""]},
        "chain_stages": CHAIN_STAGES,
        "sleep_sec": AUTO_SLEEP_SEC,
        "gpu_note": "本机一张卡：转写/精修/摘要互斥，被挡住的一方会让位（rc=76）而不是崩。",
    }

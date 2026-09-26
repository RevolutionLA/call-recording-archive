"""Call archive pipeline CLI.

Commands (run with the conda `sensevoice` env python):
  python pipeline.py scan              扫描录音目录入库
  python pipeline.py report            查看当前进度/统计
  python pipeline.py run [--limit N]   转写+声纹分离（可断点续跑，可反复执行）
  python pipeline.py refine            用 Qwen3-ASR+ForcedAligner 精修文本与字级时间戳
  python pipeline.py align             自动识别「我」并给所有段落打 me/other/联系人标签
  python pipeline.py enroll-me WAV [start_ms end_ms]  手动注册我的声纹
  python pipeline.py summarize         本地 LLM 摘要与分析
  python pipeline.py voices            导出各联系人音色文件（供千问 TTS 克隆）
  python pipeline.py graph             构建关系图谱/事件时间线
  python pipeline.py web               启动驾驶舱 Web 服务
"""
from __future__ import annotations
import argparse, json, sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import config, db, scan as scan_mod, audio, naming  # noqa: E402


def get_conn(cfg):
    return db.connect(cfg["db_path"])


def cmd_scan(cfg, args):
    conn = get_conn(cfg)
    target = args.path or cfg.get("recordings_dir")
    if not target:
        sys.exit("请先在 config.yaml 填写 recordings_dir，或 scan --path <录音目录>")
    root = str(Path(target).resolve())
    row = conn.execute("SELECT id FROM sources WHERE path=?", (root,)).fetchone()
    if not row:
        conn.execute("INSERT INTO sources(name,path,created_at) VALUES(?,?,?)",
                     (Path(root).name or root, root, db.now()))
        row = conn.execute("SELECT id FROM sources WHERE path=?", (root,)).fetchone()
    conn.execute("UPDATE calls SET source_id=? WHERE source_id IS NULL", (row["id"],))
    conn.commit()
    r = scan_mod.scan(conn, root, cfg["audio_extensions"],
                      cfg.get("filename_patterns"), cfg.get("exclude_dirs"),
                      source_id=row["id"])
    print(f"扫描完成：新增 {r['added']}，更新 {r['updated']}，未变 {r['unchanged']}")
    print("状态分布:", scan_mod.stats(conn))


def cmd_report(cfg, args):
    conn = get_conn(cfg)
    tot = conn.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    print(f"通话总数: {tot}")
    for k, v in sorted(scan_mod.stats(conn).items()):
        print(f"  {k}: {v}")
    dur = conn.execute("SELECT SUM(duration_sec)/3600.0 h FROM calls").fetchone()["h"]
    print(f"  音频总时长: {dur or 0:.1f} 小时")
    n_seg = conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
    n_me = conn.execute("SELECT COUNT(*) c FROM segments WHERE who='me'").fetchone()["c"]
    print(f"  转写段落: {n_seg}（标记为「我」: {n_me}）")
    n_c = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
    n_sum = conn.execute("SELECT COUNT(*) c FROM calls WHERE summary IS NOT NULL").fetchone()["c"]
    print(f"  联系人: {n_c}  摘要: {n_sum}")
    me = conn.execute("SELECT n_samples FROM me_profile WHERE id=1").fetchone()
    print(f"  「我」声纹: {'已注册' if me else '未注册（跑 align 或用 enroll-me）'}")


def cmd_run(cfg, args):
    conn = get_conn(cfg)
    from src import transcribe
    if args.jobs:
        ids = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
        transcribe.run_ids(conn, cfg, ids)
    else:
        workers = args.workers if args.workers else int(cfg["asr"].get("workers", 1))
        if workers > 1:
            transcribe.run_parallel(cfg, workers=workers, limit=args.limit)
        else:
            transcribe.run_pending(conn, cfg, limit=args.limit, verbose=not args.quiet)


def cmd_refine(cfg, args):
    conn = get_conn(cfg)
    from src import qwen_bridge
    qwen_bridge.refine_pending(conn, cfg, limit=args.limit)


def cmd_align(cfg, args):
    conn = get_conn(cfg)
    from src import identity
    ok, msg = identity.bootstrap_me(conn, thr=cfg["asr"].get("me_threshold", 0.55))
    print(msg)
    n = identity.assign_call_labels(conn)
    print(f"标注 me/other：{n} 通电话")
    m = identity.resolve_contacts(conn)
    print(f"联系人归属：{m} 个段落")


def cmd_enroll_me(cfg, args):
    conn = get_conn(cfg)
    from src import funasr_engine
    eng = funasr_engine.FunAsrEngine(cfg)
    wav = args.wav
    ok, msg = identity_set_me(conn, eng, wav, args.start, args.end)
    print(msg)
    if ok:
        n = __import__("src.identity", fromlist=["identity"]).assign_call_labels(conn)
        print(f"重新标注 {n} 通电话")


def identity_set_me(conn, eng, wav, start, end):
    from src import identity
    return identity.set_me_from_wav(conn, eng, wav, start or 0, end or 0)


def cmd_summarize(cfg, args):
    conn = get_conn(cfg)
    from src import summarize
    summarize.run_pending(conn, cfg, limit=args.limit)


def cmd_voices(cfg, args):
    conn = get_conn(cfg)
    from src import voices
    voices.export_all(conn, cfg)


def cmd_graph(cfg, args):
    conn = get_conn(cfg)
    from src import graph
    graph.build(conn, cfg)


def cmd_web(cfg, args):
    import uvicorn
    from src import server
    uvicorn.run(server.app, host=cfg["web"]["host"], port=cfg["web"]["port"])


_LOCKS = []


def _acquire_lock(name: str) -> bool:
    """Per-stage single-instance guard (Windows). Keeps the auto-ingest loop and
    a manual run from double-processing the same backlog."""
    import os
    if os.name != "nt":
        return True
    import msvcrt
    Path("data").mkdir(exist_ok=True)
    f = open(f"data/lock_{name}.lock", "a+")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return False
    _LOCKS.append(f)  # keep handle open for process lifetime
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["scan", "report", "run", "refine", "align",
                                        "enroll-me", "summarize", "voices", "graph", "web"])
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 通（0=全部）")
    ap.add_argument("--workers", type=int, default=0,
                    help="run 用：并行转写进程数（0=config asr.workers，1=串行）")
    ap.add_argument("--jobs", default="", help="run 内部用：worker 任务 id 列表 JSON 文件")
    ap.add_argument("--path", default="", help="scan 用：库名称（默认按 config 的 recordings_dir 建「本机」库）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("wav", nargs="?", help="enroll-me 用：16k 单声道 wav 路径")
    ap.add_argument("start", nargs="?", type=int, default=0)
    ap.add_argument("end", nargs="?", type=int, default=0)
    args = ap.parse_args()

    cfg = config.load()
    if args.command in ("run", "summarize", "align", "graph") and not args.jobs:
        if not _acquire_lock(args.command):
            print(f"[{args.command}] 已有实例在跑（data/lock_{args.command}.lock 被占），本实例跳过")
            return
    fn = {
        "scan": cmd_scan, "report": cmd_report, "run": cmd_run, "refine": cmd_refine,
        "align": cmd_align, "enroll-me": cmd_enroll_me, "summarize": cmd_summarize,
        "voices": cmd_voices, "graph": cmd_graph, "web": cmd_web,
    }[args.command]
    try:
        fn(cfg, args)
    except KeyboardInterrupt:
        print("\n中断：进度已保存，可重新运行续跑。")


if __name__ == "__main__":
    main()

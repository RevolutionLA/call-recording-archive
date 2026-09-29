"""Call archive pipeline CLI.

Commands (run with the conda `sensevoice` env python):
  python pipeline.py scan              扫描录音目录入库
  python pipeline.py dedup             查重：标出同一录音的多份备份（只读库，不读音频）
  python pipeline.py report            查看当前进度/统计
  python pipeline.py run [--limit N]   转写+声纹分离（可断点续跑，可反复执行）
  python pipeline.py refine            用 Qwen3-ASR+ForcedAligner 精修文本与字级时间戳
  python pipeline.py align             自动识别「我」并给所有段落打 me/other/联系人标签
  python pipeline.py lines             专线分人：同一个机构名下按声纹拆出不同坐席
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


def cmd_refresh(cfg, args):
    """文件名解析规则升级后，回填已入库通话的 名字/号码/时间（不改状态、不碰音频）。"""
    conn = get_conn(cfg)
    r = scan_mod.refresh_hints(conn, cfg.get("filename_patterns"))
    print(f"回填完成：更新 {r['changed']} / {r['total']} 通")


def cmd_dedup(cfg, args):
    """判重：先用库里的大小/时长/时间/文本找候选，再对候选抽读文件头尾各 64KB 核验内容。"""
    conn = get_conn(cfg)
    from src import dedup
    r = dedup.mark(conn, cfg)
    run = r["this_run"]
    print(f"查重完成：本轮新标 {run['copies']} 份副本（{run['groups']} 组），"
          f"库里累计 {r['copies']} 份 / {r['groups']} 组，"
          f"省下约 {r['saved_sec'] / 3600:.1f} 小时（{r['saved_bytes'] / 1073741824:.1f} GB）")
    cand = r["candidates"]
    print(f"候选：同指纹 {cand['same_fingerprint']} 组（核验后留下 {r['exact_kept']} 组）、"
          f"近似 {cand['near']} 组；这一轮真正判成副本的候选组 {run['candidate_groups']} 个；"
          f"抽读内容指纹 {r['probed']} 条；专线标记 {r['lines_marked']} 通")
    print("正本规则：已转写完成的优先当正本；副本不再进转写/精修/摘要队列，随时可解除。")


def cmd_lines(cfg, args):
    """专线分人：同一个机构名下按声纹拆出不同坐席并自动起名。"""
    conn = get_conn(cfg)
    from src import dedup, sharedline
    dedup.classify_lines(conn, cfg.get("shared_line_names"))
    r = sharedline.split(conn, cfg)
    note = r.get("note")
    if note:
        print(note)
    print(f"专线分人：拆分 {r['groups']} 条总机 / 新建 {r['seats']} 个坐席档案 / "
          f"重挂 {r['moved']} 通通话（按号码认出专线 {r.get('shared_calls', 0)} 通，"
          f"库里共 {r['orgs']} 个总机、{r['seat_contacts']} 个坐席）")
    for g in r["splits"]:
        who = "、".join(f"{s['name']}（{s['calls']} 通，自比 {s['intra_sim']}）" for s in g["seats"])
        # 「簇间最像的一对」= 这几簇之间相似度最高的那一对。它才是这一刀该不该切的依据：
        # 接近自比说明本来是同一个人被劈成两半，越小（两拨人越不像）切得越放心。
        near = min((s["cross_sim"] for s in g["seats"] if s.get("cross_sim") is not None),
                   default=None)
        print(f"  {g['org']}[{g['line_kind']}] -> {who}"
              + (f" ｜簇间最像的一对 {near}" if near is not None else ""))


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
    m = identity.resolve_contacts(conn, extra_names=cfg.get("shared_line_names"))
    print(f"联系人归属：{m} 个段落")
    mg = identity.merge_same_person(conn, cfg["asr"].get("same_person_merge_sim", 0.8))
    print(f"同人并档（不同号码同一声纹）：合并 {mg} 个联系人")


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
    if cfg["llm"].get("wait_for_refine") and (cfg.get("qwen") or {}).get("enabled", True):
        from src import qwen_bridge
        n = qwen_bridge.pending_count(conn)
        if n:
            print(f"摘要让位精修：还有 {n} 通未精修，跑完再摘要"
                  f"（llm.wait_for_refine=false 可立刻开始）")
            return
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
    import os
    import uvicorn
    from src import server
    # Windows 的 Hyper-V/WSL 会随机吞掉一整段端口（本机实测 8710-8809 被保留，
    # 8760 直接 bind 失败 10013）。启动器挑到空闲端口时用这两个变量传进来，
    # 没有就用 config 里的默认值。
    host = os.environ.get("CALLREC_HOST") or cfg["web"]["host"]
    port = int(os.environ.get("CALLREC_PORT") or cfg["web"]["port"])
    print(f"驾驶舱： http://{host}:{port}", flush=True)
    uvicorn.run(server.app, host=host, port=port)


_LOCKS = []


def _acquire_lock(name: str) -> bool:
    """Per-stage single-instance guard. Keeps the auto-ingest loop and a manual
    run from double-processing the same backlog. msvcrt on Windows, flock on
    Linux/macOS -- otherwise an open-source user running auto_keepalive next to
    a manual `run` gets no protection at all and both chew through the backlog."""
    import os
    Path("data").mkdir(exist_ok=True)
    f = open(f"data/lock_{name}.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCKS.append(f)  # keep handle open for process lifetime, that is what holds the lock
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["scan", "refresh", "report", "run", "refine", "align",
                                        "dedup", "lines",
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
    # rc=76：被另一个实例的锁挡住。守护脚本据此拉长重试间隔，而不是把它当成
    # 「正常跑完」在 5 秒后原地空转
    if args.command in ("run", "refine", "summarize", "align", "graph", "dedup", "lines") and not args.jobs:
        if not _acquire_lock(args.command):
            print(f"[{args.command}] 已有实例在跑（data/lock_{args.command}.lock 被占），本实例跳过")
            raise SystemExit(76)
    # 单卡 6GB：转写、Qwen3 精修、Ollama 摘要任何一个都会把显存吃满，
    # 同跑不会报错只会一起变慢（实测精修被挤到 20 分钟零输出），所以三者互斥
    if args.command in ("run", "refine", "summarize") and not args.jobs and not _acquire_lock("gpu"):
        print(f"[{args.command}] GPU 正被 run/refine/summarize 中的另一阶段占用，本实例跳过")
        raise SystemExit(76)
    fn = {
        "scan": cmd_scan, "refresh": cmd_refresh, "report": cmd_report, "run": cmd_run,
        "refine": cmd_refine, "align": cmd_align, "enroll-me": cmd_enroll_me,
        "dedup": cmd_dedup, "lines": cmd_lines,
        "summarize": cmd_summarize, "voices": cmd_voices, "graph": cmd_graph, "web": cmd_web,
    }[args.command]
    try:
        fn(cfg, args)
    except KeyboardInterrupt:
        print("\n中断：进度已保存，可重新运行续跑。")


if __name__ == "__main__":
    main()

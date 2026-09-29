"""Contract + behaviour test for the 工作台 job runner (src/jobs.py).

Why this exists: the web page offers one button per pipeline stage, and each
button's meaning comes from a table in jobs.py. If somebody renames a CLI
subcommand, edits CHAIN_STAGES, or drops a label, the page silently grows a
button that does nothing. This test catches that without a GPU, without models
and without writing to the real archive:

  * every stage maps to a subcommand that pipeline.py actually accepts
  * the 一键/整夜 chain only contains real stages, in the documented order
  * every stage has the copy the UI renders (label / icon / 大白话 hint)
  * the runner really spawns, really tails UTF-8 logs, and really reports rc
    (only when a local config.yaml + database exist; skipped on CI)

Run: python scripts/test_jobs.py
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


def cli_subcommands():
    """Read the choices= list straight out of pipeline.py (no import: it needs yaml)."""
    src = (ROOT / "pipeline.py").read_text(encoding="utf-8")
    m = re.search(r'add_argument\(\s*"command"\s*,\s*choices=\[(.*?)\]', src, re.S)
    if not m:
        raise AssertionError("pipeline.py 里找不到 command 的 choices 列表，测试该跟着改了")
    return set(re.findall(r'"([a-z-]+)"', m.group(1)))


def test_contract():
    from src import jobs

    cmds = cli_subcommands()
    check("pipeline.py 有子命令清单", len(cmds) >= 8, ",".join(sorted(cmds)))

    for st in jobs.STAGES:
        key = st["key"]
        check(f"{key}: 子命令真实存在", st["args"] and st["args"][0] in cmds, st["args"])
        check(f"{key}: 有按钮文案", bool(st.get("label")) and bool(st.get("icon")), st.get("label"))
        hint = st.get("hint", "")
        check(f"{key}: 说明写成人话（>=20 字）", len(hint) >= 20, hint[:24])
        pend = st.get("pending")
        check(f"{key}: 待处理数要么没有要么是函数", pend is None or callable(pend))

    keys = set(jobs.STAGE_BY_KEY)
    check("链里每一步都存在", all(k in keys for k in jobs.CHAIN_STAGES), "->".join(jobs.CHAIN_STAGES))
    # 查重排在转写之前（先摘掉重复备份才省钱），专线分人排在归并之后（要先有声纹归属）
    check("链顺序 = 扫描→查重→转写→精修→归并→专线→摘要→图谱",
          jobs.CHAIN_STAGES == ["scan", "dedup", "run", "refine", "align",
                                "lines", "summarize", "graph"],
          "->".join(jobs.CHAIN_STAGES))
    check("转写/精修/摘要都算得出待处理数",
          all(jobs.STAGE_BY_KEY[k].get("pending") for k in ("run", "refine", "summarize")))
    check("rc=76 讲成「让位」而不是「失败」", "让位" in jobs._rc_note(76), jobs._rc_note(76))
    check("被杀掉的进程讲成「可续跑」", "续跑" in jobs._rc_note(-1), jobs._rc_note(-1))


def test_runner_live():
    """Actually spawn a read-only stage. Needs this machine's env + database."""
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        print("SKIP 实跑：没有 config.yaml（CI 上正常，契约检查已经跑过）")
        return
    from src import config, db, jobs
    cfg = config.load()
    if not Path(cfg.get("python_env", "")).exists():
        print("SKIP 实跑：python_env 不存在")
        return
    db_path = Path(cfg["db_path"])
    if not db_path.exists():
        print("SKIP 实跑：还没有数据库")
        return

    probe = {"key": "_test", "label": "只读探针", "icon": "·",
             "hint": "跑一次 pipeline.py report，只读不写", "args": ["report"]}
    jobs.STAGES.insert(0, probe)
    jobs.STAGE_BY_KEY["_test"] = probe
    try:
        rc = jobs._run_stage("_test", origin="test")
        check("实跑 report 退出码 0", rc == 0, f"rc={rc}")
        log = jobs.tail("_test")
        check("日志是 UTF-8 可读中文", "通话总数" in log, log.strip().splitlines()[-1][:40])
        st = jobs.status(db.connect(db_path), cfg)
        row = [s for s in st["stages"] if s["key"] == "_test"][0]
        check("状态里能看到结束时间与结论", row["finished"] and row["note"] == "已完成", row["note"])
    finally:
        p = jobs._log_path("_test")
        jobs.STAGES.remove(probe)
        jobs.STAGE_BY_KEY.pop("_test", None)
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    test_contract()
    test_runner_live()
    print("\n" + ("全部通过" if not FAILED else f"{len(FAILED)} 项失败：{FAILED}"))
    sys.exit(1 if FAILED else 0)

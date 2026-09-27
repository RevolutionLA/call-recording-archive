"""Seam audit: measure long-segment split behavior on real refined data.

Long segments (> qwen3.refine.chunk_sec) are cut into blocks, recognized
separately, then stitched back. Blocks do not overlap, so a character sitting
on a cut point can be dropped. This script puts a number on it using only the
data already in archive.db -- no GPU, no re-run.

Two things are measured:
  1. frequency  - how many segments actually split, and how many seams that is
  2. suspicion  - whether the forced-aligner timeline has a hole sitting right
                  on a seam (a dropped char leaves a gap between its neighbors)

A seam-to-seam comparison against the general inter-character gap distribution
turns "there is a gap" into "there is an unusually large gap here". A gap that
is only natural silence is indistinguishable from a dropped char by timeline
alone, so the >threshold hits are suspects, not verdicts - listen before fixing.

    python scripts/seam_audit.py [--chunk-sec 20] [--gap-ms 600]
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter

os.chdir(os.path.join(os.path.dirname(__file__), ".."))


def split_ms(a, b, step):
    """Same cutting rule as src/worker_qwen.py, kept in sync deliberately."""
    parts, s = [], int(a)
    b = max(int(b), s + 400)
    while b - s > step:
        parts.append((s, s + step))
        s += step
    parts.append((s, b))
    return parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-sec", type=float, default=20.0)
    ap.add_argument("--gap-ms", type=int, default=600,
                    help="inter-char gap above which a seam is a drop suspect")
    ap.add_argument("--db", default="data/archive.db")
    args = ap.parse_args()
    step = int(args.chunk_sec * 1000)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    calls = [r["call_id"] for r in con.execute(
        "SELECT call_id FROM asr_outputs WHERE engine='qwen3-asr'")]
    if not calls:
        sys.exit("库里还没有精修结果（asr_outputs 无 qwen3-asr 行），无可审计数据")

    n_seg = n_split = n_seam = 0
    longest = 0.0
    seam_gaps, all_gaps = [], []
    suspects = []
    for cid in calls:
        seams = set()
        for sg in con.execute(
                "SELECT start_ms, end_ms FROM segments WHERE call_id=?", (cid,)):
            dur = (sg["end_ms"] - sg["start_ms"]) / 1000
            longest = max(longest, dur)
            parts = split_ms(sg["start_ms"], sg["end_ms"], step)
            n_seg += 1
            if len(parts) > 1:
                n_split += 1
            for a, b in parts[:-1]:
                seams.add(b)
                n_seam += 1
        row = con.execute(
            "SELECT words_json FROM asr_outputs WHERE call_id=? AND engine='qwen3-asr'",
            (cid,)).fetchone()
        if not row or not row["words_json"]:
            continue
        words = sorted(json.loads(row["words_json"]), key=lambda w: w["s"])
        for w1, w2 in zip(words, words[1:]):
            gap = w2["s"] - w1["e"]
            if gap < 0:
                continue
            all_gaps.append(gap)
            hit = next((sm for sm in seams if w1["e"] <= sm <= w2["s"]), None)
            if hit is None:
                continue
            seam_gaps.append(gap)
            if gap > args.gap_ms:
                suspects.append((cid, hit, w1["w"], w2["w"], gap))

    base = [g for g in all_gaps if g > args.gap_ms]
    seam_over = [g for g in seam_gaps if g > args.gap_ms]
    print(f"精修通话 {len(calls)} 通 / 段 {n_seg}，最长段 {longest:.1f}s，切分阈值 {args.chunk_sec:g}s")
    print(f"跨块段 {n_split}（{n_split / max(n_seg, 1):.1%}），接缝总数 {n_seam}")
    print(f"接缝正落在字间隙上的样本 {len(seam_gaps)} 处；其中 >{args.gap_ms}ms 的 {len(seam_over)} 处"
          f"（{len(seam_over) / len(seam_gaps):.1%}）" if seam_gaps else "接缝处无可用字级间隙样本")
    print(f"全库字间隙 >{args.gap_ms}ms 的基础比例：{len(base) / max(len(all_gaps), 1):.2%}")
    if seam_gaps and base:
        ratio = (len(seam_over) / len(seam_gaps)) / (len(base) / len(all_gaps))
        print(f"接缝处出现大空洞的相对风险：{ratio:.1f} 倍于随机位置")
    if suspects:
        print(f"\n疑似丢字（接缝处空洞 >{args.gap_ms}ms），按间隙从大到小，建议试听这些点：")
        for cid, at, a, b, g in sorted(suspects, key=lambda x: -x[4])[:20]:
            print(f"  call {cid:>4}  t={at / 1000:>8.2f}s  「{a}」_「{b}」  空洞 {g}ms")
    print("\n注：空洞可能只是自然停顿，此项非自动判丢字；需人工试听后再决定是否上块间重叠。")


if __name__ == "__main__":
    main()

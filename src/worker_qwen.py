"""Subprocess worker: Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B.

Runs inside the conda `vllm` env (py3.10). Reads job JSONL on stdin-ish
path arg, writes result JSONL.
argv: jobs.jsonl out.jsonl asr_path aligner_path [lang] [batch_items]
      [align 1|0] [chunk_sec]
Each job:
  {"call_id":int,"seg_id":int,"wav":str,"start_ms":int,"end_ms":int,"context":str}
Result:
  {"call_id":..,"seg_id":..,"text":str,"lang":str,"words":[{"w","s","e"}] in call ms}
"""
import json, sys, time, types, wave
import numpy as np

MAX_CHUNK_MS = 20000   # 单条输入时长上限，见 split_ms


def _stub_nagisa():
    """qwen_asr imports the Japanese tokenizer `nagisa` at module load, which
    MemoryErrors on a tight page file. Callers here are Mandarin (incl.
    Sichuan/Henan accents), so replace it with a failing stub instead."""
    try:
        import nagisa  # noqa: F401
        return
    except Exception:
        pass
    m = types.ModuleType("nagisa")

    def _unavailable(*a, **k):
        raise RuntimeError("nagisa (Japanese tokenizer) is disabled; text is Mandarin")

    m.tagging = _unavailable
    m.Tagger = _unavailable
    sys.modules["nagisa"] = m


def read_slice(path, start_ms, end_ms):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        w.setpos(int(start_ms * sr / 1000))
        raw = w.readframes(int((end_ms - start_ms) * sr / 1000))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def ts_to_words(ts):
    """ForcedAlignResult -> [{w,s,e}] ms. Items are ForcedAlignItem(text, start_time, end_time in seconds)."""
    if ts is None:
        return []
    out = []
    items = getattr(ts, "items", None) \
        or getattr(ts, "alignments", None) or getattr(ts, "words", None) \
        or (ts if isinstance(ts, (list, tuple)) else None)
    if items is None:
        return []
    for it in items:
        if isinstance(it, dict):
            w = it.get("text") or it.get("w") or it.get("char") or it.get("word")
            s = it.get("start_time", it.get("start", it.get("s")))
            e = it.get("end_time", it.get("end", it.get("e")))
        else:
            w = getattr(it, "text", None) or getattr(it, "char", None) or getattr(it, "word", None)
            s = getattr(it, "start_time", None)
            if s is None:
                s = getattr(it, "start", None)
            e = getattr(it, "end_time", None)
            if e is None:
                e = getattr(it, "end", None)
        if s is None or e is None:
            continue
        # seconds -> ms (values are per-slice seconds in this SDK)
        out.append({"w": str(w), "s": int(float(s) * 1000), "e": int(float(e) * 1000)})
    return out


def split_ms(a, b, step):
    """Qwen3-ASR decodes open-end on long input: one 80s slice stalled the GPU for
    8 minutes. Cut a segment into <=step pieces and stitch the texts back."""
    parts, s = [], int(a)
    b = max(int(b), s + 400)
    while b - s > step:
        parts.append((s, s + step))
        s += step
    parts.append((s, b))
    return parts


def main():
    job_file, out_file, asr_path, aligner_path = sys.argv[1:5]
    lang = sys.argv[5] if len(sys.argv) > 5 else "Chinese"
    # 一次 transcribe 最多喂几「块」。6G 卡上 1.7B+0.6B 权重已占 ~4.6G，块数一多
    # 显存溢出到内存分页，实测 16 块一批会慢到 8 分钟出一条
    items = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6].isdigit() else 4
    # 字级时间戳由第二个模型（ForcedAligner）逐字生成，成本约占一半；
    # 只要更准的文本时可以用 align=0 换速度（VAD 段边界本来就有）
    align = (len(sys.argv) <= 7 or sys.argv[7] != "0")
    chunk_ms = (int(float(sys.argv[8]) * 1000) if len(sys.argv) > 8
                and float(sys.argv[8]) > 0 else MAX_CHUNK_MS)
    jobs = [json.loads(l) for l in open(job_file, encoding="utf-8") if l.strip()]
    print(f"[qwen] {len(jobs)} jobs, lang={lang}, items={items}, align={align}, "
          f"chunk={chunk_ms//1000}s, loading {asr_path}", flush=True)

    import torch
    _stub_nagisa()
    from qwen_asr import Qwen3ASRModel
    kw = dict(torch_dtype=torch.bfloat16, device_map="cuda",
              low_cpu_mem_usage=True)
    try:
        model = Qwen3ASRModel.from_pretrained(asr_path,
                                              forced_aligner=aligner_path if align else None,
                                              **kw)
    except TypeError:
        model = Qwen3ASRModel.from_pretrained(asr_path,
                                              forced_aligner=aligner_path if align else None)

    # 按「块」组批而不是按「段」：一个 80s 段会切成 5 块，若按段凑 4 条就会一次喂
    # 20 块且全部 padding 到最长，实测从 4s/段恶化到 36s/段
    groups, cur_jobs, cur_plan = [], [], []
    for j in jobs:
        chunks = split_ms(j["start_ms"], j["end_ms"], chunk_ms)
        if cur_plan and len(cur_plan) + len(chunks) > items:
            groups.append((cur_jobs, cur_plan))
            cur_jobs, cur_plan = [], []
        bi = len(cur_jobs)
        cur_jobs.append(j)
        for s, e in chunks:
            a, sr = read_slice(j["wav"], s, e)
            cur_plan.append((bi, (a, sr), s))
    if cur_plan:
        groups.append((cur_jobs, cur_plan))

    with open(out_file, "w", encoding="utf-8") as fo:
        t0, n = time.time(), 0
        for batch, plan in groups:
            audios = [p[1] for p in plan]
            try:
                res = model.transcribe(audio=audios if len(audios) > 1 else audios[0],
                                       context=[batch[p[0]].get("context", "") for p in plan],
                                       language=[lang] * len(plan) if lang else None,
                                       return_time_stamps=align)
            except Exception as e:
                print(f"[qwen] {len(batch)}段 {len(plan)}块 失败: {e}", flush=True)
                for j in batch:
                    fo.write(json.dumps({"call_id": j["call_id"], "seg_id": j["seg_id"],
                                         "error": str(e)[:200]}, ensure_ascii=False) + "\n")
                fo.flush()
                n += len(batch)
                continue
            merged = {bi: {"parts": [], "words": [], "lang": ""} for bi in range(len(batch))}
            for (bi, _, pstart), r in zip(plan, res):
                m = merged[bi]
                if r.text:
                    m["parts"].append(r.text.strip())
                m["lang"] = m["lang"] or (r.language or "")
                ws = ts_to_words(getattr(r, "time_stamps", None))
                for w in ws:               # chunk-local ms -> call timeline
                    w["s"] += pstart; w["e"] += pstart
                m["words"] += ws
            for bi, j in enumerate(batch):
                m = merged[bi]
                fo.write(json.dumps({"call_id": j["call_id"], "seg_id": j["seg_id"],
                                     "text": "".join(m["parts"]), "lang": m["lang"],
                                     "words": m["words"]}, ensure_ascii=False) + "\n")
            fo.flush()
            n += len(batch)
            now = time.time()
            print(f"[qwen] {n}/{len(jobs)} 上批 {len(batch)}段/{len(plan)}块 "
                  f"{now-t0:.0f}s ({(now-t0)/len(batch):.1f}s/段)", flush=True)
            t0 = now


if __name__ == "__main__":
    main()

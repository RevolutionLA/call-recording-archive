"""Subprocess worker: Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B.

Runs inside the conda `vllm` env (py3.10). Reads job JSONL on stdin-ish
path arg, writes result JSONL. Each job:
  {"call_id":int,"seg_id":int,"wav":str,"start_ms":int,"end_ms":int,"context":str}
Result:
  {"call_id":..,"seg_id":..,"text":str,"lang":str,"words":[{"w","s","e"}] in call ms}
"""
import json, sys, types, wave
import numpy as np


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


def main():
    job_file, out_file, asr_path, aligner_path = sys.argv[1:5]
    lang = sys.argv[5] if len(sys.argv) > 5 else "Chinese"
    jobs = [json.loads(l) for l in open(job_file, encoding="utf-8") if l.strip()]
    print(f"[qwen] {len(jobs)} jobs, lang={lang}, loading {asr_path}", flush=True)

    import torch
    _stub_nagisa()
    from qwen_asr import Qwen3ASRModel
    kw = dict(torch_dtype=torch.bfloat16, device_map="cuda",
              low_cpu_mem_usage=True)
    try:
        model = Qwen3ASRModel.from_pretrained(asr_path, forced_aligner=aligner_path,
                                              **kw)
    except TypeError:
        model = Qwen3ASRModel.from_pretrained(asr_path, forced_aligner=aligner_path)

    with open(out_file, "w", encoding="utf-8") as fo:
        B = 16
        for i in range(0, len(jobs), B):
            batch = jobs[i:i + B]
            audios = []
            for j in batch:
                a, sr = read_slice(j["wav"], j["start_ms"], max(j["end_ms"], j["start_ms"] + 400))
                audios.append((a, sr))
            try:
                res = model.transcribe(audio=audios if len(audios) > 1 else audios[0],
                                       context=[j.get("context", "") for j in batch],
                                       language=[lang] * len(batch) if lang else None,
                                       return_time_stamps=True)
            except Exception as e:
                print(f"[qwen] batch {i} failed: {e}", flush=True)
                for j in batch:
                    fo.write(json.dumps({"call_id": j["call_id"], "seg_id": j["seg_id"],
                                         "error": str(e)[:200]}, ensure_ascii=False) + "\n")
                fo.flush()
                continue
            for j, r in zip(batch, res):
                words = ts_to_words(getattr(r, "time_stamps", None))
                for w in words:  # align on slice -> shift to call timeline
                    w["s"] += j["start_ms"]; w["e"] += j["start_ms"]
                fo.write(json.dumps({"call_id": j["call_id"], "seg_id": j["seg_id"],
                                     "text": r.text, "lang": r.language,
                                     "words": words}, ensure_ascii=False) + "\n")
            fo.flush()
            print(f"[qwen] {min(i+B,len(jobs))}/{len(jobs)} done", flush=True)


if __name__ == "__main__":
    main()

"""Parse call-recording filenames: extract contact name / phone / call time.

Strategy: strip known noise (extension, REC/CALL keywords, date & time runs),
then look for Chinese-name tokens, phone numbers, or Latin name tokens.
Generic rules cover common Android auto-recording names; project-specific
patterns can be added in config.yaml `filename_patterns`.
"""
from __future__ import annotations
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

DATE_RE = re.compile(r"(?P<y>20\d{2})[-_.]?#?(?P<m>\d{1,2})[-_.]?(?P<d>\d{1,2})")
TIME_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2})[.:_-](?P<mi>\d{2})(?:[.:_-](?P<s>\d{2}))?(?!\d)")
TIME4_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])([0-5]\d)(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]*)?(?P<mob>1[3-9]\d[\s-]?\d{4}[\s-]?\d{4})(?!\d)|(?<!\d)(?P<land>(?:0\d{2,3}-?)?\d{7,8})(?!\d)")
COMPACT_DT_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})?(?!\d)")
MOBILE_RE = re.compile(r"1[3-9]\d{9}")   # 去掉分隔符后的标准手机号，用于「手机号优先」
SPACED_DIGITS_RE = re.compile(r"\d(?:[\s-]\d){5,}")  # "1 3 8 0 0 1 3 8 0 0 0" 这类逐位带分隔的号码
# 座机/热线常写成分组带空格：'010 6598 1234'、'400 610 1234'（示例号码为占位，非真实号码）
DIGIT_GROUPS_RE = re.compile(r"(?<!\d)\d{3,4}(?:[\s-]+\d{3,4}){1,2}(?!\d)")
# 手机/座机都没有时的身份线索：95xxx 特服、10086/12306、400/800 热线、00/+ 国际直拨
HOTLINE_RE = re.compile(
    r"(?<!\d)(?P<hl>(?:95\d{2,4}|1[0123]\d{3,4}|[48]00\d{7,9}|(?:00|\+)[\s-]?\d{8,15}))(?!\d)")

NOISE_WORDS = {
    "rec", "record", "recording", "call", "callout", "callin", "incoming", "outgoing",
    "m4a", "mp3", "wav", "amr", "ogg", "opus", "flac", "aac", "video", "audio",
    "auto", "cnt", "cnm", "dial", "from", "to", "with", "tel", "phone", "sim",
}
CN_NOISE = ["通话录音", "录音", "通话", "来电", "拨出", "打入", "拨入", "电话", "语音", "自动"]


def _is_noise_token(tok: str) -> bool:
    """噪声判给整个拼接词：'CallRecording' 算噪声，'Tom' / 'WangFang' 不算。"""
    parts = [p for p in re.split(r"(?<=[a-z])(?=[A-Z])|[.\-]+", tok) if p]
    return bool(parts) and all(p.lower() in NOISE_WORDS for p in parts)


def _squash_spaced_digits(text: str) -> str:
    """Collapse '138 0013 8000' / '+86 138...' / '010 6234 5678' into one digit
    run so the date and phone regexes work on grouped-number filenames."""

    def sub(m):
        return re.sub(r"\D", "", m.group(0))

    return SPACED_DIGITS_RE.sub(sub, DIGIT_GROUPS_RE.sub(sub, text))


CN_DATE_RE = re.compile(r"(?P<y>20\d{2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?")

def parse_filename(path: str | Path, file_mtime: Optional[float] = None,
                   extra_patterns: Optional[list[str]] = None) -> dict:
    """Return {name, phone, call_time(iso), matched_by}."""
    p = Path(path)
    stem = p.stem
    out: dict = {"name": None, "phone": None, "call_time": None, "matched_by": None}
    work = _squash_spaced_digits(stem)  # '+86 138 0013 8000' -> '+8613800138000'

    # Chinese date form first (2024年1月5日)
    cm = CN_DATE_RE.search(work)
    if cm:
        y, mo, d = (int(x) for x in cm.groups())
        try:
            base = datetime(y, mo, d)
            nd = (work[:cm.start()] + " " + work[cm.end():]).strip()
            tm = TIME_RE.search(nd) or TIME4_RE.search(nd)
            if tm:
                g = tm.groups()
                base = base.replace(hour=min(int(g[0]), 23), minute=min(int(g[1]), 59),
                                    second=int(g[2] or 0) if len(g) > 2 else 0)
                nd = (nd[:tm.start()] + " " + nd[tm.end():]).strip()
            out["call_time"] = base.isoformat(timespec="seconds")
            out["matched_by"] = "cn_date"
            work = nd
        except ValueError:
            pass

    # compact yyyyMMddHHmmss first
    m = COMPACT_DT_RE.search(work) if out["call_time"] is None else None
    if m:
        y, mo, d, h, mi, s = m.groups()
        try:
            out["call_time"] = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0)).isoformat(timespec="seconds")
            out["matched_by"] = "compact_datetime"
        except ValueError:
            pass
        work = (work[:m.start()] + " " + work[m.end():]).strip()

    if out["call_time"] is None:
        dm = DATE_RE.search(work)
        dt = None
        if dm:
            y, mo, d = (int(x) for x in dm.groups())
            try:
                base = datetime(y, mo, d)
                after, before = work[dm.end():].lstrip(), work[:dm.start()].rstrip()
                work = (work[:dm.start()] + " " + work[dm.end():]).strip()
                tm = TIME_RE.search(work)
                if tm:
                    h, mi, s = tm.groups()
                    base = base.replace(hour=min(int(h), 23), minute=min(int(mi), 59), second=int(s or 0))
                    work = (work[:tm.start()] + " " + work[tm.end():]).strip()
                else:
                    # 只认紧贴日期的 4 位时分（'2024-02-02 1011'），孤立数字不当时间
                    hhmm = re.match(r"(\d{4})(?!\d)", after) or re.search(r"(?<!\d)(\d{4})$", before)
                    if hhmm:
                        h, mi = int(hhmm.group(1)[:2]), int(hhmm.group(1)[2:])
                        if h <= 23 and mi <= 59:
                            base = base.replace(hour=h, minute=mi)
                            work = work.replace(hhmm.group(1), " ", 1).strip()
                dt = base.isoformat(timespec="seconds")
            except ValueError:
                dt = None
        out["call_time"] = dt
        if dt:
            out["matched_by"] = "date_time"

    # phone numbers
    phones = [ (m.group("mob") or m.group("land")).replace(" ", "").replace("-", "")
               for m in PHONE_RE.finditer(work) ]
    if phones:
        # 手机号优先于「最长」：带区号的座机能凑到 12 位，按长度会挑错主体
        out["phone"] = next((p for p in phones if MOBILE_RE.fullmatch(p)),
                            max(phones, key=len))
        work = PHONE_RE.sub(" ", work)
    else:
        # 没有手机号/座机时，退一步认特服号与热线：95xxx、10086/12306、400/800、00 国际直拨
        hl = [m.group("hl").replace(" ", "").replace("-", "") for m in HOTLINE_RE.finditer(work)]
        if hl:
            out["phone"] = max(hl, key=len)
            work = HOTLINE_RE.sub(" ", work)

    # strip config-provided custom patterns (named groups name/phone)
    for pat in extra_patterns or []:
        try:
            mm = re.search(pat, stem)
        except re.error:
            continue
        if mm:
            gd = mm.groupdict()
            if gd.get("name"):
                out["name"] = gd["name"]
                out["matched_by"] = f"pattern:{pat[:24]}"
            if gd.get("phone") and not out["phone"]:
                out["phone"] = gd["phone"]

    # Chinese name tokens: 2-4 chars look like a person, 5-6 like a company/客服
    if not out["name"]:
        cands = []
        for cn_word in re.findall(r"[\u4e00-\u9fa5]{2,6}", work):
            w = cn_word
            for noise in CN_NOISE:
                w = w.replace(noise, "")
            if 2 <= len(w) <= 6:
                cands.append(w)
        if cands:
            out["name"] = next((x for x in cands if len(x) <= 4), cands[0])
    # Latin tokens
    if not out["name"]:
        toks = re.split(r"[^A-Za-z.\-]+", work)
        cand = [t for t in toks if len(t) >= 2 and not _is_noise_token(t)]
        if cand:
            out["name"] = " ".join(cand[:2]).strip(" .-")

    if out["call_time"] is None and file_mtime and _cfg_fallback():
        out["call_time"] = datetime.fromtimestamp(file_mtime).isoformat(timespec="seconds")
        out["matched_by"] = out["matched_by"] or "mtime"
    return out


_fb_cache: Optional[bool] = None


def _cfg_fallback() -> bool:
    global _fb_cache
    if _fb_cache is None:
        try:
            from . import config
            _fb_cache = bool(config.load().get("fallback_to_mtime", True))
        except Exception:
            _fb_cache = True
    return _fb_cache


if __name__ == "__main__":
    import sys, json
    samples = sys.argv[1:] or [
        "REC_2023-11-02_14.32.18 张伟.m4a",
        "通话录音 李娜 2024年1月5日 0930.m4a",
        "20240315_201530_from_13800138000.wav",
        "CallRecording_20230707153022_WangFang.m4a",
        "来电 18612345678 2023-09-09.m4a",
        "010 6234 5678_20240115143022.m4a",
        "中国移动客服@95588_20240115143022.m4a",
        "+86 138 0013 8000 2024-02-02 1011.m4a",
    ]
    for s in samples:
        print(json.dumps(parse_filename(s), ensure_ascii=False))

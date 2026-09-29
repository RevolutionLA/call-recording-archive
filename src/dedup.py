"""Duplicate pass: the same recording backed up into several folders.

Copies are only *marked*: originals stay on disk untouched, every mark is
reversible, and the canonical pick prefers whichever copy already has the most
work done on it. Candidate grouping is pure SQL (size + duration); because a
constant-bitrate recording makes size and duration the same information, our
own archive has 112 such "collisions" that are all different calls — so a
candidate is only accepted once content evidence agrees: transcript
similarity when both sides have one, otherwise a sampled fingerprint of the
first and last 64 KiB (never a whole-file hash, and it is cached in the db).
"""
from __future__ import annotations
import difflib, hashlib
from datetime import datetime
from pathlib import Path

from . import db, naming

NEAR_SEC = 2.0            # 转码副本的时长容差
NEAR_TEXT = 0.85          # 两边都已有转写文本时要求文本相似度
NEAR_MIN = 120            # 没文本可比时，两份录音的通话时间要在这个秒数内
SLICE = 65536             # 抽样指纹读的字节数（头尾各这么多）
AUTO = "自动判重"


def _status_rank(status: str) -> int:
    """已投入的算力越多越该当正本——副本再跑一遍纯属浪费显卡。"""
    return {"analyzed": 4, "summarized": 4, "transcribed": 3, "normalized": 2,
            "pending": 1, "skipped": 0, "error": 0}.get(status, 1)


def _path_score(path: str) -> tuple:
    """同分时挑「看起来像原件」的那条路径：目录浅的、名字里没有「副本/backup」的。"""
    p = Path(path)
    noisy = 1 if any(k in str(p).lower() for k in ("副本", "copy", "backup", "备份", "bak")) else 0
    return (noisy, len(p.parts), str(p))


def _pick(rows, manual_canon=None):
    if manual_canon is not None and any(r["id"] == manual_canon for r in rows):
        return next(r for r in rows if r["id"] == manual_canon)

    def key(r):
        noisy, depth, _ = _path_score(r["path"])
        return (-_status_rank(r["status"]), noisy, depth, r["call_time"] or "~", r["path"])
    return min(rows, key=key)


def assign_keys(conn) -> int:
    """库内指纹：文件大小 + 时长（0.1 秒）。复制/改名都能对上，转码的走近似规则。"""
    conn.execute(
        "UPDATE calls SET audio_key = COALESCE(size_bytes,'-') || '|' ||"
        " CASE WHEN duration_sec IS NULL THEN '-' ELSE CAST(ROUND(duration_sec,1) AS TEXT) END")
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM calls WHERE audio_key IS NOT NULL").fetchone()[0]


def _clear_auto(conn) -> int:
    """每轮重算：只清自动标记，用户在页面上手动指定的正本原样保留。"""
    n = conn.execute("SELECT COUNT(*) FROM calls WHERE dup_reason LIKE ?", (AUTO + "%",)).fetchone()[0]
    conn.execute("UPDATE calls SET dup_of=NULL, dup_reason=NULL WHERE dup_reason LIKE ?",
                 (AUTO + "%",))
    conn.execute("UPDATE calls SET status='pending' WHERE status='skipped' AND dup_of IS NULL")
    conn.commit()
    return n


def _manual_canons(conn) -> dict:
    """{通话组内被手动指定过的正本 id: 正本 id}，用于让它压过自动选择。"""
    return {r["dup_of"]: r["dup_of"] for r in conn.execute(
        "SELECT DISTINCT dup_of FROM calls WHERE dup_reason LIKE '手动%' AND dup_of IS NOT NULL")}


_COLS = ("id, path, filename, size_bytes, duration_sec, status, dup_of, dup_reason, "
         "contact_hint, phone, call_time, source_id, probe_key")


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _text_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a[:2000], b[:2000]).ratio()


def _iso(t: str):
    try:
        return datetime.fromisoformat(t)
    except (TypeError, ValueError):
        return None


def _same_stem(a, b) -> bool:
    return Path(a["filename"]).stem == Path(b["filename"]).stem


def _close_time(a, b) -> bool:
    x, y = _iso(a["call_time"]), _iso(b["call_time"])
    return bool(x and y) and abs((x - y).total_seconds()) <= NEAR_MIN


def _probe_file(path: str):
    """头尾各 64KB 的抽样指纹：读 128KB 而不是整个文件，
    但已经足够把「巧合同大小同长度」的两通不同电话分开。"""
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            head = f.read(SLICE)
            tail = b""
            if size > 2 * SLICE:
                f.seek(size - SLICE)
                tail = f.read(SLICE)
    except OSError:
        return None
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def ensure_probes(conn, rows) -> dict:
    """只为候选组算抽样指纹，算过就存进 calls.probe_key，重跑不再读盘。"""
    out, dirty = {}, []
    for r in rows:
        v = r.get("probe_key") or _probe_file(r["path"])
        if v:
            out[r["id"]] = v
            if not r.get("probe_key"):
                dirty.append((v, r["id"]))
    if dirty:
        conn.executemany("UPDATE calls SET probe_key=? WHERE id=?", dirty)
        conn.commit()
    return out


def _same_content(a, b, txt, probes=None, same_size=False) -> bool:
    """两份是不是同一段录音。

    先比转写文本（最便宜也最不会骗人）；文本没备齐时：近似组（同一分钟、时长
    相差两秒内）已经被文件名/时间约束住，而「大小+时长完全相同」的组在本机实测
    112 组全是巧合——m4a 是常量码率，同大小就等于同时长，两个不同电话凑一起并不
    稀奇，所以那种组必须要抽样指纹一致才算副本。
    """
    ta, tb = txt.get(a["id"], ""), txt.get(b["id"], "")
    if ta and tb:
        return _text_sim(ta, tb) >= NEAR_TEXT
    if same_size:
        probes = probes or {}
        pa, pb = probes.get(a["id"]), probes.get(b["id"])
        if pa is None or pb is None:      # 文件读不到（NAS 掉线）才退回文件名
            return _same_stem(a, b) and _close_time(a, b)
        return pa == pb
    return _same_stem(a, b)      # 近似组：转码会改大小，但一般不改文件名


def _texts(conn, ids) -> dict:
    """优先用检索全文；还没写全文的（多数只转过写）直接拼段落，别为了比对重跑音频。
    一次传进来的可能是全库 id，SQLite 的变量上限挡得住几万行，但别拿它赌：分块查。"""
    out = {i: "" for i in ids}
    for k in range(0, len(ids), 500):
        part = list(ids[k:k + 500])
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT id, fulltext_zh FROM calls WHERE id IN ({q})", part):
            out[r["id"]] = r["fulltext_zh"] or ""
        for r in conn.execute(f"SELECT call_id id, GROUP_CONCAT(text_zh,'') t FROM segments "
                              f"WHERE call_id IN ({q}) AND text_zh IS NOT NULL GROUP BY call_id",
                              part):
            if not out[r["id"]]:
                out[r["id"]] = r["t"] or ""
    return out


def _near_groups(conn):
    """转码过的副本：同一个对方线索 + 同一分钟 + 时长相差 <=2 秒；
    两边都有转写文本时再要一次文本相似，避免把「同一分钟两通一样长的电话」误判。

    组是按核验过的**成对关系**连出来的，不是把整个时间桶一起端走：同一分钟里
    可能有两对各自独立的备份，混成一组会把四通电话并成一通，档案里凭空少两通。
    """
    rows = _rows(conn, f"SELECT {_COLS} FROM calls "
                       "WHERE dup_of IS NULL AND duration_sec IS NOT NULL AND call_time IS NOT NULL")
    txt = _texts(conn, [r["id"] for r in rows])
    buckets = {}
    for r in rows:
        who = r["contact_hint"] or r["phone"]
        if not who:
            continue
        buckets.setdefault((who, (r["call_time"] or "")[:16]), []).append(r)
    out = []
    for grp in buckets.values():
        if len(grp) < 2:
            continue
        parent = {r["id"]: r["id"] for r in grp}

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                if abs((a["duration_sec"] or 0) - (b["duration_sec"] or 0)) > NEAR_SEC:
                    continue
                if _same_content(a, b, txt):
                    parent[root(a["id"])] = root(b["id"])
        comps = {}
        for r in grp:
            comps.setdefault(root(r["id"]), []).append(r)
        out += [g for g in comps.values() if len(g) >= 2]
    return out


def mark(conn, cfg=None) -> dict:
    """(重新)标出全库的重复备份。可反复运行，结果只增不减地朝当前数据看齐。"""
    keyed = assign_keys(conn)
    line_n = classify_lines(conn, (cfg or {}).get("shared_line_names"))
    _clear_auto(conn)
    manual = _manual_canons(conn)

    groups = []
    exact = []
    for r in conn.execute("SELECT audio_key FROM calls WHERE audio_key IS NOT NULL "
                          "GROUP BY audio_key HAVING COUNT(*)>1"):
        rows = [x for x in _rows(conn, f"SELECT {_COLS} FROM calls WHERE audio_key=? ORDER BY id",
                                 (r["audio_key"],))
                if x["dup_of"] is None or x["id"] in manual or x["dup_of"] in manual]
        if len(rows) >= 2:
            exact.append(rows)
    txt = _texts(conn, [x["id"] for g in exact for x in g])
    picked = [(rows, _pick(rows, next((i for i in manual if any(x["id"] == i for x in rows)), None)))
              for rows in exact]
    # 一组里只要「正本与副本都转过写」就能靠文本判定，不必读盘；否则整组都算抽样指纹
    need = [x for rows, canon in picked
            if not all(txt.get(canon["id"]) and txt.get(x["id"]) for x in rows)
            for x in rows]
    probes = ensure_probes(conn, need)
    for rows, canon in picked:
        members = [x for x in rows if x["id"] == canon["id"]
                   or _same_content(canon, x, txt, probes, same_size=True)]
        if len(members) >= 2:
            groups.append(("exact", members))
    exact_kept = sum(1 for k, _ in groups if k == "exact")
    near = _near_groups(conn)
    for rows in near:
        groups.append(("near", rows))

    # 两轮分组可能撞上同一行（既是同指纹又是近似）：先判定的那份说了算，后一轮不再改写，
    # 否则会拿近似规则的理由覆盖掉更硬的同指纹结论。用户手动指定的正本同样先到先得。
    done = {r["id"]: r["dup_of"] for _, rows in groups for r in rows
            if r["dup_of"] and str(r["dup_reason"] or "").startswith("手动")}
    copies = marked_groups = 0
    for kind, rows in groups:
        free = [r for r in rows if r["id"] not in done]
        if len(free) < 2:
            continue
        canon = _pick(free, next((i for i in manual if any(x["id"] == i for x in free)), None))
        reason = (f"{AUTO}：与正本同为 {(canon['size_bytes'] or 0)/1048576:.1f}MB / "
                  f"{(canon['duration_sec'] or 0):.0f} 秒，且转写或抽样内容一致") if kind == "exact" else \
                 f"{AUTO}：同一分钟、时长相差 {NEAR_SEC:.0f} 秒内的转码副本"
        hit = 0
        for r in free:
            if r["id"] == canon["id"]:
                continue
            conn.execute("UPDATE calls SET dup_of=?, dup_reason=?, updated_at=? WHERE id=?",
                         (canon["id"], reason, db.now(), r["id"]))
            if r["status"] in ("pending", "normalized", "error"):
                conn.execute("UPDATE calls SET status='skipped' WHERE id=?", (r["id"],))
            done[r["id"]] = canon["id"]
            copies += 1
            hit += 1
        marked_groups += 1 if hit else 0
    conn.commit()
    # groups/copies 是「库里现在共多少」，this_run 才是「这一轮新标了多少」——
    # 两个数以前挤在同一组键里，重跑时把"本轮 0 份"报成"3 份"，看不出没干活。
    return {"keyed": keyed, "lines_marked": line_n,
            "this_run": {"groups": marked_groups, "copies": copies,
                         "candidate_groups": len(groups)},
            "candidates": {"same_fingerprint": len(exact), "near": len(near)},
            "exact_kept": exact_kept, "probed": len(probes),
            **summary(conn)}


def summary(conn) -> dict:
    row = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(duration_sec),0) d, "
                       "COALESCE(SUM(size_bytes),0) b FROM calls WHERE dup_of IS NOT NULL").fetchone()
    gr = conn.execute("SELECT COUNT(DISTINCT dup_of) FROM calls WHERE dup_of IS NOT NULL").fetchone()[0]
    return {"copies": row["c"], "groups": gr, "saved_sec": row["d"], "saved_bytes": row["b"]}


def list_groups(conn, limit: int = 300) -> list:
    ids = [r["dup_of"] for r in conn.execute(
        "SELECT DISTINCT dup_of FROM calls WHERE dup_of IS NOT NULL ORDER BY dup_of LIMIT ?",
        (limit,)).fetchall()]
    out = []
    for cid in ids:
        rows = _rows(conn, f"SELECT {_COLS} FROM calls WHERE id=? OR dup_of=? "
                           "ORDER BY dup_of IS NULL DESC, id", (cid, cid))
        out.append({"canonical": rows[0], "copies": [r for r in rows if r["dup_of"] is not None]})
    return out


def set_canonical(conn, call_id: int) -> dict:
    """把组里任意一份改成正本：其余（含原正本）都指向它，并放回待处理队列。"""
    row = conn.execute("SELECT id, dup_of FROM calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise LookupError("通话不存在")
    old = row["dup_of"] or call_id
    ids = {r["id"] for r in conn.execute("SELECT id FROM calls WHERE id=? OR dup_of=?",
                                         (old, old)).fetchall()} | {call_id}
    q = ",".join("?" * len(ids))
    conn.execute(f"UPDATE calls SET dup_of=?, dup_reason='手动指定正本：#{call_id}' "
                 f"WHERE id!=? AND id IN ({q})", (call_id, call_id, *ids))
    conn.execute("UPDATE calls SET dup_of=NULL, dup_reason=NULL WHERE id=?", (call_id,))
    conn.execute("UPDATE calls SET status='pending' WHERE id=? AND status='skipped'", (call_id,))
    conn.commit()
    return {"ok": True, "canonical": call_id, "restamped": len(ids) - 1}


def unmark(conn, call_id: int) -> dict:
    """解除这一份的副本标记：它重新变成一份独立通话。"""
    conn.execute("UPDATE calls SET dup_of=NULL, dup_reason=NULL WHERE id=?", (call_id,))
    conn.execute("UPDATE calls SET status='pending' WHERE id=? AND status='skipped'", (call_id,))
    conn.commit()
    return {"ok": True, "id": call_id}


def classify_lines(conn, extra_names=None) -> int:
    """标 person / shared_line：企业专线（大厂总机、银行客服、95xxx）对面会换人。"""
    n = 0
    for r in conn.execute("SELECT id, contact_hint, phone, line_kind FROM calls").fetchall():
        kind = naming.classify_line(r["contact_hint"], r["phone"], extra_names)
        if kind != r["line_kind"]:
            conn.execute("UPDATE calls SET line_kind=? WHERE id=?", (kind, r["id"]))
            n += 1
    conn.commit()
    return n

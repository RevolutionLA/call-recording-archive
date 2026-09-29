"""Shared lines: one number, several people (大厂总机、银行客服、快递站点).

`identity.resolve_contacts` trusts the filename hint and stamps a whole call
onto one contact — right for a person's mobile, wrong for an organisation
line where whoever picks up becomes the same archive entry. This module
clusters the 'other' voiceprints *inside* one line, names each cluster from
what the seat said about themselves (工号 / 我叫…), and re-attaches the
segments. Auto-applied; 「并回总机」 in the cockpit reverses a wrong split.
"""
from __future__ import annotations
from collections import defaultdict

import numpy as np

from . import db, naming

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _cfg(cfg, key, default):
    return (cfg or {}).get("identity", {}).get(key, default)


def _norm(v):
    return np.asarray(v, dtype=np.float32) / (np.linalg.norm(v) + 1e-9)


def _call_centroids(conn, min_segs=2):
    """{call_id: (centroid, n_segments)}，只收有 >=2 条声纹的通话：
    一句 1.5 秒的话不足以凭空立出「另一个人」。"""
    acc = defaultdict(list)
    for r in conn.execute("SELECT call_id, embedding FROM segments "
                          "WHERE who='other' AND embedding IS NOT NULL"):
        acc[r["call_id"]].append(np.frombuffer(r["embedding"], dtype=np.float32))
    out = {}
    for cid, vs in acc.items():
        if len(vs) >= min_segs:
            out[cid] = (_norm(np.mean(vs, axis=0)), len(vs))
    return out


def _cluster(items, thr):
    """贪心余弦聚类，items = [(key, centroid)]。"""
    clusters = []
    for key, v in items:
        best, best_sim = None, -1.0
        for cl in clusters:
            sim = float(v @ cl["m"])
            if sim > best_sim:
                best, best_sim = cl, sim
        if best is not None and best_sim >= thr:
            n = len(best["keys"])
            best["m"] = _norm(best["m"] * n + v)
            best["keys"].append(key)
        else:
            clusters.append({"m": v, "keys": [key]})
    return clusters


def _cross_sim(clusters):
    worst = 1.0
    for i, a in enumerate(clusters):
        for b in clusters[i + 1:]:
            worst = min(worst, float(a["m"] @ b["m"]))
    return worst


def _label(conn, call_ids, org_hint=None):
    """从这几通的对方文本里抠坐席自称（工号优先）。"""
    if not call_ids:
        return None
    q = ",".join("?" * len(call_ids))
    for r in conn.execute(f"SELECT text_zh FROM segments WHERE call_id IN ({q}) "
                          "AND who='other' AND text_zh IS NOT NULL ORDER BY call_id, idx",
                          list(call_ids)):
        v = naming.extract_seat_label(r["text_zh"] or "")
        # "这里是申通客服" 抠出来的是单位名不是人名，拿它当坐席名等于没拆
        if v and not (org_hint and (v == org_hint or v in org_hint or org_hint in v)):
            return v
    return None


def _org_contact(conn, hint, phone):
    row = conn.execute("SELECT id FROM contacts WHERE name=?", (hint,)).fetchone()
    if row:
        cid = row["id"]
        conn.execute("UPDATE contacts SET kind=COALESCE(kind,'org') WHERE id=?", (cid,))
    else:
        cid = conn.execute("INSERT INTO contacts(name,phone,kind,note,updated_at) VALUES(?,?,?,?,?)",
                          (hint, phone, "org", "总机/共享线路（按声纹自动分人）", db.now())).lastrowid
    return cid


def _seat_centroid(conn, seat_id):
    vs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in
          conn.execute("SELECT embedding FROM voiceprints WHERE contact_id=?", (seat_id,))]
    return _norm(np.mean(vs, axis=0)) if vs else None


def _reuse_seat(conn, org_id, centroid, thr, claimed=()):
    """已有坐席里找声纹对得上的：编号和名字保持稳定，别每次重跑都换人。
    claimed = 本轮已经被别的簇占用的坐席，不能再叠第二个簇上去（否则两把嗓子
    并成一个档案，正好是这一步要解决的问题）。"""
    best, best_sim = None, -1.0
    for s in conn.execute("SELECT id FROM contacts WHERE parent_id=?", (org_id,)):
        if s["id"] in claimed:
            continue
        m = _seat_centroid(conn, s["id"])
        if m is None:
            continue
        sim = float(m @ centroid)
        if sim > best_sim:
            best, best_sim = s["id"], sim
    return best if best_sim >= thr else None


def _next_letter(conn, hint, org_id):
    used = {r["name"].split("·")[-1] for r in
            conn.execute("SELECT name FROM contacts WHERE parent_id=?", (org_id,))}
    return next((t for t in _LETTERS if t not in used), str(len(used) + 1))


def _is_letter_tag(name, hint):
    return bool(name and name.startswith(hint + "·")
                and name.split("·")[-1] in _LETTERS and len(name.split("·")[-1]) == 1)


def _enroll(conn, seat_id, call_ids, cents):
    conn.execute("DELETE FROM voiceprints WHERE contact_id=?", (seat_id,))
    for cid in call_ids:
        if cid not in cents:
            continue
        conn.execute("INSERT INTO voiceprints(contact_id,embedding,source_call_id,quality,created_at)"
                     " VALUES(?,?,?,?,?)",
                     (seat_id, cents[cid][0].astype(np.float32).tobytes(), cid, 0.6, db.now()))


def split(conn, cfg=None) -> dict:
    """把「同一个机构名」下的不同坐席按声纹拆成子档案，并自动起名。"""
    thr = float(_cfg(cfg, "seat_sim_thr", 0.5))
    max_sep = float(_cfg(cfg, "seat_split_max_sim", 0.45))
    sep_person = float(_cfg(cfg, "seat_split_max_sim_person", 0.35))
    need_line = int(_cfg(cfg, "seat_min_calls", 2))
    need_person = int(_cfg(cfg, "seat_person_min_calls", 3))

    cents = _call_centroids(conn)
    if not cents:
        return {"groups": 0, "seats": 0, "moved": 0, "splits": [],
                "note": "还没有可用的对方声纹：先跑「转写 + 分出你我」和「归并联系人」"}
    calls = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, contact_hint, phone, line_kind, dup_of FROM calls").fetchall()}
    by_hint = defaultdict(list)
    for cid, (m, _n) in cents.items():
        c = calls.get(cid)
        if c and c["contact_hint"] and not c["dup_of"]:
            by_hint[c["contact_hint"]].append((cid, m))

    groups = seats = moved = 0
    splits = []
    for hint, items in by_hint.items():
        if len(items) < 2:
            continue
        line = calls[items[0][0]]["line_kind"] or "person"
        need = need_line if line == "shared_line" else need_person
        limit = max_sep if line == "shared_line" else sep_person
        big = [c for c in _cluster(items, thr) if len(c["keys"]) >= need]
        # 拆分的代价是把一个人切成两份档案，所以要求簇之间足够远才动手
        if len(big) < 2 or _cross_sim(big) >= limit:
            continue
        phone = next((calls[k]["phone"] for cl in big for k in cl["keys"] if calls[k]["phone"]), None)
        org_id = _org_contact(conn, hint, phone)
        groups += 1
        rows = []
        claimed = set()
        for cl in sorted(big, key=lambda c: len(c["keys"]), reverse=True):
            centroid = _norm(cl["m"])
            intra = float(np.mean([float(cents[k][0] @ centroid) for k in cl["keys"]]))
            seat_id = _reuse_seat(conn, org_id, centroid, thr, claimed)
            label = _label(conn, cl["keys"], hint)
            created = False
            if seat_id is None:
                name = f"{hint}·{label or _next_letter(conn, hint, org_id)}"
                hit = conn.execute("SELECT id FROM contacts WHERE name=?", (name,)).fetchone()
                if hit:
                    seat_id = hit["id"]
                else:
                    seat_id = conn.execute(
                        "INSERT INTO contacts(name,phone,kind,parent_id,note,updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (name, phone, "seat", org_id,
                         f"由「{hint}」按声纹拆分：{len(cl['keys'])} 通，簇内自比 {intra:.2f}",
                         db.now())).lastrowid
                    created = True
            elif label:
                cur = conn.execute("SELECT name FROM contacts WHERE id=?", (seat_id,)).fetchone()
                if _is_letter_tag(cur["name"], hint):        # 先前只有字母代号，现在听到了真名
                    new = f"{hint}·{label}"
                    if not conn.execute("SELECT 1 FROM contacts WHERE name=?", (new,)).fetchone():
                        conn.execute("UPDATE contacts SET name=?,updated_at=? WHERE id=?",
                                     (new, db.now(), seat_id))
                        cur = {"name": new}
                    label = None
            claimed.add(seat_id)
            q = ",".join("?" * len(cl["keys"]))
            conn.execute(f"UPDATE segments SET contact_id=?, contact_conf=? WHERE who='other' "
                         f"AND call_id IN ({q})", (seat_id, 0.8, *cl["keys"]))
            _enroll(conn, seat_id, cl["keys"], cents)
            moved += len(cl["keys"])
            if created:
                seats += 1
            rows.append({"seat_id": seat_id, "name": conn.execute(
                "SELECT name FROM contacts WHERE id=?", (seat_id,)).fetchone()["name"],
                "calls": len(cl["keys"]), "intra_sim": round(intra, 2),
                "cross_sim": round(_cross_sim(big), 2)})
        conn.execute("UPDATE contacts SET n_calls=(SELECT COUNT(DISTINCT call_id) FROM segments "
                     "WHERE contact_id=contacts.id) WHERE id=? OR parent_id=?", (org_id, org_id))
        splits.append({"org": hint, "org_id": org_id, "line_kind": line, "seats": rows})
    conn.commit()
    return {"groups": groups, "seats": seats, "moved": moved, "splits": splits,
            **report_counts(conn)}


def report_counts(conn) -> dict:
    return {"orgs": conn.execute("SELECT COUNT(*) FROM contacts WHERE kind='org'").fetchone()[0],
            "seat_contacts": conn.execute("SELECT COUNT(*) FROM contacts WHERE kind='seat'").fetchone()[0],
            # 光看号码就能认出是专线，这一步和声纹拆人无关，先报出来免得页面看着像"没识别到专线"
            "shared_calls": conn.execute("SELECT COUNT(*) FROM calls WHERE line_kind='shared_line'"
                                         " AND dup_of IS NULL").fetchone()[0]}


def merge_back(conn, seat_id: int) -> dict:
    """拆错了：坐席并回总机，段落归属和声纹库都退回上级联系人。"""
    seat = conn.execute("SELECT id,parent_id,name FROM contacts WHERE id=?", (seat_id,)).fetchone()
    if not seat or seat["parent_id"] is None:
        raise LookupError("这不是拆分出来的坐席档案")
    conn.execute("UPDATE segments SET contact_id=? WHERE contact_id=?", (seat["parent_id"], seat_id))
    conn.execute("UPDATE voiceprints SET contact_id=? WHERE contact_id=?", (seat["parent_id"], seat_id))
    conn.execute("DELETE FROM contacts WHERE id=?", (seat_id,))
    conn.execute("UPDATE contacts SET n_calls=(SELECT COUNT(DISTINCT call_id) FROM segments "
                 "WHERE contact_id=contacts.id) WHERE id=?", (seat["parent_id"],))
    conn.commit()
    return {"ok": True, "into": seat["parent_id"], "name": seat["name"]}


def seat_for(conn, hint: str, centroid) -> int | None:
    """resolve_contacts 用：专线通话先落到已知坐席，落不到再回总机。"""
    org = conn.execute("SELECT id FROM contacts WHERE name=?", (hint,)).fetchone()
    if not org or org["id"] is None:
        return None
    if conn.execute("SELECT 1 FROM contacts WHERE id=? AND kind='org'", (org["id"],)).fetchone() is None:
        return None
    return _reuse_seat(conn, org["id"], centroid, 0.5)


def report(conn, limit: int = 60) -> list:
    """总机 -> 坐席的拆分结果（含证据），给驾驶舱展示。"""
    out = []
    for org in conn.execute("SELECT id,name,phone,n_calls,note FROM contacts WHERE kind='org' "
                            "ORDER BY n_calls DESC LIMIT ?", (limit,)).fetchall():
        seats = []
        for s in conn.execute("SELECT id,name,n_calls,note FROM contacts WHERE parent_id=? "
                              "ORDER BY n_calls DESC", (org["id"],)).fetchall():
            calls = conn.execute("SELECT DISTINCT c.id, c.call_time, c.filename FROM segments sg "
                                 "JOIN calls c ON c.id=sg.call_id WHERE sg.contact_id=? "
                                 "ORDER BY c.call_time DESC LIMIT 12", (s["id"],)).fetchall()
            seats.append({"id": s["id"], "name": s["name"], "n_calls": s["n_calls"],
                          "note": s["note"], "calls": [dict(c) for c in calls]})
        if not seats:
            continue
        rest = conn.execute("SELECT COUNT(DISTINCT call_id) FROM segments sg WHERE contact_id=? "
                            "AND call_id IN (SELECT id FROM calls WHERE contact_hint=?)",
                            (org["id"], org["name"])).fetchone()[0]
        out.append({"id": org["id"], "name": org["name"], "phone": org["phone"],
                    "n_calls": org["n_calls"], "note": org["note"],
                    "unsplit_calls": rest, "seats": seats})
    return out

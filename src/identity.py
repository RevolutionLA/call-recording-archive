"""Speaker identity: who is 'me', which contact is 'other', voiceprint library.

Flow
  A) transcribe stage stores per-utterance CAM++ embeddings + local labels.
  B) bootstrap_me(): every call contains me + one other. Cluster all per-call
     speaker centroids; the cluster that spans the most distinct calls is me.
     (If that cluster is barely bigger than others, fall back to manual
      enrollment: `pipeline.py enroll-me <wav> [start end]`.)
  C) assign_call_labels(): the local speaker closest (cosine) to the me
     profile becomes who='me'; the other one who='other'.
  D) resolve_contacts(): 'other' utterances matched against the voiceprint
     library give contact_id + confidence; unknown voices get a new contact
     seeded from the filename hint (phone/name).
"""
from __future__ import annotations
import sqlite3
import numpy as np
from . import db


def _norm(e):
    return np.asarray(e, dtype=np.float32) / (np.linalg.norm(e) + 1e-9)


def _call_centroids(conn) -> dict:
    """{call_id: {local_label: (mean_emb, n)}} from stored segment embeddings."""
    out = {}
    rows = conn.execute(
        "SELECT call_id, spk_local, embedding FROM segments "
        "WHERE embedding IS NOT NULL").fetchall()
    acc = {}
    for r in rows:
        key = (r["call_id"], r["spk_local"])
        v = np.frombuffer(r["embedding"], dtype=np.float32)
        s, n = acc.get(key, (np.zeros_like(v), 0))
        acc[key] = (s + v, n + 1)
    for (cid, lab), (s, n) in acc.items():
        out.setdefault(cid, {})[lab] = (_norm(s / n), n)
    return out


def bootstrap_me(conn, thr: float = 0.55, min_calls: int = 5):
    """Auto-derive the 'me' voiceprint. Returns (ok, info)."""
    cents = _call_centroids(conn)
    if len(cents) < min_calls:
        return False, f"已转写通话不足 {min_calls} 通，暂不能自动识别「我」"
    items = []  # (call_id, label, centroid)
    for cid, d in cents.items():
        for lab, (c, n) in d.items():
            items.append((cid, lab, c))
    # greedy clustering by cosine similarity
    clusters = []  # [{ids:set, mean:np, n:int}]
    for cid, lab, c in items:
        placed = False
        for cl in clusters:
            sim = float(np.dot(c, cl["mean"]))
            if sim >= thr:
                m = cl["n"]
                cl["mean"] = _norm(cl["mean"] * m + c)
                cl["n"] = m + 1
                cl["calls"].add(cid)
                placed = True
                break
        if not placed:
            clusters.append({"mean": c, "n": 1, "calls": {cid}})
    clusters.sort(key=lambda cl: len(cl["calls"]), reverse=True)
    top = clusters[0]
    if len(top["calls"]) < max(3, int(len(cents) * 0.2)):
        return False, "声纹聚类无法确定稳定的「我」，请手动注册（enroll-me 命令）"
    emb = top["mean"].astype(np.float32).tobytes()
    conn.execute("INSERT INTO me_profile(id,mean_embedding,n_samples,updated_at) "
                 "VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                 "mean_embedding=excluded.mean_embedding,n_samples=excluded.n_samples,updated_at=excluded.updated_at",
                 (emb, len(top["calls"]), db.now()))
    conn.commit()
    return True, f"已自动识别「我」：覆盖 {len(top['calls'])} 通电话"


def set_me_from_wav(conn, engine, wav_path, start_ms=0, end_ms=0):
    """Manual enrollment from a wav (or a slice of it)."""
    from .funasr_engine import load_pcm16k, slice_ms
    pcm = load_pcm16k(wav_path)
    if end_ms > start_ms:
        pcm = slice_ms(pcm, start_ms, end_ms)
    emb = engine.embed_utterance(pcm)
    if emb is None:
        return False, "embedding 提取失败"
    v = _norm(emb)
    row = conn.execute("SELECT mean_embedding,n_samples FROM me_profile WHERE id=1").fetchone()
    if row:
        old = _norm(np.frombuffer(row["mean_embedding"], dtype=np.float32))
        n = row["n_samples"] or 1
        v = _norm(old * n + v)
        n += 1
    else:
        n = 1
    conn.execute("INSERT INTO me_profile(id,mean_embedding,n_samples,updated_at) VALUES(1,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET mean_embedding=excluded.mean_embedding,"
                 "n_samples=excluded.n_samples,updated_at=excluded.updated_at",
                 (v.astype(np.float32).tobytes(), n, db.now()))
    conn.commit()
    return True, f"已注册「我」的声纹（样本数 {n}）"


def get_me_embedding(conn):
    row = conn.execute("SELECT mean_embedding FROM me_profile WHERE id=1").fetchone()
    return _norm(np.frombuffer(row["mean_embedding"], dtype=np.float32)) if row else None


def assign_call_labels(conn, me_thr: float = 0.55):
    """For calls with speaker_map unset, label local speakers me/other by voiceprint."""
    me = get_me_embedding(conn)
    if me is None:
        return 0
    cents = _call_centroids(conn)
    n_done = 0
    import json
    for cid, d in cents.items():
        row = conn.execute("SELECT speaker_map FROM calls WHERE id=?", (cid,)).fetchone()
        if not row or row["speaker_map"]:
            continue
        sims = {lab: float(np.dot(c, me)) for lab, (c, n) in d.items()}
        if not sims:
            continue
        me_lab = max(sims, key=sims.get)
        if sims[me_lab] < me_thr:
            smap = {lab: "unknown" for lab in d}
        else:
            smap = {lab: ("me" if lab == me_lab else "other") for lab in d}
        conn.execute("UPDATE calls SET speaker_map=?,updated_at=? WHERE id=?",
                     (json.dumps(smap), db.now(), cid))
        for lab, who in smap.items():
            conn.execute("UPDATE segments SET who=? WHERE call_id=? AND spk_local=?",
                         (who, cid, lab))
        # short segments have no voiceprint (spk_local=-1): inherit nearest label
        unk = conn.execute("SELECT id,start_ms FROM segments WHERE call_id=? AND who='unknown' "
                           "ORDER BY start_ms", (cid,)).fetchall()
        known = conn.execute("SELECT id,start_ms,who FROM segments WHERE call_id=? "
                             "AND who IN ('me','other') ORDER BY start_ms", (cid,)).fetchall()
        if unk and known:
            for u in unk:
                near = min(known, key=lambda k: abs(k["start_ms"] - u["start_ms"]))
                conn.execute("UPDATE segments SET who=?, spk_local=? WHERE id=?",
                             (near["who"], near["who"] == "me" and "0" or "1", u["id"]))
        n_done += 1
    conn.commit()
    return n_done


def _contact_embedding(conn, cid):
    rows = conn.execute("SELECT embedding FROM voiceprints WHERE contact_id=?", (cid,)).fetchall()
    if not rows:
        return None
    acc = None
    for r in rows:
        v = np.frombuffer(r["embedding"], dtype=np.float32)
        acc = v if acc is None else acc + v
    return _norm(acc / len(rows))


def merge_same_person(conn, sim_thr: float = 0.8):
    """One person, several identities: merge duplicate contact profiles.
    Pass 1: same phone number (deterministic). Pass 2: near-identical
    voiceprint centroids but different numbers (>= sim_thr). The profile with
    more traffic survives; the loser's phone/name is kept as an alias note."""
    merged = 0
    tag = "同一个人其他号码/称呼: "

    def _parts(cid):
        return conn.execute("SELECT name,phone,note FROM contacts WHERE id=?",
                            (cid,)).fetchone()

    def _has_real_name(cid):
        r = _parts(cid)
        return bool(r and r["name"] and r["name"] != r["phone"])

    def _merge(keep_id, drop_id):
        nonlocal merged
        ra, rb = _parts(keep_id), _parts(drop_id)
        if not ra or not rb or keep_id == drop_id:
            return
        conn.execute("UPDATE segments SET contact_id=? WHERE contact_id=?",
                     (keep_id, drop_id))
        conn.execute("UPDATE voiceprints SET contact_id=? WHERE contact_id=?",
                     (keep_id, drop_id))
        # rebuild alias list: dedup, drop values equal to the survivor's own
        # name/phone, and repair notes written by earlier buggy merges
        aliases = []
        for part in (ra["note"] or "").split(" | "):
            if part.startswith(tag):
                aliases += [v for v in part[len(tag):].split(" / ") if v]
        aliases = [v for v in dict.fromkeys(aliases) if v not in (ra["phone"], ra["name"])]
        for v in (rb["phone"], rb["name"]):
            if v and v not in (ra["phone"], ra["name"]) and v not in aliases:
                aliases.append(v)
        plain = [p for p in (ra["note"] or "").split(" | ") if not p.startswith(tag)]
        note = " | ".join(plain + ([tag + " / ".join(aliases)] if aliases else []))
        conn.execute("UPDATE contacts SET note=?,updated_at=? WHERE id=?",
                     (note, db.now(), keep_id))
        conn.execute("DELETE FROM contacts WHERE id=?", (drop_id,))
        merged += 1

    # pass 1: identical phone -> same person, no voiceprint needed
    dups = conn.execute("SELECT phone FROM contacts WHERE phone IS NOT NULL AND phone<>'' "
                        "GROUP BY phone HAVING COUNT(*)>1").fetchall()
    for d in dups:
        rows = conn.execute("SELECT id FROM contacts WHERE phone=? "
                            "ORDER BY n_calls DESC, id ASC", (d["phone"],)).fetchall()
        keep = next((r["id"] for r in rows if _has_real_name(r["id"])), rows[0]["id"])
        for r in rows:
            if r["id"] != keep:
                _merge(keep, r["id"])

    # pass 2: same voiceprint, different numbers
    # 一次性预取（存在性 / n_calls / 有无真名），避免每对联系人都回表查 4 次
    cents = {c["id"]: _contact_embedding(conn, c["id"]) for c in
             conn.execute("SELECT id FROM contacts")}
    rows = {c["id"]: c for c in
            conn.execute("SELECT id,name,phone,n_calls FROM contacts")}

    def _named(rec):
        return bool(rec and rec["name"] and rec["name"] != rec["phone"])

    ids = sorted(i for i, v in cents.items() if v is not None and i in rows)
    for a in ids:
        if a not in rows:
            continue
        for b in ids:
            if b <= a or b not in rows:
                continue
            if float(np.dot(cents[a], cents[b])) < sim_thr:
                continue
            va, vb = _named(rows[a]), _named(rows[b])
            na = rows[a]["n_calls"] or 0
            nb = rows[b]["n_calls"] or 0
            # a profile with a real name beats a bare number; otherwise more traffic wins
            if va != vb:
                keep, drop = (a, b) if va else (b, a)
            else:
                keep, drop = (a, b) if na >= nb else (b, a)
            _merge(keep, drop)
            rows.pop(drop, None)
            if keep in cents:
                # 并档后声纹均值变了，不重算会让后面的比较用旧向量
                cents[keep] = _contact_embedding(conn, keep)
            if drop == a:
                break

    # a survivor that is just a number, but has a readable name in its
    # aliases: promote that name to the display name
    for r in conn.execute("SELECT id,name,phone,note FROM contacts "
                          "WHERE name=phone AND name IS NOT NULL").fetchall():
        aliases = []
        for part in (r["note"] or "").split(" | "):
            if part.startswith(tag):
                aliases = [v for v in part[len(tag):].split(" / ") if v]
        real = next((a for a in aliases if not a.isdigit()), None)
        if real:
            rest = [a for a in aliases if a not in (real, r["phone"])]
            plain = [p for p in (r["note"] or "").split(" | ") if not p.startswith(tag)]
            note = " | ".join(plain + ([tag + " / ".join(rest)] if rest else []))
            conn.execute("UPDATE contacts SET name=?,note=?,updated_at=? WHERE id=?",
                         (real, note, db.now(), r["id"]))

    conn.execute("UPDATE contacts SET n_calls=(SELECT COUNT(DISTINCT call_id) FROM segments "
                 "WHERE contact_id=contacts.id)")
    conn.commit()
    return merged


def resolve_contacts(conn, match_thr: float = 0.55, seed_quality: float = 0.6):
    """Map 'other' segments to contacts by voiceprint; grow the library."""
    #  contacts 只有几百行，但段有成千上万条：把 name/phone→id 和每通的文件名
    #    线索一次性拿进内存，别在段循环里逐条回表
    by_name, by_phone, name_of = {}, {}, {}
    for c in conn.execute("SELECT id,name,phone FROM contacts"):
        by_name.setdefault(c["name"], c["id"])
        name_of[c["id"]] = c["name"]
        if c["phone"]:
            by_phone.setdefault(c["phone"], c["id"])

    def _remember(cid, name, phone):
        by_name[name] = cid
        name_of[cid] = name
        if phone:
            by_phone.setdefault(phone, cid)
        return cid

    # 1) make sure filename hints have contact rows -- but never a second row
    #    for a phone we already have (that churn fought the same-person merge)
    for r in conn.execute(
            "SELECT DISTINCT contact_hint, phone FROM calls "
            "WHERE contact_hint IS NOT NULL OR phone IS NOT NULL"):
        label = r["contact_hint"] or r["phone"]
        if r["phone"]:
            ex_id = by_phone.get(r["phone"])
            if ex_id is not None:
                if label != r["phone"] and name_of.get(ex_id) == r["phone"] and label not in by_name:
                    conn.execute("UPDATE contacts SET name=?,updated_at=? WHERE id=?",
                                 (label, db.now(), ex_id))
                    by_name.pop(name_of[ex_id], None)
                    _remember(ex_id, label, r["phone"])
                continue
        if label in by_name:      # UNIQUE(name) 已存在，INSERT OR IGNORE 本来就是空操作
            continue
        cur = conn.execute("INSERT INTO contacts(name,phone,updated_at) VALUES(?,?,?)",
                           (label, r["phone"], db.now()))
        _remember(cur.lastrowid, label, r["phone"])
    conn.commit()

    cents = {cid: _contact_embedding(conn, cid) for cid in name_of}
    ids = [cid for cid, v in cents.items() if v is not None]
    mat = np.vstack([cents[cid] for cid in ids]) if ids else np.zeros((0, 1))

    hints = {r["id"]: (r["contact_hint"], r["phone"]) for r in conn.execute(
        "SELECT id, contact_hint, phone FROM calls")}

    segs = conn.execute(
        "SELECT id, call_id, embedding FROM segments "
        "WHERE who='other' AND embedding IS NOT NULL AND contact_id IS NULL").fetchall()
    me = get_me_embedding(conn)
    assigned = 0
    for s in segs:
        v = _norm(np.frombuffer(s["embedding"], dtype=np.float32))
        if me is not None and float(np.dot(v, me)) > 0.6:
            conn.execute("UPDATE segments SET who='me' WHERE id=?", (s["id"],))
            continue
        best, best_sim = None, -1
        if len(ids):
            sims = mat @ v
            j = int(np.argmax(sims))
            best, best_sim = ids[j], float(sims[j])
        call_hint, call_phone = hints.get(s["call_id"], (None, None))
        hint = call_hint or call_phone
        if best is not None and best_sim >= match_thr:
            conn.execute("UPDATE segments SET contact_id=?,contact_conf=? WHERE id=?",
                         (best, best_sim, s["id"]))
            assigned += 1
            # filename hint says another (known) contact -> trust text over voice
            hid = by_name.get(hint) if hint else None
            if hid is not None and hid != best:
                conn.execute("UPDATE segments SET contact_id=?,contact_conf=? WHERE id=?",
                             (hid, 0.95, s["id"]))
        elif hint:
            cid = by_name.get(hint)
            if cid is None:
                cid = by_phone.get(call_phone) if call_phone else None
            if cid is None:
                cur = conn.execute("INSERT INTO contacts(name,phone,updated_at) VALUES(?,?,?)",
                                   (hint, call_phone, db.now()))
                cid = _remember(cur.lastrowid, hint, call_phone)
            conn.execute("UPDATE segments SET contact_id=?,contact_conf=? WHERE id=?",
                         (cid, 0.9, s["id"]))
            if cid not in cents:
                cents[cid] = v
                ids.append(cid)
                mat = np.vstack([mat, v]) if len(ids) > 1 else v.reshape(1, -1)
            assigned += 1
        else:
            # brand new voice, no hint -> create 声纹联系人
            name = f"未知声纹{int(s['id'])}"
            cur = conn.execute("INSERT INTO contacts(name,note,updated_at) VALUES(?,?,?)",
                               (name, "由声纹聚类自动创建", db.now()))
            cid = _remember(cur.lastrowid, name, None)
            cents[cid] = v
            ids.append(cid)
            mat = np.vstack([mat, v]) if len(ids) > 1 else v.reshape(1, -1)
            conn.execute("UPDATE segments SET contact_id=?,contact_conf=? WHERE id=?",
                         (cid, 0.7, s["id"]))
            conn.execute("INSERT INTO voiceprints(contact_id,embedding,source_call_id,"
                         "source_segment_id,quality,created_at) VALUES(?,?,?,?,?,?)",
                         (cid, v.astype(np.float32).tobytes(), s["call_id"], s["id"],
                          0.6, db.now()))
            assigned += 1
    # grow library: add strong, new-ish embeddings per contact (cap per contact)
    for c in conn.execute("SELECT id FROM contacts").fetchall():
        have = conn.execute("SELECT COUNT(*) c FROM voiceprints WHERE contact_id=?",
                            (c["id"],)).fetchone()["c"]
        if have >= 8:
            continue
        extra = conn.execute(
            "SELECT id, call_id, embedding FROM segments WHERE contact_id=? "
            "AND embedding IS NOT NULL AND id NOT IN "
            "(SELECT source_segment_id FROM voiceprints WHERE source_segment_id IS NOT NULL) "
            "LIMIT ?", (c["id"], 8 - have)).fetchall()
        for e in extra:
            v = _norm(np.frombuffer(e["embedding"], dtype=np.float32))
            conn.execute("INSERT INTO voiceprints(contact_id,embedding,source_call_id,"
                         "source_segment_id,quality,created_at) VALUES(?,?,?,?,?,?)",
                         (c["id"], v.astype(np.float32).tobytes(), e["call_id"], e["id"],
                          0.5, db.now()))
    # short 'other' segments without embeddings: inherit the call's dominant contact
    conn.execute(
        "UPDATE segments SET contact_id = ("
        "  SELECT s2.contact_id FROM segments s2 WHERE s2.call_id=segments.call_id"
        "    AND s2.contact_id IS NOT NULL GROUP BY s2.contact_id"
        "    ORDER BY COUNT(*) DESC LIMIT 1),"
        " contact_conf=0.6 "
        "WHERE who='other' AND contact_id IS NULL AND embedding IS NULL "
        "AND EXISTS (SELECT 1 FROM segments s3 WHERE s3.call_id=segments.call_id"
        "            AND s3.contact_id IS NOT NULL)")
    conn.execute("UPDATE contacts SET n_calls=(SELECT COUNT(DISTINCT call_id) FROM segments "
                 "WHERE contact_id=contacts.id)")
    conn.commit()
    return assigned

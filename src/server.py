"""FastAPI cockpit: search / stats dashboard / call detail / voices / graph."""
from __future__ import annotations
import json, sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config, db as dbm

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

app = FastAPI(title="通话录音档案库")
_cfg = None


def cfg():
    global _cfg
    if _cfg is None:
        _cfg = config.load()
    return _cfg


def conn():
    c = sqlite3.connect(cfg()["db_path"], timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def dict_rows(rows):
    return [dict(r) for r in rows]


_schema_ready = False


def init_conn():
    global _schema_ready
    c = conn()
    c.row_factory = sqlite3.Row
    if not _schema_ready:              # one-time DDL/migration per process, not per request
        c.executescript(dbm.SCHEMA)
        dbm.migrate(c)
        c.commit()
        _schema_ready = True
    return c


# ---------- static ----------
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


# ---------- overview / stats ----------
@app.get("/api/overview")
def overview():
    c = init_conn()
    o = {}
    o["total_calls"] = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    o["done"] = c.execute("SELECT COUNT(*) FROM calls WHERE status IN ('transcribed','analyzed')").fetchone()[0]
    o["contacts"] = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    o["hours"] = round((c.execute("SELECT COALESCE(SUM(duration_sec),0) FROM calls").fetchone()[0]) / 3600, 2)
    o["first_call"] = c.execute("SELECT MIN(call_time) FROM calls").fetchone()[0]
    o["last_call"] = c.execute("SELECT MAX(call_time) FROM calls").fetchone()[0]
    return o


@app.get("/api/stats/timeline")
def timeline(bucket: str = Query("month", pattern="^(day|week|month|year)$")):
    c = init_conn()
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y"}[bucket]
    rows = c.execute(
        "SELECT strftime(?, call_time) k, COUNT(*) n, ROUND(SUM(duration_sec)/60.0,1) minutes"
        " FROM calls WHERE call_time IS NOT NULL GROUP BY k ORDER BY k", (fmt,)).fetchall()
    return [{"bucket": r[0], "calls": r[1], "minutes": r[2]} for r in rows if r[0]]


@app.get("/api/stats/heatmap")
def heatmap():
    """7x24 weekday/hour distribution."""
    c = init_conn()
    rows = c.execute(
        "SELECT strftime('%w', call_time) wd, strftime('%H', call_time) hh, COUNT(*) n"
        " FROM calls WHERE call_time IS NOT NULL GROUP BY wd, hh").fetchall()
    grid = [[0] * 24 for _ in range(7)]
    for wd, hh, n in rows:
        if wd is None or hh is None:
            continue
        grid[int(wd)][int(hh)] = n
    return grid


@app.get("/api/stats/contacts")
def stats_contacts(limit: int = 30, since: str = "", until: str = ""):
    """Top contacts by call attribution on the calls table itself
    (filename hint/phone), so it covers every scanned recording,
    not only voiceprint-aligned segments."""
    c = init_conn()
    w, params = ["1=1"], []
    if since:
        w.append("call_time>=?"); params.append(since)
    if until:
        w.append("call_time<=?"); params.append(until)
    rows = c.execute(
        f"SELECT COALESCE(NULLIF(contact_hint,''), NULLIF(phone,''), '未知') name,"
        f" COUNT(*) calls, ROUND(SUM(COALESCE(duration_sec,0))/60.0,1) minutes,"
        f" MIN(call_time) first, MAX(call_time) last"
        f" FROM calls WHERE {' AND '.join(w)} GROUP BY name"
        f" ORDER BY calls DESC LIMIT ?", (*params, limit)).fetchall()
    return dict_rows(rows)


@app.get("/api/stats/contact-timeline")
def contact_timeline(contact_id: int, bucket: str = Query("month", pattern="^(day|week|month)$")):
    c = init_conn()
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[bucket]
    rows = c.execute(
        "SELECT strftime(?, c.call_time) k, COUNT(DISTINCT c.id) n FROM segments s"
        " JOIN calls c ON c.id=s.call_id WHERE s.who='other' AND s.contact_id=?"
        " GROUP BY k ORDER BY k", (fmt, contact_id)).fetchall()
    return [{"bucket": r[0], "calls": r[1]} for r in rows if r[0]]


@app.get("/api/stats/duration")
def duration_hist():
    c = init_conn()
    rows = c.execute("SELECT duration_sec FROM calls WHERE duration_sec IS NOT NULL").fetchall()
    bins = [10, 30, 60, 120, 300, 600, 1800, 3600, 1e9]
    labels = ["<10s", "10-30s", "30s-1m", "1-2m", "2-5m", "5-10m", "10-30m", "30-60m", ">1h"]
    hist = [0] * len(labels)
    for (d,) in rows:
        for i, b in enumerate(bins):
            if d < b:
                hist[i] += 1
                break
    return [{"label": l, "count": h} for l, h in zip(labels, hist)]


# ---------- recording sources (外部录音库) ----------
_scan_job = {"running": False, "source_id": None, "message": ""}


def _seed_default_source(c):
    if c.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
        root = str((ROOT / cfg()["recordings_dir"]).resolve())
        c.execute("INSERT INTO sources(name,path,created_at) VALUES(?,?,?)",
                  ("本机", root, dbm.now()))
        c.execute("UPDATE calls SET source_id=(SELECT id FROM sources WHERE name='本机') "
                  "WHERE source_id IS NULL")
        c.commit()


@app.get("/api/sources")
def list_sources():
    c = init_conn()
    _seed_default_source(c)
    rows = c.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM calls x WHERE x.source_id=s.id) n_calls,"
        " (SELECT ROUND(SUM(COALESCE(x.duration_sec,0))/3600.0,2) FROM calls x WHERE x.source_id=s.id) hours,"
        " (SELECT COUNT(*) FROM calls x WHERE x.source_id=s.id AND x.status IN ('transcribed','analyzed')) n_done,"
        " (SELECT COUNT(*) FROM calls x WHERE x.source_id=s.id AND x.status='error') n_err,"
        " (SELECT COUNT(*) FROM calls x WHERE x.source_id=s.id AND x.duration_sec IS NULL) n_nodur"
        " FROM sources s ORDER BY s.id").fetchall()
    return dict_rows(rows)


@app.post("/api/sources")
def add_source(payload: dict):
    name = (payload.get("name") or "").strip()
    path = (payload.get("path") or "").strip().strip('"')
    if not name or not path:
        raise HTTPException(400, "需要 name 和 path")
    p = Path(path)
    if not p.exists():
        raise HTTPException(400, f"路径不存在或不可访问: {path}（NAS 请先挂载）")
    c = init_conn()
    try:
        c.execute("INSERT INTO sources(name,path,enabled,note,created_at) VALUES(?,?,?,?,?)",
                  (name, str(p.resolve()), int(payload.get("enabled", 1)),
                   payload.get("note", ""), dbm.now()))
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, f"已存在同名或同路径的库: {e}")
    c.commit()
    return {"ok": True}


@app.put("/api/sources/{sid}")
def update_source(sid: int, payload: dict):
    c = init_conn()
    row = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    fields, params = [], []
    for k in ("name", "path", "note"):
        if payload.get(k) is not None:
            fields.append(f"{k}=?"); params.append(str(payload[k]).strip().strip('"'))
    if payload.get("enabled") is not None:
        fields.append("enabled=?"); params.append(1 if payload["enabled"] else 0)
    if fields:
        c.execute(f"UPDATE sources SET {','.join(fields)} WHERE id=?", (*params, sid))
        c.commit()
    return {"ok": True}


@app.delete("/api/sources/{sid}")
def delete_source(sid: int, unlink: int = 0):
    c = init_conn()
    if not unlink:
        n = c.execute("SELECT COUNT(*) FROM calls WHERE source_id=?", (sid,)).fetchone()[0]
        if n:
            raise HTTPException(400, f"该库下还有 {n} 通通话。加 ?unlink=1 仅移除库记录（通话保留但不再归属）")
    c.execute("UPDATE calls SET source_id=NULL WHERE source_id=?", (sid,))
    c.execute("DELETE FROM sources WHERE id=?", (sid,))
    c.commit()
    return {"ok": True}


def _scan_thread(sid: int, path: str):
    import threading
    import traceback
    from . import scan as scanmod
    _scan_job.update(running=True, source_id=sid, message="扫描中…")
    try:
        a = cfg()
        c = sqlite3.connect(cfg()["db_path"])
        c.row_factory = sqlite3.Row
        res = scanmod.scan(c, path, a["audio_extensions"], a.get("filename_patterns"),
                           a.get("exclude_dirs"), source_id=sid)
        c.execute("UPDATE sources SET last_scan_at=?, last_result=? WHERE id=?",
                  (dbm.now(), json.dumps(res, ensure_ascii=False), sid))
        c.commit(); c.close()
        _scan_job.update(running=False,
                         message=f"新增 {res['added']}，更新 {res['updated']}，未变 {res['unchanged']}")
    except Exception as e:
        traceback.print_exc()
        _scan_job.update(running=False, message=f"扫描失败: {e}")


@app.post("/api/sources/{sid}/scan")
def scan_source(sid: int):
    if _scan_job["running"]:
        raise HTTPException(409, f"已有扫描任务在跑（库 #{_scan_job['source_id']}）：{_scan_job['message']}")
    c = init_conn()
    row = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if not Path(row["path"]).exists():
        raise HTTPException(400, f"路径当前不可访问: {row['path']}（NAS 是否已挂载？）")
    import threading
    threading.Thread(target=_scan_thread, args=(sid, row["path"]), daemon=True).start()
    return {"ok": True}


@app.get("/api/sources/scan-status")
def scan_status():
    return _scan_job


@app.get("/api/sources/{sid}/stats")
def source_stats(sid: int):
    c = init_conn()
    st = c.execute("SELECT status, COUNT(*) n FROM calls WHERE source_id=? GROUP BY status",
                   (sid,)).fetchall()
    return {r["status"]: r["n"] for r in st}


# ---------- search / detail ----------
@app.get("/api/calls")
def list_calls(q: str = "", contact: str = "", who: str = "", date_from: str = "",
               date_to: str = "", status: str = "", source: int = 0,
               page: int = 1, size: int = 25):
    c = init_conn()
    w, p = ["1=1"], []
    if q:
        w.append("(c.filename LIKE ? OR c.summary LIKE ? OR s.text_zh LIKE ?)")
        p += [f"%{q}%"] * 3
    if contact:
        w.append("(c.contact_hint LIKE ? OR c.phone LIKE ? OR ct.name LIKE ?)")
        p += [f"%{contact}%"] * 3
    if who:
        w.append("s.who=?"); p.append(who)
    if date_from:
        w.append("c.call_time>=?"); p.append(date_from)
    if date_to:
        w.append("c.call_time<=?"); p.append(date_to + "T23:59:59")
    if status:
        w.append("c.status=?"); p.append(status)
    if source:
        w.append("c.source_id=?"); p.append(source)
    where = " AND ".join(w)
    total = c.execute(
        f"SELECT COUNT(DISTINCT c.id) FROM calls c LEFT JOIN segments s ON s.call_id=c.id"
        f" LEFT JOIN contacts ct ON ct.id=s.contact_id WHERE {where}", p).fetchone()[0]
    rows = c.execute(
        f"SELECT c.id, c.filename, c.contact_hint, c.phone, c.call_time,"
        f" c.duration_sec, c.status, c.summary,"
        f" (SELECT GROUP_CONCAT(DISTINCT ct2.name) FROM segments s2"
        f"  JOIN contacts ct2 ON ct2.id=s2.contact_id"
        f"  WHERE s2.call_id=c.id AND s2.who='other') contact FROM calls c"
        f" LEFT JOIN segments s ON s.call_id=c.id"
        f" LEFT JOIN contacts ct ON ct.id=s.contact_id"
        f" WHERE {where} GROUP BY c.id ORDER BY c.call_time DESC, c.id DESC LIMIT ? OFFSET ?",
        (*p, size, (page - 1) * size)).fetchall()
    return {"total": total, "page": page, "size": size, "items": dict_rows(rows)}


@app.get("/api/calls/{call_id}")
def call_detail(call_id: int):
    c = init_conn()
    call = c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    if not call:
        raise HTTPException(404, "no such call")
    segs = c.execute(
        "SELECT s.id, s.call_id, s.idx, s.start_ms, s.end_ms, s.who, s.contact_id,"
        " s.text_zh, s.align_source, s.ts_confidence, ct.name contact_name FROM segments s"
        " LEFT JOIN contacts ct ON ct.id=s.contact_id WHERE s.call_id=? ORDER BY s.idx",
        (call_id,)).fetchall()
    out = dict(call)
    for k in ("embedding",):
        out.pop(k, None)
    out["segments"] = [dict(r) for r in segs]
    return out


@app.get("/api/media/{call_id}")
def media(call_id: int):
    c = init_conn()
    row = c.execute("SELECT path, wav_path FROM calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    for path in (row[0], row[1]):
        if path and Path(path).exists():
            return FileResponse(path)
    raise HTTPException(404, "文件不存在")


@app.get("/api/contacts")
def contacts(q: str = ""):
    c = init_conn()
    rows = c.execute(
        "SELECT ct.*, (SELECT COUNT(*) FROM segments s WHERE s.contact_id=ct.id AND s.who='other') n_seg"
        " FROM contacts ct WHERE ct.name LIKE ? OR ct.phone LIKE ? ORDER BY n_calls DESC",
        (f"%{q}%", f"%{q}%")).fetchall()
    return dict_rows(rows)


# ---------- voices / graph / progress ----------
@app.get("/api/voices")
def voices():
    root = Path(cfg()["voices_dir"])
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            meta = d / "meta.json"
            if d.is_dir() and meta.exists():
                m = json.loads(meta.read_text(encoding="utf-8"))
                m["dir"] = d.name
                for cl in m.get("clips", []):
                    cl["url"] = f"/api/voice-file/{d.name}/{cl['file']}"
                out.append(m)
    return out


@app.get("/api/voice-file/{name}/{fn}")
def voice_file(name: str, fn: str):
    p = (Path(cfg()["voices_dir"]) / name / fn).resolve()
    if not str(p).startswith(str(Path(cfg()["voices_dir"]).resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/graph")
def graph(since: str = "", until: str = ""):
    from . import graph as g
    return g.as_cytoscape(init_conn(), since or None, until or None)


@app.get("/api/events")
def events(limit: int = 200):
    c = init_conn()
    rows = c.execute(
        "SELECT e.*, c.filename FROM events e LEFT JOIN calls c ON c.id=e.call_id"
        " ORDER BY e.event_time DESC LIMIT ?", (limit,)).fetchall()
    return dict_rows(rows)


@app.get("/api/progress")
def progress():
    c = init_conn()
    st = c.execute("SELECT status, COUNT(*) FROM calls GROUP BY status").fetchall()
    return {r[0]: r[1] for r in st}

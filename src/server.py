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
    return sqlite3.connect(cfg()["db_path"])


def dict_rows(rows):
    return [dict(r) for r in rows]


def init_conn():
    c = conn()
    c.row_factory = sqlite3.Row
    c.executescript(dbm.SCHEMA)
    c.commit()
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
    c = init_conn()
    where, params = "s.who='other' AND s.contact_id IS NOT NULL", []
    if since:
        where += " AND c.call_time>=?"; params.append(since)
    if until:
        where += " AND c.call_time<=?"; params.append(until)
    rows = c.execute(
        f"SELECT ct.id, ct.name, COUNT(DISTINCT c.id) calls,"
        f" ROUND(SUM(DISTINCT c.duration_sec)/60.0,1) minutes,"
        f" MIN(c.call_time) first, MAX(c.call_time) last"
        f" FROM segments s JOIN calls c ON c.id=s.call_id JOIN contacts ct ON ct.id=s.contact_id"
        f" WHERE {where} GROUP BY ct.id ORDER BY calls DESC LIMIT ?", (*params, limit)).fetchall()
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


# ---------- search / detail ----------
@app.get("/api/calls")
def list_calls(q: str = "", contact: str = "", who: str = "", date_from: str = "",
               date_to: str = "", status: str = "", page: int = 1, size: int = 25):
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
    where = " AND ".join(w)
    total = c.execute(
        f"SELECT COUNT(DISTINCT c.id) FROM calls c LEFT JOIN segments s ON s.call_id=c.id"
        f" LEFT JOIN contacts ct ON ct.id=s.contact_id WHERE {where}", p).fetchone()[0]
    rows = c.execute(
        f"SELECT DISTINCT c.id, c.filename, c.contact_hint, c.phone, c.call_time,"
        f" c.duration_sec, c.status, c.summary, ct.name contact FROM calls c"
        f" LEFT JOIN segments s ON s.call_id=c.id"
        f" LEFT JOIN contacts ct ON ct.id=s.contact_id"
        f" WHERE {where} ORDER BY c.call_time DESC, c.id DESC LIMIT ? OFFSET ?",
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

"""Scan recordings dir -> populate `calls` table with parsed metadata."""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from typing import Optional

from . import db, naming


def iter_audio_files(root: Path, extensions: list, exclude_dirs: list) -> list:
    """Recursively find audio files, skipping excluded/hidden dirs and symlink loops."""
    skip = {d.lower() for d in exclude_dirs}
    found, seen = [], set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in skip and not d.startswith((".", "~"))
                       and not (dp / d).is_symlink()]
        for fn in filenames:
            if Path(fn).suffix.lower() in extensions:
                found.append(dp / fn)
    return sorted(found)


def scan(conn: sqlite3.Connection, recordings_dir: str, extensions: list,
          extra_patterns: Optional[list] = None,
          exclude_dirs: Optional[list] = None,
          source_id: Optional[int] = None,
          probe_duration: bool = True) -> dict:
    root = Path(recordings_dir).resolve()
    if not root.exists():
        raise SystemExit(f"录音目录不存在: {root}（请先在 config.yaml 的 recordings_dir 填写）")
    known = {r["path"]: r for r in conn.execute("SELECT id,path,size_bytes FROM calls")}
    added = updated = unchanged = 0
    for p in iter_audio_files(root, extensions, exclude_dirs or []):
        sp = str(p.resolve())
        st = p.stat()
        row = known.get(sp)
        if row and row["size_bytes"] == st.st_size:
            unchanged += 1
            continue
        info = naming.parse_filename(p.name, st.st_mtime, extra_patterns)
        dur = None
        if probe_duration:
            from . import audio
            dur = audio.probe_duration(sp)
        vals = dict(
            filename=p.name, contact_hint=info["name"], phone=info["phone"],
            call_time=info["call_time"], size_bytes=st.st_size, duration_sec=dur,
            status="pending", updated_at=db.now(),
        )
        if row:
            conn.execute("UPDATE calls SET filename=?,contact_hint=?,phone=?,call_time=?,"
                         "size_bytes=?,duration_sec=?,source_id=COALESCE(?,source_id),"
                         "status='pending',error=NULL,updated_at=? WHERE path=?",
                         (vals["filename"], vals["contact_hint"], vals["phone"],
                          vals["call_time"], vals["size_bytes"], vals["duration_sec"],
                          source_id, vals["updated_at"], sp))
            updated += 1
        else:
            conn.execute("INSERT INTO calls(path,filename,contact_hint,phone,call_time,"
                         "size_bytes,duration_sec,source_id,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (sp, vals["filename"], vals["contact_hint"], vals["phone"],
                          vals["call_time"], vals["size_bytes"], vals["duration_sec"],
                          source_id, "pending", vals["updated_at"]))
            added += 1
    conn.commit()
    if probe_duration:
        from . import audio
        pend = conn.execute("SELECT id,path FROM calls WHERE duration_sec IS NULL").fetchall()
        for r in pend:
            if Path(r["path"]).exists():
                d = audio.probe_duration(r["path"])
                if d:
                    conn.execute("UPDATE calls SET duration_sec=? WHERE id=?", (d, r["id"]))
        conn.commit()
    return {"added": added, "updated": updated, "unchanged": unchanged}


def stats(conn: sqlite3.Connection) -> dict:
    out = {}
    for r in conn.execute("SELECT status, COUNT(*) c FROM calls GROUP BY status"):
        out[r["status"]] = r["c"]
    return out

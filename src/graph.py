"""Build the relationship map / knowledge graph from archive contents.

Nodes: me, contacts (people), events (from LLM analysis), topics.
Edges: talked (me<->contact, weighted by calls), involves (contact<->event),
       about (event<->call), topic_of.
Everything is stored in SQLite so the web layer can slice by time range.
"""
from __future__ import annotations
import json, sqlite3
from . import db


def _node(conn, type_, ref_id, label):
    if ref_id is None:  # free-form node (topic): dedupe by label
        row = conn.execute("SELECT id FROM graph_nodes WHERE type=? AND label=?",
                           (type_, label)).fetchone()
        if row:
            return row[0]
        cur = conn.execute("INSERT INTO graph_nodes(type,ref_id,label) VALUES(?,?,?)",
                           (type_, None, label))
        return cur.lastrowid
    conn.execute(
        "INSERT INTO graph_nodes(type,ref_id,label) VALUES(?,?,?) "
        "ON CONFLICT(type,ref_id) DO UPDATE SET label=excluded.label",
        (type_, ref_id, label))
    return conn.execute("SELECT id FROM graph_nodes WHERE type=? AND ref_id=?",
                        (type_, ref_id)).fetchone()[0]


def build(conn: sqlite3.Connection, cfg: dict, with_llm: bool = False):
    conn.execute("DELETE FROM graph_nodes"); conn.execute("DELETE FROM graph_edges")
    conn.execute("DELETE FROM events")
    me_id = _node(conn, "me", None, "我")
    # person nodes from contacts
    contact_ids = {}
    for c in conn.execute("SELECT id,name,n_calls FROM contacts"):
        contact_ids[c["id"]] = _node(conn, "contact", c["id"], c["name"])
    # talked edges with stats
    for r in conn.execute(
            "SELECT s.contact_id cid, COUNT(*) n, MIN(c.call_time) t0, MAX(c.call_time) t1,"
            " SUM(c.duration_sec) dur FROM segments s JOIN calls c ON c.id=s.call_id"
            " WHERE s.who='other' AND s.contact_id IS NOT NULL GROUP BY s.contact_id"):
        dst = contact_ids.get(r["cid"])
        if not dst:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO graph_edges(src_node,dst_node,kind,weight,edge_time) "
            "VALUES(?,?,?,?,?)", (me_id, dst, "talked", r["n"] or 1, r["t1"] or r["t0"]))

    # events from LLM analysis
    n_ev = 0
    for c in conn.execute("SELECT id, call_time, analysis, contact_hint, phone FROM calls "
                          "WHERE analysis IS NOT NULL"):
        try:
            a = json.loads(c["analysis"])
        except (json.JSONDecodeError, TypeError):
            continue
        others = c["contact_hint"] or c["phone"] or "对方"
        parts = {others}
        for title in (a.get("events") or [])[:6]:
            title = str(title).strip()
            if not title:
                continue
            cur = conn.execute("INSERT INTO events(call_id,title,description,event_time,"
                               "participants,created_at) VALUES(?,?,?,?,?,?)",
                               (c["id"], title, a.get("summary", ""), c["call_time"],
                                json.dumps(sorted(parts), ensure_ascii=False), db.now()))
            ev_id = cur.lastrowid
            en = _node(conn, "event", ev_id, title)
            conn.execute("INSERT OR IGNORE INTO graph_edges(src_node,dst_node,kind,call_id,edge_time)"
                         " VALUES(?,?,?,?,?)", (me_id, en, "involves", c["id"], c["call_time"]))
            if c["contact_hint"] and c["contact_hint"] in {v["name"] for v in
                                                           conn.execute("SELECT name FROM contacts")}:
                cid = conn.execute("SELECT id FROM contacts WHERE name=?",
                                   (c["contact_hint"],)).fetchone()[0]
                conn.execute("INSERT OR IGNORE INTO graph_edges(src_node,dst_node,kind,call_id,edge_time)"
                             " VALUES(?,?,?,?,?)",
                             (contact_ids.get(cid), en, "involves", c["id"], c["call_time"]))
            n_ev += 1
        for tp in (a.get("topics") or [])[:6]:
            tn = _node(conn, "topic", None, str(tp))
            conn.execute("INSERT OR IGNORE INTO graph_edges(src_node,dst_node,kind,weight,call_id,edge_time)"
                         " VALUES(?,?,?,?,?,?)", (me_id, tn, "topic", 1, c["id"], c["call_time"]))
    conn.commit()
    nn = conn.execute("SELECT COUNT(*) c FROM graph_nodes").fetchone()["c"]
    ne = conn.execute("SELECT COUNT(*) c FROM graph_edges").fetchone()["c"]
    print(f"图谱：节点 {nn}，边 {ne}，事件 {n_ev}")


def as_cytoscape(conn, since=None, until=None):
    """Serialize graph to Cytoscape.js elements, optionally time-filtered."""
    where, params = "1=1", []
    if since:
        where += " AND (e.edge_time>=? OR e.edge_time IS NULL)"; params.append(since)
    if until:
        where += " AND (e.edge_time<=? OR e.edge_time IS NULL)"; params.append(until)
    nodes = []
    for n in conn.execute("SELECT * FROM graph_nodes"):
        deg = conn.execute(
            "SELECT COUNT(*) c FROM graph_edges e JOIN graph_nodes t"
            " ON (e.dst_node=t.id OR e.src_node=t.id)"
            " WHERE (e.src_node=? OR e.dst_node=?) AND " + where,
            (n["id"], n["id"], *params)).fetchone()["c"]
        if deg == 0 and n["type"] != "me":
            continue
        nodes.append({"data": {"id": str(n["id"]), "label": n["label"], "type": n["type"],
                               "deg": deg}})
    ids = {int(x["data"]["id"]) for x in nodes}
    edges = []
    for e in conn.execute("SELECT * FROM graph_edges e WHERE " + where, params):
        if e["src_node"] in ids and e["dst_node"] in ids:
            edges.append({"data": {"id": str(e["id"]), "source": str(e["src_node"]),
                                   "target": str(e["dst_node"]), "kind": e["kind"],
                                   "weight": e["weight"], "time": e["edge_time"],
                                   "call_id": e["call_id"]}})
    return {"nodes": nodes, "edges": edges}

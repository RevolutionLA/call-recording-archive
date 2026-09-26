"""Local LLM summarization/analysis via a local model server.

Ollama native (http://localhost:11434) and any OpenAI-compatible endpoint
(LM Studio http://localhost:1234/v1, vLLM, llama.cpp server) both work; the
flavour is auto-detected, or pinned with llm.api: ollama / openai. Produces
per-call Chinese summary + structured JSON (topics, action items, entities,
sentiment) used by the dashboard and the knowledge-graph builder.
"""
from __future__ import annotations
import json, sqlite3, time
import requests
from . import db

PROMPT = """你是通话录音分析助手。下面是「我」与「{other}」在 {when} 一通电话的转写（每行以 [我]/[{other}] 开头）。
请输出 JSON（不要其他文字），字段：
"summary": 不超过120字的中文摘要
"topics": [关键话题, 最多6个]
"todos": [双方约定的待办, 没有则空数组]
"people": [提到的其他人名]
"events": [本通电话中发生的具体事件, 简短动宾短语]
"sentiment": 整体情绪(一个词)
"importance": 1-5 整数

转写：
{transcript}
"""


_WHO = {}          # host -> "ollama" | "openai"，探测结果缓存，别每通都探


def _target(llm):
    """Return (kind, url). Ollama thinking models (qwen3.5) come back empty
    through their OpenAI-compat route unless think:false is passed, and that
    field only exists on the native /api/chat one — so prefer native whenever
    the host answers /api/version. Pin with llm.api: ollama|openai to skip it.

    OpenAI-compatible servers (LM Studio / vLLM / llama.cpp) mount their API
    UNDER the configured /v1 prefix, so the openai branch keeps it; only the
    Ollama native branch strips /v1 off before appending /api/chat."""
    raw = llm["base_url"].rstrip("/")
    root = raw[:-3] if raw.endswith("/v1") else raw   # 探测 / Ollama 原生用（去掉 /v1）
    guess = "openai" if raw.endswith("/v1") else "ollama"
    kind = llm.get("api")
    if kind not in ("ollama", "openai"):
        if root not in _WHO:
            try:
                r = requests.get(root + "/api/version", timeout=3)
                _WHO[root] = "ollama" if (r.ok and "version" in r.json()) else "openai"
            except Exception:
                # 服务没应答 ≠ 服务不是 Ollama。本机默认就是 …:11434/v1，冷启动时
                # 把猜的结果写进缓存，会把整轮摘要锁到 OpenAI 路由上，而思考模型在
                # 那条路上返回空正文 —— 所以这里只猜、不记，下一通仍然重探
                pass
        kind = _WHO.get(root, guess)
    if kind == "ollama":
        return kind, root + "/api/chat"
    return kind, raw + "/chat/completions"           # OpenAI 兼容挂在 /v1 下面，别剥


def chat(cfg, prompt, retries=2):
    llm = cfg["llm"]
    kind, url = _target(llm)
    payload = {
        "model": llm["model"],
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    if kind == "ollama":
        payload["think"] = False
        payload["options"] = {
            "temperature": llm.get("temperature", 0.3),
            "num_predict": llm.get("max_tokens", 1024),
        }
    else:
        payload["temperature"] = llm.get("temperature", 0.3)
        payload["max_tokens"] = llm.get("max_tokens", 1024)
    headers = {"Authorization": f"Bearer {llm.get('api_key') or 'none'}"}
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers,
                              timeout=llm.get("timeout", 180))
            r.raise_for_status()
            data = r.json()
            content = (data["message"]["content"] if kind == "ollama"
                       else data["choices"][0]["message"]["content"])
            # 空正文是「静默退化」的主要形态：思考模型被派到 OpenAI 兼容路由上就是
            # HTTP 200 + 空 content。当成失败抛出去，调用方记 error 并下一轮重跑，
            # 而不是写一条空摘要把这通标成 analyzed 从此不再回头
            if not (content or "").strip():
                raise RuntimeError(f"{kind} 路由返回空正文（{url}）")
            return content
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2 + attempt * 3)


def _extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        t = t[4:] if t.lower().startswith("json") else t
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except json.JSONDecodeError:
            pass
    return {"summary": text[:200], "raw": True}


def _name_map(conn):
    return {c["id"]: c["name"] for c in conn.execute("SELECT id,name FROM contacts")}


def transcript_text(conn, cid, names=None):
    rows = conn.execute(
        "SELECT who, contact_id, text_zh FROM segments WHERE call_id=? ORDER BY idx",
        (cid,)).fetchall()
    names = _name_map(conn) if names is None else names
    lines = []
    for r in rows:
        who = "我" if r["who"] == "me" else (names.get(r["contact_id"], "对方") if r["who"] == "other" else "某人")
        lines.append(f"[{who}] {r['text_zh']}")
    return "\n".join(lines)


def run_pending(conn: sqlite3.Connection, cfg: dict, limit: int = 0):
    if not cfg["llm"].get("enabled", True):
        print("llm.enabled=false，跳过摘要")
        return
    q = ("SELECT id, contact_hint, phone, call_time FROM calls "
         "WHERE status='transcribed' AND summary IS NULL ORDER BY id")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    print(f"待摘要通话: {len(rows)}")
    # 单卡 GPU 要和 Qwen3 精修轮转：一次把 1400 通跑完会把精修饿掉好几个小时
    cap = float(cfg["llm"].get("max_minutes", 30)) * 60
    t0 = time.time()
    names = _name_map(conn)      # 批量摘要时别每通重扫一遍 contacts
    done = fail = 0
    for i, r in enumerate(rows, 1):
        if time.time() - t0 > cap:
            print(f"  摘要让位：本轮已到 {cap/60:.0f} 分钟上限，"
                  f"剩余 {len(rows)-i+1} 通下一轮续跑", flush=True)
            break
        transcript = transcript_text(conn, r["id"], names)
        if not transcript.strip():
            conn.execute("UPDATE calls SET summary='(空)',status='analyzed',updated_at=? WHERE id=?",
                         (db.now(), r["id"]))
            conn.commit()
            continue
        other = r["contact_hint"] or r["phone"] or "对方"
        try:
            raw = chat(cfg, PROMPT.format(other=other, when=r["call_time"] or "未知时间",
                                          transcript=transcript[:6000]))
            data = _extract_json(raw)
            summary = data.get("summary", "")
            conn.execute("UPDATE calls SET summary=?,analysis=?,status='analyzed',"
                         "fulltext_zh=?,updated_at=? WHERE id=?",
                         (summary, json.dumps(data, ensure_ascii=False), transcript,
                          db.now(), r["id"]))
            done += 1
        except Exception as e:
            fail += 1
            conn.execute("UPDATE calls SET error=?,updated_at=? WHERE id=?",
                         (f"LLM: {e}"[:300], db.now(), r["id"]))
            print(f"  [{i}] call#{r['id']} 摘要失败: {e}")
        conn.commit()
        if i % 10 == 0 or i == len(rows):
            print(f"  摘要进度 {i}/{len(rows)}", flush=True)
    print(f"摘要完成 {done}，失败 {fail}")

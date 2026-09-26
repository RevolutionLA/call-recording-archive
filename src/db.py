"""SQLite schema & helpers for the call-recording archive."""
from __future__ import annotations
import sqlite3, json, time, os
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE NOT NULL,           -- 原始录音绝对路径
  filename TEXT,
  contact_hint TEXT,                   -- 文件名解析出的对方名字/号码
  phone TEXT,
  call_time TEXT,                      -- ISO 时间，解析或 mtime
  duration_sec REAL,
  size_bytes INTEGER,
  status TEXT DEFAULT 'pending',       -- pending|normalized|transcribed|summarized|error|skipped
  error TEXT,
  wav_path TEXT,                       -- 规范化 16k 单声道 wav
  lang TEXT,                           -- zh / en / mixed（检测）
  summary TEXT,                        -- LLM 摘要
  analysis TEXT,                       -- LLM 结构化分析 JSON
  fulltext_zh TEXT,                    -- 中文全文（检索用）
  fulltext_en TEXT,                    -- 英文全文（检索用）
  speaker_map TEXT,                    -- JSON {local_spk: global_speaker_id or 'me'/'other'}
  source_id INTEGER,                   -- FK sources.id（可空）
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  spk_local TEXT,                      -- 说话人分离的局部标签 '0'/'1'
  who TEXT,                            -- 'me' | 'other' | 'unknown'
  contact TEXT,                        -- other 时归属的联系人
  contact_id INTEGER,                  -- FK contacts.id（声纹匹配结果）
  contact_conf REAL,                   -- 声纹匹配置信度 0~1
  start_ms INTEGER, end_ms INTEGER,
  text_zh TEXT,                        -- 中文转写（SenseVoice/Qwen3-ASR 原文或译文）
  text_en TEXT,                        -- 英文转写/译文
  align_source TEXT,                   -- 时间戳来源: qwen3-aligner|funasr-timestamp|whisper
  ts_confidence REAL,                  -- 时间戳可信度（来自 aligner 分数等）
  embedding BLOB,                      -- 该段声纹 float32 向量(可空)
  FOREIGN KEY(call_id) REFERENCES calls(id)
);
CREATE INDEX IF NOT EXISTS idx_seg_call ON segments(call_id);
CREATE INDEX IF NOT EXISTS idx_seg_who ON segments(who);
CREATE INDEX IF NOT EXISTS idx_seg_contact ON segments(contact_id);

CREATE TABLE IF NOT EXISTS asr_outputs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  engine TEXT NOT NULL,                -- sensevoice|faster-whisper|qwen3-asr
  lang TEXT,
  text TEXT,
  words_json TEXT,                     -- [{w,s,e}] 词级时间戳
  model_path TEXT,
  created_at TEXT,
  UNIQUE(call_id, engine)
);

CREATE TABLE IF NOT EXISTS voiceprints(                      -- 声纹库：每人的参考声纹
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL,
  embedding BLOB NOT NULL,             -- float32 向量
  source_call_id INTEGER,
  source_segment_id INTEGER,
  quality REAL,                        -- 0~1，段长/清晰度启发
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_vp_contact ON voiceprints(contact_id);

CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,           -- 展示名（联系人/号码）
  phone TEXT,
  alias TEXT,                         -- JSON 别名列表（文件名中出现的其他写法）
  mean_embedding BLOB,                -- 该联系人声纹均值（供后续匹配）
  n_calls INTEGER DEFAULT 0,
  note TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS me_profile(
  id INTEGER PRIMARY KEY CHECK (id=1),
  mean_embedding BLOB,
  n_samples INTEGER DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events(                       -- 知识图谱：事件节点
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER,
  title TEXT,
  description TEXT,
  event_time TEXT,
  participants TEXT,                 -- JSON [contact,...]
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS graph_nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,                -- 'me' | 'contact' | 'event' | 'topic'
  ref_id INTEGER,
  label TEXT,
  UNIQUE(type, ref_id)
);
CREATE TABLE IF NOT EXISTS graph_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_node INTEGER NOT NULL,
  dst_node INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- 'talked' | 'involves' | 'mention' | 'related'
  weight REAL DEFAULT 1,
  call_id INTEGER,
  edge_time TEXT,
  UNIQUE(src_node, dst_node, kind, call_id)
);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS sources(                        -- 外部录音库（本机目录/NAS 挂载）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  path TEXT UNIQUE NOT NULL,
  enabled INTEGER DEFAULT 1,
  note TEXT,
  last_scan_at TEXT,
  last_result TEXT,                    -- JSON {added,updated,unchanged}
  created_at TEXT
);
"""

MIGRATIONS = [
    "ALTER TABLE calls ADD COLUMN source_id INTEGER",
]


def migrate(conn):
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # duplicate column
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)
    return conn


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default

"""Exit 0 if there are pending/normalized calls, 1 if the queue is empty.

判不出队列（老库还没迁移、库被锁住）时按"有活"退出 0：让挂机循环照常起一轮，
pipeline.py 自己会迁移建列，别在这里静默把自动摄取关掉。
"""
import os
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
import sqlite3, sys
try:
    n = sqlite3.connect("data/archive.db", timeout=30).execute(
        "SELECT COUNT(*) FROM calls WHERE status IN ('pending','normalized')"
        " AND dup_of IS NULL").fetchone()[0]
except sqlite3.Error as e:
    print(f"排队数读不出来（按有活处理）：{e}", file=sys.stderr)
    n = 1
sys.exit(0 if n else 1)

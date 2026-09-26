"""Exit 0 if there are pending/normalized calls, 1 if queue is empty."""
import os
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
import sqlite3, sys
n = sqlite3.connect("data/archive.db").execute(
    "SELECT COUNT(*) FROM calls WHERE status IN ('pending','normalized')").fetchone()[0]
sys.exit(0 if n else 1)

import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "borrowboard.db"

def connect():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    # 并发借出撞写锁时等待对方提交，再由唯一索引判定谁赢，而不是直接抛 500
    c.execute("PRAGMA busy_timeout=5000")
    return c

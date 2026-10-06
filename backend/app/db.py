import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=float(os.environ.get("SQLITE_TIMEOUT", "10")))
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={int(float(os.environ.get('SQLITE_TIMEOUT', '10')) * 1000)}")
    return c

@contextmanager
def read_conn():
    """只读连接：永远不会在业务表上留下写锁。"""
    c = connect()
    try:
        c.execute("PRAGMA query_only=ON")
        yield c
    finally:
        c.close()

@contextmanager
def write_tx():
    """整场一个写事务（BEGIN IMMEDIATE）。

    - 进入即拿 RESERVED 锁：下架/消费互斥，注入失败时消费不可能打到半 expired 的批；
    - 正常退出统一 commit；任何异常（含故障注入）都 rollback，
      调用方必须让异常继续向外抛，禁止吞掉后冒充成功。
    """
    c = connect()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        # 手工事务：关掉 Python sqlite3 的隐式 BEGIN，否则它会在第一条 DML 前
        # 再发一个 BEGIN，与我们的 BEGIN IMMEDIATE 冲突。
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

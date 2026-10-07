"""造数助手：给跨层不变量测例搭一个隔离世界。

一个 World = 一个临时 sqlite 文件 + 一个指向它的 FastAPI TestClient。
所有造数（item/loan 直插、HTTP 借出归还、抽 sqlite 实况、看板快照、
左右分栏与顶细条计数的映射）都收口在本文件，测例只描述场景与期望。

注意：这里不允许只问引擎要结论。snapshot() 每一层都独立取证——
纯函数结果来自 borrow_rules，分栏来自 /api/board 回包，
sqlite 实况直接 SELECT——测例再要求它们在同一世界里对齐。
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from starlette.testclient import TestClient

from app.engines.borrow_rules import can_lend, classify_loans
from app.main import app

TODAY = date.today().isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()
FUTURE = (date.today() + timedelta(days=30)).isoformat()


class World:
    def __init__(self, client: TestClient, db_file: Path):
        self.client = client
        self.db_file = db_file

    # ---- 直连 sqlite：造数与取证都不经 HTTP ----
    def raw(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_file)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def reset(self):
        """清空 startup 种子，每个场景从空世界开始。"""
        c = self.raw()
        c.execute("DELETE FROM loans")
        c.execute("DELETE FROM items")
        c.execute("DELETE FROM settings")
        c.commit()
        c.close()

    def add_item(self, title: str, owner: str = "老周",
                 status: str = "available", data_quality: str = "clean") -> int:
        c = self.raw()
        cur = c.execute(
            "INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
            (title, owner, status, data_quality))
        c.commit(); iid = cur.lastrowid; c.close()
        return iid

    def add_loan(self, item_id: int, borrower: str = "邻居甲",
                 status: str = "active", due_date: str = FUTURE) -> int:
        c = self.raw()
        cur = c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (item_id, borrower, status, due_date, "2026-01-01T00:00:00+00:00"))
        c.commit(); lid = cur.lastrowid; c.close()
        return lid

    def set_item_status(self, item_id: int, status: str):
        c = self.raw()
        c.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
        c.commit(); c.close()

    def remove_item(self, item_id: int):
        c = self.raw()
        c.execute("DELETE FROM loans WHERE item_id=?", (item_id,))
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
        c.commit(); c.close()

    def item_status(self, item_id: int) -> str:
        c = self.raw()
        s = c.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()["status"]
        c.close(); return s

    def count_loans(self, status: str | None = None) -> int:
        c = self.raw()
        if status is None:
            n = c.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"]
        else:
            n = c.execute("SELECT COUNT(*) n FROM loans WHERE status=?", (status,)).fetchone()["n"]
        c.close(); return n

    # ---- HTTP 借出 / 归还：回包原文留存，失败不抛 ----
    def lend(self, item_id: int, borrower: str = "邻居甲", due_date: str = FUTURE) -> dict:
        r = self.client.post(
            f"/api/items/{item_id}/lend",
            json={"borrower": borrower, "due_date": due_date})
        return {"http_status": r.status_code, "body": _safe_json(r)}

    def ret(self, loan_id: int) -> dict:
        r = self.client.post(f"/api/loans/{loan_id}/return", json={})
        return {"http_status": r.status_code, "body": _safe_json(r)}

    # ---- 决策瞬间：提交层即将喂给纯函数的两个入参 ----
    def decision(self, item_id: int) -> dict:
        c = self.raw()
        item = c.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        active = c.execute(
            "SELECT COUNT(*) n FROM loans WHERE item_id=? AND status='active'",
            (item_id,)).fetchone()["n"]
        c.close()
        status = item["status"] if item else None
        return {"item_status": status, "active_loans": active,
                "can_lend": can_lend(status, active)}

    # ---- 同一世界里各层同时取证 ----
    def snapshot(self) -> dict:
        board = self.client.get("/api/board").json()
        loans_view = self.client.get("/api/loans").json()

        c = self.raw()
        db_items = [dict(r) for r in c.execute("SELECT * FROM items ORDER BY id")]
        db_loans = [dict(r) for r in c.execute(
            "SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id ORDER BY loans.id")]
        c.close()

        # 纯函数层：用与 /api/board 同样的入参再算一遍
        cls = classify_loans(
            [{k: L.get(k) for k in ("id", "status", "due_date", "title", "borrower")}
             for L in db_loans], TODAY)
        engine_buckets = {k: len(v) for k, v in cls.items()}

        overdue_db = {L["id"] for L in db_loans
                      if L["status"] == "active" and L["due_date"] < TODAY}
        active_db = {L["id"] for L in db_loans if L["status"] == "active"}
        returned_db = {L["id"] for L in db_loans if L["status"] == "returned"}

        active_count_by_item = {}
        for L in db_loans:
            if L["status"] == "active":
                active_count_by_item[L["item_id"]] = active_count_by_item.get(L["item_id"], 0) + 1

        # 前端 Board.vue 的渲染：左栏 available，右栏先逾期后在借
        left_ids = [i["id"] for i in board["available"]]
        right_ids = [L["id"] for L in board["overdue"]] + [L["id"] for L in board["active"]]

        return {
            "board": board,
            "left_ids": left_ids,
            "right_ids": right_ids,
            "counts": board["counts"],
            "sqlite_items": db_items,
            "sqlite_loans": db_loans,
            "sqlite_available_ids": [i["id"] for i in db_items if i["status"] == "available"],
            "sqlite_active_ids": sorted(active_db),
            "sqlite_overdue_ids": sorted(overdue_db),
            "sqlite_returned_ids": sorted(returned_db),
            "sqlite_active_count_by_item": dict(sorted(active_count_by_item.items())),
            "engine_classify_counts": engine_buckets,
            "engine_bucket_ids": {
                "active": sorted(L["id"] for L in cls["active"]),
                "overdue": sorted(L["id"] for L in cls["overdue"]),
                "returned": sorted(L["id"] for L in cls["returned"]),
            },
            "http_loans_bucket_ids": {
                "active": sorted(L["id"] for L in loans_view["active"]),
                "overdue": sorted(L["id"] for L in loans_view["overdue"]),
                "returned": sorted(L["id"] for L in loans_view["returned"]),
            },
        }

    # ---- 并发：两笔借出同时打到同一物 ----
    def concurrent_lend_once(self, item_id: int, due_date: str = FUTURE) -> dict:
        barrier = threading.Barrier(2)
        results = []

        def call(borrower):
            # 开闸瞬间每个线程各自直读一次 items.status：留下"第二笔看到什么"的证据
            c0 = self.raw()
            saw_status = c0.execute(
                "SELECT status FROM items WHERE id=?", (item_id,)).fetchone()["status"]
            saw_active = c0.execute(
                "SELECT COUNT(*) n FROM loans WHERE item_id=? AND status='active'",
                (item_id,)).fetchone()["n"]
            c0.close()
            barrier.wait(timeout=5)
            r = self.client.post(
                f"/api/items/{item_id}/lend",
                json={"borrower": borrower, "due_date": due_date})
            results.append({"borrower": borrower, "saw_item_status": saw_status,
                            "saw_active_rows": saw_active,
                            "http_status": r.status_code, "body": _safe_json(r)})

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(call, "邻居甲")
            f2 = pool.submit(call, "邻居乙")
            f1.result(timeout=15); f2.result(timeout=15)
        results.sort(key=lambda x: x["borrower"])
        return {"calls": results}

    # ---- 落库层直证：两个事务都过了演算、抢着写 active ----
    def commit_layer_double_insert(self, item_id: int, due_date: str = FUTURE) -> str:
        """模拟两笔请求同时通过 can_lend 后的 INSERT 竞争。

        主线程 c1 先插不提交占住写锁；另一线程里自建连接 c2 抢插，
        会先挂在 busy_timeout 上；c1 提交后，c2 必须被部分唯一索引拒绝。
        c1 落的行随后清掉，只把 c2 收到的错误原文交回给测例。
        """
        sql = ("INSERT INTO loans(item_id,borrower,status,due_date,lent_at) "
               "VALUES (?,?, 'active', ?, '2026-10-07T00:00:00+00:00')")
        c1 = self.raw()
        c1.execute(sql, (item_id, "落库甲", due_date))
        err = {}

        def second():
            c2 = self.raw()  # sqlite 连接不得跨线程，必须在本线程内创建
            try:
                c2.execute(sql, (item_id, "落库乙", due_date))
                err["e"] = ""  # 居然写进去了：唯一索引不存在才会到这
            except sqlite3.IntegrityError as e:
                err["e"] = str(e)
            finally:
                c2.close()

        t = threading.Thread(target=second)
        t.start(); t.join(1.0)        # 等 c2 挂在写锁上
        c1.commit(); t.join(5.0)
        # 清掉 c1 的行，恢复本场景造数前的状态
        c1.execute("DELETE FROM loans WHERE item_id=? AND borrower='落库甲'", (item_id,))
        c1.commit()
        c1.close()
        return err.get("e", "<第二写竟然成功：唯一互斥未落地>")


def _safe_json(r):
    try:
        return r.json()
    except Exception:
        return r.text


@contextmanager
def world_session(tmp_path, monkeypatch):
    """DATA_DIR 指到临时目录，启动一次 app（跑 init_db），再清空种子。

    用法（测试文件里的 fixture）：
        @pytest.fixture
        def world(tmp_path, monkeypatch):
            with world_session(tmp_path, monkeypatch) as w:
                yield w
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_file = Path(str(tmp_path)) / "borrowboard.db"
    with TestClient(app) as client:
        w = World(client, db_file)
        w.reset()
        yield w

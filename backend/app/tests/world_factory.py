"""造数助手：为互斥/逾期跨层不变量测试铺"同一个世界"。

每个 World 实例对应一个独立 sqlite 文件 + 一个 TestClient，所有造数、HTTP
动作、各层读数都打在这同一个世界上。测试不准只看纯函数，必须把四层对齐：

  引擎层  app.engines.borrow_rules（can_lend / is_overdue / classify_loans）
  HTTP层  /api/board、/api/loans、/api/items/{id}/lend、/api/loans/{id}/return
  分栏层  Board.vue 的投影：左栏=available；右栏=[...overdue, ...active]；
          顶细条=board.counts 的 available / active / overdue
  sqlite  items.status 原值与 loans 各状态行数（逾期只是演算标签，不改库）

注意右栏是"在借 / 逾期"合栏，条数 = overdue + active；而顶细条的"在借"
计数只含非逾期 active。两个口径分别暴露，不能混算。
"""
from datetime import date, timedelta

from app.db import connect

TODAY = date.today().isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()
FUTURE = (date.today() + timedelta(days=365)).isoformat()


class World:
    def __init__(self, client):
        self.client = client

    # ---- 直接落库的造数原语（脏数据 / 残局只能绕过 HTTP 铺） ----
    def reset(self):
        """清空 startup 种子，让每个场景从空世界开始，id 从 1 起。"""
        c = connect()
        c.execute("DELETE FROM loans")
        c.execute("DELETE FROM items")
        c.execute("DELETE FROM sqlite_sequence WHERE name IN ('items','loans')")
        c.commit()
        c.close()

    def add_item(self, title, owner="老周", status="available", data_quality="clean"):
        c = connect()
        cur = c.execute(
            "INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
            (title, owner, status, data_quality))
        c.commit()
        iid = cur.lastrowid
        c.close()
        return iid

    def add_loan(self, item_id, borrower="邻居甲", status="active", due_date=FUTURE):
        c = connect()
        cur = c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (item_id, borrower, status, due_date, "2026-01-01T00:00:00+00:00"))
        c.commit()
        lid = cur.lastrowid
        c.close()
        return lid

    def set_item_status(self, item_id, status):
        c = connect()
        c.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
        c.commit()
        c.close()

    # ---- HTTP 动作，回包原样交回 (状态码, json) ----
    def lend_http(self, item_id, borrower="邻居乙", due_date=FUTURE):
        r = self.client.post(
            f"/api/items/{item_id}/lend",
            json={"borrower": borrower, "due_date": due_date})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def return_http(self, loan_id):
        r = self.client.post(f"/api/loans/{loan_id}/return", json={})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def board(self):
        return self.client.get("/api/board").json()

    def loans_classified(self):
        return self.client.get("/api/loans").json()

    # ---- 分栏层：严格照抄 Board.vue / App.vue 的投影 ----
    def panes(self, board=None):
        b = board or self.board()
        # Board.vue 右栏 v-for="[...board.overdue, ...board.active]"，逾期排前面
        right = [l["id"] for l in [*b["overdue"], *b["active"]]]
        return {
            "left_ids": [i["id"] for i in b["available"]],
            "right_loan_ids": right,
            "right_n": len(right),
            "top_counts": b["counts"],  # App.vue 顶细条：可借/在借/逾期
        }

    # ---- sqlite 层：绕过任何演算，抽库内原值 ----
    def item_status(self, item_id):
        c = connect()
        st = c.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()[0]
        c.close()
        return st

    def loan_ids_by_status(self, item_id=None, status="active"):
        c = connect()
        if item_id is None:
            rows = c.execute("SELECT id FROM loans WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = c.execute(
                "SELECT id FROM loans WHERE item_id=? AND status=? ORDER BY id",
                (item_id, status)).fetchall()
        c.close()
        return [r[0] for r in rows]

    def total_loans(self):
        c = connect()
        n = c.execute("SELECT COUNT(*) c FROM loans").fetchone()["c"]
        c.close()
        return n

    def loans_raw(self, item_id=None):
        """库内 loans 原始行（dict），供纯函数 classify_loans 独立演算。"""
        c = connect()
        if item_id is None:
            rows = [dict(r) for r in c.execute("SELECT * FROM loans ORDER BY id")]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM loans WHERE item_id=? ORDER BY id", (item_id,))]
        c.close()
        return rows

    def loan_status(self, loan_id):
        c = connect()
        st = c.execute("SELECT status FROM loans WHERE id=?", (loan_id,)).fetchone()[0]
        c.close()
        return st

    def active_count(self, item_id=None):
        return len(self.loan_ids_by_status(item_id, "active"))


def assert_same_world(row_name, actual, expected):
    """逐键核对同一世界里各层的读数；对不上就失败并印出表格键名。"""
    diffs = {
        k: {"实际": actual.get(k), "期望": v}
        for k, v in expected.items()
        if actual.get(k) != v
    }
    assert not diffs, (
        f"\n场景【{row_name}】跨层不变量对不上，以下表格键名不一致：\n"
        + "\n".join(f"  {k}: 实际={d['实际']!r}  期望={d['期望']!r}" for k, d in diffs.items())
    )

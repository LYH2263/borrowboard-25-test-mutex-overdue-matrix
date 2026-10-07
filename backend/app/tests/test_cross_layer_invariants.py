"""互斥与逾期的跨层不变量。

每一行场景都必须在【同一个世界】里对齐四层，任何一层对不上即失败并印出
表格键名：
  1. 纯函数引擎  can_lend / is_overdue / classify_loans，喂的是从 sqlite
     抽出的原始 items.status 与 loans 行，而不是测试自己臆造的入参；
  2. HTTP 回包   借出/归还的状态码与 detail，以及 /api/board、/api/loans；
  3. 左右分栏     Board.vue 左栏 available、右栏 overdue+active 的条数与排序，
     App.vue 顶细条 available/active/overdue 计数；
  4. sqlite      items.status 原值、loans 的 active/returned 实际行数。

逾期只是引擎演算标签：库里在借行 status 仍是 'active'，所以右栏能看到一物、
顶细条"在借"却为 0 —— 两个口径分别核，不许混。
"""
import threading

import pytest

from app.engines.borrow_rules import can_lend, classify_loans
from app.tests.world_factory import TODAY, PAST, FUTURE, assert_same_world

ROWS = [
    "空闲可放出",
    "已有在借则拒",
    "已还不算逾期",
    "应还早于今天的在借算逾期",
    "脏数据-无主不得挡电钻",
    "借还交错同一物唯一active",
    "并发两笔只留一种世界",
    "残局onloan无loan第二笔不得当可借",
]


def snapshot(w, iid):
    """从同一个世界抽四层快照。eng_* 吃的是 sqlite 原值。"""
    raw_status = w.item_status(iid)
    raw_active_n = w.active_count(iid)
    check = can_lend(raw_status, raw_active_n)
    cls = classify_loans(w.loans_raw(), TODAY)
    b = w.board()
    panes = w.panes(b)
    lc = w.loans_classified()
    return {
        # sqlite 层
        "sqlite_status": raw_status,
        "sqlite_active_ids": w.loan_ids_by_status(iid, "active"),
        "sqlite_returned_ids": w.loan_ids_by_status(iid, "returned"),
        "sqlite_total_loans": w.total_loans(),
        # 纯函数层（入参抽自库内原值）
        "eng_can_lend": check["ok"],
        "eng_reason": check["reason"],
        "eng_active_ids": [x["id"] for x in cls["active"]],
        "eng_overdue_ids": [x["id"] for x in cls["overdue"]],
        "eng_returned_ids": [x["id"] for x in cls["returned"]],
        # HTTP 层
        "http_left_ids": [i["id"] for i in b["available"]],
        "http_active_ids": [x["id"] for x in b["active"]],
        "http_overdue_ids": [x["id"] for x in b["overdue"]],
        "http_returned_ids": [x["id"] for x in lc["returned"]],
        "http_counts": b["counts"],
        # 分栏 / 顶细条层（投影照抄 Board.vue、App.vue）
        "pane_left_ids": panes["left_ids"],
        "pane_right_ids": panes["right_loan_ids"],
        "pane_right_n": panes["right_n"],
        "top_counts": panes["top_counts"],
    }


def assert_layers_agree(row, s):
    """与期望无关的横向不变量：四层两两对得上。失败印键名。"""
    pairs = [
        ("eng_active_ids", "http_active_ids"),
        ("eng_overdue_ids", "http_overdue_ids"),
        ("eng_returned_ids", "http_returned_ids"),
        ("http_left_ids", "pane_left_ids"),
        ("http_counts", "top_counts"),
    ]
    for a, b in pairs:
        # id 列表只比成员多重集：/api/loans 按 id DESC 而 classify 保库内顺序，
        # 排序不是本不变量；右栏"逾期排在在借之前"的顺序另由下一条专门核。
        assert sorted(s[a]) == sorted(s[b]), (
            f"场景【{row}】跨层不一致：{a}={s[a]!r} != {b}={s[b]!r}")
    # Board.vue: v-for="[...board.overdue, ...board.active]"，逾期在前（board 内保序）
    assert s["pane_right_ids"] == s["http_overdue_ids"] + s["http_active_ids"], (
        f"场景【{row}】右栏投影与逾期+在借不符：pane_right_ids={s['pane_right_ids']!r}")
    assert s["pane_right_n"] == len(s["pane_right_ids"]), (
        f"场景【{row}】右栏条数键 pane_right_n={s['pane_right_n']} 与列表不符")
    c = s["http_counts"]
    assert c == {"available": len(s["http_left_ids"]),
                 "active": len(s["http_active_ids"]),
                 "overdue": len(s["http_overdue_ids"])} , (
        f"场景【{row}】顶细条计数 http_counts={c} 与各栏行数不符")
    # sqlite 与引擎演算：active 行集合必须一致（逾期行在库里仍属 active）
    assert set(s["sqlite_active_ids"]) == set(s["eng_active_ids"]) | set(s["eng_overdue_ids"]), (
        f"场景【{row}】sqlite active 行与引擎 active+逾期 不一致")


def assert_envelope(row, got, want_code, want_body=None):
    code, body = got
    assert code == want_code, f"场景【{row}】HTTP 状态码：实际 {code} 期望 {want_code}，回包 {body}"
    if want_body is not None:
        assert body == want_body, f"场景【{row}】HTTP 回包：实际 {body} 期望 {want_body}"


def test_row_空闲可放出(world):
    w = world
    w.reset()
    drill = w.add_item("电钻", owner="老周")
    # 动作前：纯函数对库内原值判定可借
    assert can_lend(w.item_status(drill), w.active_count(drill))["ok"] is True

    assert_envelope("空闲可放出", w.lend_http(drill, borrower="甲"),
                    200, {"loan_id": 1})

    s = snapshot(w, drill)
    expect = {
        "sqlite_status": "on_loan", "sqlite_active_ids": [1],
        "sqlite_returned_ids": [], "sqlite_total_loans": 1,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [1], "eng_overdue_ids": [], "eng_returned_ids": [],
        "http_left_ids": [], "http_active_ids": [1], "http_overdue_ids": [],
        "http_returned_ids": [],
        "http_counts": {"available": 0, "active": 1, "overdue": 0},
        "pane_left_ids": [], "pane_right_ids": [1], "pane_right_n": 1,
        "top_counts": {"available": 0, "active": 1, "overdue": 0},
    }
    assert_layers_agree("空闲可放出", s)
    assert_same_world("空闲可放出", s, expect)


def test_row_已有在借则拒(world):
    w = world
    w.reset()
    drill = w.add_item("电钻")
    lid = w.add_loan(drill, borrower="甲", due_date=FUTURE)
    w.set_item_status(drill, "on_loan")
    before_total = w.total_loans()

    # 409 的 reason 必须与纯函数对同一库内状态的判定一字不差
    reason = can_lend(w.item_status(drill), w.active_count(drill))["reason"]
    assert_envelope("已有在借则拒", w.lend_http(drill, borrower="乙"),
                    409, {"detail": reason})
    assert reason == "item_not_available"

    s = snapshot(w, drill)
    expect = {
        # 互斥挡住：整单不写，库内行数原样
        "sqlite_status": "on_loan", "sqlite_active_ids": [lid],
        "sqlite_returned_ids": [], "sqlite_total_loans": before_total,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [lid], "eng_overdue_ids": [], "eng_returned_ids": [],
        "http_left_ids": [], "http_active_ids": [lid], "http_overdue_ids": [],
        "http_returned_ids": [],
        "http_counts": {"available": 0, "active": 1, "overdue": 0},
        "pane_left_ids": [], "pane_right_ids": [lid], "pane_right_n": 1,
        "top_counts": {"available": 0, "active": 1, "overdue": 0},
    }
    assert_layers_agree("已有在借则拒", s)
    assert_same_world("已有在借则拒", s, expect)


def test_row_已还不算逾期(world):
    w = world
    w.reset()
    drill = w.add_item("电钻")
    lid = w.add_loan(drill, borrower="甲", due_date=PAST)  # 应还日早于今天
    w.set_item_status(drill, "on_loan")

    assert_envelope("已还不算逾期", w.return_http(lid), 200, {"ok": True})

    s = snapshot(w, drill)
    expect = {
        "sqlite_status": "available", "sqlite_active_ids": [],
        "sqlite_returned_ids": [lid], "sqlite_total_loans": 1,
        "eng_can_lend": True, "eng_reason": "",
        "eng_active_ids": [], "eng_overdue_ids": [], "eng_returned_ids": [lid],
        "http_left_ids": [drill], "http_active_ids": [], "http_overdue_ids": [],
        "http_returned_ids": [lid],
        "http_counts": {"available": 1, "active": 0, "overdue": 0},
        "pane_left_ids": [drill], "pane_right_ids": [], "pane_right_n": 0,
        "top_counts": {"available": 1, "active": 0, "overdue": 0},
    }
    assert_layers_agree("已还不算逾期", s)
    assert_same_world("已还不算逾期", s, expect)


def test_row_在借且应还早于今天算逾期(world):
    w = world
    w.reset()
    drill = w.add_item("电钻")
    lid = w.add_loan(drill, borrower="甲", due_date=PAST)
    w.set_item_status(drill, "on_loan")

    s = snapshot(w, drill)
    expect = {
        # 库里仍是 active，逾期只是演算标签
        "sqlite_status": "on_loan", "sqlite_active_ids": [lid],
        "sqlite_returned_ids": [], "sqlite_total_loans": 1,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [], "eng_overdue_ids": [lid], "eng_returned_ids": [],
        "http_left_ids": [], "http_active_ids": [], "http_overdue_ids": [lid],
        "http_returned_ids": [],
        # 右栏有 1 行（在借/逾期合栏），顶细条"在借"却是 0
        "http_counts": {"available": 0, "active": 0, "overdue": 1},
        "pane_left_ids": [], "pane_right_ids": [lid], "pane_right_n": 1,
        "top_counts": {"available": 0, "active": 0, "overdue": 1},
    }
    assert_layers_agree("在借且应还早于今天算逾期", s)
    assert_same_world("在借且应还早于今天算逾期", s, expect)


def test_row_脏数据无主不得挡电钻(world):
    w = world
    w.reset()
    dirty = w.add_item("脏数据-无主", owner="", data_quality="dirty")
    drill = w.add_item("电钻", owner="老周")
    # 无主脏数据自身仍可借（data_quality 不参与资格），且不得波及电钻
    assert can_lend(w.item_status(dirty), w.active_count(dirty))["ok"] is True
    assert can_lend(w.item_status(drill), w.active_count(drill))["ok"] is True

    assert_envelope("脏数据-无主不得挡电钻", w.lend_http(drill, borrower="甲"),
                    200, {"loan_id": 1})

    s = snapshot(w, drill)
    expect = {
        "sqlite_status": "on_loan", "sqlite_active_ids": [1],
        "sqlite_returned_ids": [], "sqlite_total_loans": 1,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [1], "eng_overdue_ids": [], "eng_returned_ids": [],
        # 电钻借出后，左栏只剩无主脏数据一条
        "http_left_ids": [dirty], "http_active_ids": [1], "http_overdue_ids": [],
        "http_returned_ids": [],
        "http_counts": {"available": 1, "active": 1, "overdue": 0},
        "pane_left_ids": [dirty], "pane_right_ids": [1], "pane_right_n": 1,
        "top_counts": {"available": 1, "active": 1, "overdue": 0},
    }
    assert_layers_agree("脏数据-无主不得挡电钻", s)
    assert_same_world("脏数据-无主不得挡电钻", s, expect)
    # 无主物在 API 里 owner 为空串，前端才渲染成 '—'；后端不得替它改名
    titles = {i["id"]: (i["owner"], i["data_quality"]) for i in w.client.get("/api/items").json()}
    assert titles[dirty] == ("", "dirty")


def test_row_借还交错同一物不得两笔active(world):
    w = world
    w.reset()
    drill = w.add_item("电钻")

    l1 = w.lend_http(drill, borrower="甲")
    assert_envelope("交错#1", l1, 200, {"loan_id": 1})
    assert w.active_count(drill) == 1
    blocked = w.lend_http(drill, borrower="乙")  # 未还再借，拒
    assert_envelope("交错#2", blocked, 409, {"detail": "item_not_available"})
    assert w.active_count(drill) == 1
    assert_envelope("交错#3", w.return_http(1), 200, {"ok": True})
    assert w.active_count(drill) == 0
    l3 = w.lend_http(drill, borrower="丙")
    assert_envelope("交错#4", l3, 200, {"loan_id": 2})
    assert w.active_count(drill) == 1
    assert_envelope("交错#5", w.return_http(2), 200, {"ok": True})
    assert w.active_count(drill) == 0
    l5 = w.lend_http(drill, borrower="丁")
    assert_envelope("交错#6", l5, 200, {"loan_id": 3})

    s = snapshot(w, drill)
    expect = {
        "sqlite_status": "on_loan", "sqlite_active_ids": [3],
        "sqlite_returned_ids": [1, 2], "sqlite_total_loans": 3,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [3], "eng_overdue_ids": [], "eng_returned_ids": [1, 2],
        "http_left_ids": [], "http_active_ids": [3], "http_overdue_ids": [],
        "http_returned_ids": [2, 1],  # /api/loans 为 ORDER BY id DESC，成员与引擎一致、顺序相反
        "http_counts": {"available": 0, "active": 1, "overdue": 0},
        "pane_left_ids": [], "pane_right_ids": [3], "pane_right_n": 1,
        "top_counts": {"available": 0, "active": 1, "overdue": 0},
    }
    assert_layers_agree("借还交错同一物唯一active", s)
    assert_same_world("借还交错同一物唯一active", s, expect)


def test_row_并发两笔叠在电钻上只留一种世界(world):
    w = world
    w.reset()

    w.reset()

    # 每轮重置世界（id 从 1 起）：board 是全局视图，重置后全局视图恰为该物视图。
    # 连跑 5 轮屏障双发，把调度时序的不确定性压出来。
    for round_no in range(5):
        w.reset()
        drill = w.add_item("电钻")
        barrier = threading.Barrier(2)
        envelopes = {}

        def hit(tag, borrower):
            barrier.wait()  # 两笔同时打出
            envelopes[tag] = w.lend_http(drill, borrower=borrower)

        t1 = threading.Thread(target=hit, args=("A", "甲"))
        t2 = threading.Thread(target=hit, args=("B", "乙"))
        t1.start(); t2.start(); t1.join(); t2.join()

        codes = sorted(code for code, _ in envelopes.values())
        assert codes == [200, 409], (
            f"并发第{round_no}轮没有收敛成单一世界：{envelopes}")
        winner = next(body["loan_id"] for code, body in envelopes.values() if code == 200)
        losers = [(code, body) for code, body in envelopes.values() if code != 200]
        # 输家在写锁内必须读到 items.status='on_loan'，故挡因是状态而非计数：
        # 这正是"核第二笔同时打来时读到的 status"。
        assert losers == [(409, {"detail": "item_not_available"})], (
            f"并发第{round_no}轮输家读到的状态不对：{losers}")

        s = snapshot(w, drill)
        expect = {
            "sqlite_status": "on_loan", "sqlite_active_ids": [winner],
            "sqlite_returned_ids": [], "sqlite_total_loans": winner,
            "eng_can_lend": False, "eng_reason": "item_not_available",
            "eng_active_ids": [winner], "eng_overdue_ids": [], "eng_returned_ids": [],
            "http_left_ids": [], "http_active_ids": [winner],
            "http_overdue_ids": [], "http_returned_ids": [],
            "http_counts": {"available": 0, "active": 1, "overdue": 0},
            "pane_left_ids": [], "pane_right_ids": [winner], "pane_right_n": 1,
            "top_counts": {"available": 0, "active": 1, "overdue": 0},
        }
        assert_layers_agree(f"并发第{round_no}轮", s)
        assert_same_world(f"并发第{round_no}轮", s, expect)

        # 第二笔（以及任何后来者）再看可借栏：电钻不得被当成成功可借
        assert drill not in s["http_left_ids"]
        third_code, third_body = w.lend_http(drill, borrower="丙")
        assert (third_code, third_body) == (409, {"detail": "item_not_available"})
        assert w.active_count(drill) == 1


def test_row_残局onloan但loan未插入(world):
    w = world
    w.reset()
    drill = w.add_item("电钻")
    w.set_item_status(drill, "on_loan")  # 残局：状态翻了，loans 一行都没插
    assert w.total_loans() == 0

    # 第二笔先读可借栏：左栏不得把电钻列为可借，顶细条可借为 0
    board = w.board()
    assert drill not in [i["id"] for i in board["available"]]
    assert board["counts"]["available"] == 0

    # 纯函数吃库内原值：哪怕 active 计数为 0，status 已 on_loan 也必须拒
    check = can_lend(w.item_status(drill), w.active_count(drill))
    assert check == {"ok": False, "reason": "item_not_available"}

    before = w.total_loans()
    assert_envelope("残局第二笔", w.lend_http(drill, borrower="乙"),
                    409, {"detail": "item_not_available"})

    s = snapshot(w, drill)
    expect = {
        "sqlite_status": "on_loan", "sqlite_active_ids": [],
        "sqlite_returned_ids": [], "sqlite_total_loans": before,
        "eng_can_lend": False, "eng_reason": "item_not_available",
        "eng_active_ids": [], "eng_overdue_ids": [], "eng_returned_ids": [],
        # 残局真相：物标在借却无借据，两栏都看不到它
        "http_left_ids": [], "http_active_ids": [], "http_overdue_ids": [],
        "http_returned_ids": [],
        "http_counts": {"available": 0, "active": 0, "overdue": 0},
        "pane_left_ids": [], "pane_right_ids": [], "pane_right_n": 0,
        "top_counts": {"available": 0, "active": 0, "overdue": 0},
    }
    assert_layers_agree("残局onloan无loan第二笔不得当可借", s)
    assert_same_world("残局onloan无loan第二笔不得当可借", s, expect)


def test_表格行齐全():
    # 钉死场景清单，少一行即失败
    assert len(ROWS) == 8

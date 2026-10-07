"""互斥与逾期的跨层不变量测例。

每一行（场景）都在同一个临时世界里同时取五层证据并要求它们对上：

  纯函数层   app.engines.borrow_rules 的 can_lend / is_overdue / classify_loans
  HTTP 层    /api/items/{id}/lend、/api/loans/{id}/return 的真实回包（状态码+detail）
  分栏层     看板左栏 available、右栏 overdue+active 的条数与条目（对应 Board.vue）
  顶细条     counts.available / counts.active / counts.overdue（对应 App.vue 状态条）
  sqlite 层  直接 SELECT items.status 与 loans 中 active 行数

任何一键对不上即失败，并打印「场景行 / 阶段 / 键名 | 期望 | 实际」表格。
不允许只断言引擎资格公式——资格公式只是表里必须对上的其中一列；
所有分栏/顶条/HTTP 视角的期望值都强制抄 sqlite 已落地的实况。

造数与各层取证全部在同目录单独文件 world_factory.py。
"""
import json

import pytest

from app.engines.borrow_rules import can_lend, is_overdue
from app.tests.world_factory import FUTURE, PAST, TODAY, world_session


# ---------------------------------------------------------------------------
# 世界 fixture：每个场景一个全新临时库
# ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, monkeypatch):
    with world_session(tmp_path, monkeypatch) as w:
        yield w


# ---------------------------------------------------------------------------
# 对齐器：从一次看板快照抽出各层事实，强制它们以 sqlite 为准互相对齐
# ---------------------------------------------------------------------------
def layer_facts(snap: dict) -> dict:
    """同一世界、同一时刻，各层各自报上来的值。"""
    return {
        "分栏.左栏ids": sorted(snap["left_ids"]),
        "分栏.右栏ids": sorted(snap["right_ids"]),
        "顶条.可借数": snap["counts"]["available"],
        "顶条.在借数": snap["counts"]["active"],
        "顶条.逾期数": snap["counts"]["overdue"],
        "sqlite.可借ids": sorted(snap["sqlite_available_ids"]),
        "sqlite.active_ids": sorted(snap["sqlite_active_ids"]),
        "sqlite.逾期ids": sorted(snap["sqlite_overdue_ids"]),
        "sqlite.已还ids": sorted(snap["sqlite_returned_ids"]),
        "sqlite.每物active行数": snap["sqlite_active_count_by_item"],
        "引擎.active_ids": sorted(snap["engine_bucket_ids"]["active"]),
        "引擎.逾期ids": sorted(snap["engine_bucket_ids"]["overdue"]),
        "引擎.已还ids": sorted(snap["engine_bucket_ids"]["returned"]),
        "借还HTTP.active_ids": sorted(snap["http_loans_bucket_ids"]["active"]),
        "借还HTTP.逾期ids": sorted(snap["http_loans_bucket_ids"]["overdue"]),
        "借还HTTP.已还ids": sorted(snap["http_loans_bucket_ids"]["returned"]),
    }


def alignment_expectations(facts: dict) -> dict:
    """跨层不变量：左栏/右栏/顶条/引擎/HTTP 全部抄 sqlite 已落地的实况。"""
    return {
        # 左栏 == sqlite 里 status=available 的物
        "分栏.左栏ids": facts["sqlite.可借ids"],
        # 右栏（逾期先、在借后）== sqlite 里全部 active 借据（含逾期）
        "分栏.右栏ids": facts["sqlite.active_ids"],
        # 顶条可借 == sqlite 可借数
        "顶条.可借数": len(facts["sqlite.可借ids"]),
        # 顶条在借 = active 且未逾期；逾期单列
        "顶条.在借数": len(facts["sqlite.active_ids"]) - len(facts["sqlite.逾期ids"]),
        "顶条.逾期数": len(facts["sqlite.逾期ids"]),
        # 引擎分类与 /api/loans 回包都必须等于 sqlite 推导
        "引擎.active_ids": sorted(set(facts["sqlite.active_ids"]) - set(facts["sqlite.逾期ids"])),
        "引擎.逾期ids": facts["sqlite.逾期ids"],
        "引擎.已还ids": facts["sqlite.已还ids"],
        "借还HTTP.active_ids": sorted(set(facts["sqlite.active_ids"]) - set(facts["sqlite.逾期ids"])),
        "借还HTTP.逾期ids": facts["sqlite.逾期ids"],
        "借还HTTP.已还ids": facts["sqlite.已还ids"],
    }


def render_table(row, phase, mismatches):
    lines = [f"场景行「{row}」阶段「{phase}」对不上：",
             f"{'键名':<30} | {'期望':<30} | {'实际'}"]
    for key, exp, got in mismatches:
        lines.append(f"{key:<30} | {json.dumps(exp, ensure_ascii=False):<30} | "
                     f"{json.dumps(got, ensure_ascii=False)}")
    return "\n".join(lines)


def fail_table(row, phase, got: dict, expected: dict):
    mismatches = [(k, expected[k], got[k]) for k in expected if got.get(k) != expected[k]]
    if mismatches:
        pytest.fail("\n" + render_table(row, phase, mismatches), pytrace=False)


def expect_aligned(row, phase, snap, extra_expected=None, pre=None):
    """取一次快照，核对全部跨层不变量 + 本步额外期望 + 挡住前后零写入。"""
    facts = layer_facts(snap)
    expected = alignment_expectations(facts)
    if extra_expected:
        expected.update(extra_expected)

    # 互斥最终形态：任一物不得有两笔 active（提交层部分唯一索引兜底）
    max_active = max(facts["sqlite.每物active行数"].values(), default=0)
    facts["不变量.每物active至多一笔"] = max_active <= 1
    expected["不变量.每物active至多一笔"] = True
    # 顶条与分栏条数自洽
    facts["不变量.顶条可借等于左栏"] = facts["顶条.可借数"] == len(facts["分栏.左栏ids"])
    facts["不变量.顶条在借逾期等于右栏"] = (
        facts["顶条.在借数"] + facts["顶条.逾期数"] == len(facts["分栏.右栏ids"]))
    expected["不变量.顶条可借等于左栏"] = True
    expected["不变量.顶条在借逾期等于右栏"] = True

    # 被挡时期望列直接抄挡住前提交层已经落地的粒度：挡住后必须逐键相同
    if pre is not None:
        for key in ("sqlite.active_ids", "sqlite.可借ids", "sqlite.逾期ids",
                    "sqlite.已还ids", "sqlite.每物active行数"):
            facts[f"挡住前后.{key}"] = facts[key]
            expected[f"挡住前后.{key}"] = pre[key]

    fail_table(row, phase, facts, expected)


def expect_http(row, phase, res, want_status, want_detail):
    got = {"HTTP.status": res["http_status"], "HTTP.body": res["body"]}
    expected = {"HTTP.status": want_status, "HTTP.body": want_detail}
    fail_table(row, phase, got, expected)


def pure_decision(world, item_id, row, phase, want_ok, want_reason, want_status, want_active):
    """同一时刻独立问三遍：直读 sqlite 入参、提交层 decision、纯函数本身。"""
    d = world.decision(item_id)
    pure = can_lend(want_status, want_active)
    got = {
        "资格.sqlite读到status": d["item_status"],
        "资格.sqlite读到active行": d["active_loans"],
        "资格.提交层判定ok": d["can_lend"]["ok"],
        "资格.提交层判定reason": d["can_lend"]["reason"],
        "资格.纯函数ok": pure["ok"],
        "资格.纯函数reason": pure["reason"],
    }
    expected = {
        "资格.sqlite读到status": want_status,
        "资格.sqlite读到active行": want_active,
        "资格.提交层判定ok": want_ok,
        "资格.提交层判定reason": want_reason,
        "资格.纯函数ok": want_ok,
        "资格.纯函数reason": want_reason,
    }
    fail_table(row, phase, got, expected)
    return got


# ---------------------------------------------------------------------------
# 场景行
# ---------------------------------------------------------------------------
def row_free_available(world):
    """空闲可放出：电钻 available 且无 active，借出成功并全层迁移。"""
    row = "空闲可放出"
    drill = world.add_item("电钻", "老周")
    pure_decision(world, drill, row, "借出前", True, "", "available", 0)
    # 纯函数逾期基线（本行只借未来日，不应逾期）
    assert is_overdue(PAST, TODAY, "active") is True

    res = world.lend(drill, "邻居甲", FUTURE)
    assert res["http_status"] == 200
    loan_id = res["body"]["loan_id"]
    expect_aligned(row, "借出后", world.snapshot(), {
        "sqlite.可借ids": [],
        "sqlite.active_ids": [loan_id],
        "sqlite.每物active行数": {drill: 1},
        "顶条.可借数": 0, "顶条.在借数": 1, "顶条.逾期数": 0,
        "分栏.左栏ids": [], "分栏.右栏ids": [loan_id],
    })
    # 显式抽 sqlite：items.status
    status = world.item_status(drill)
    fail_table(row, "借出后", {"sqlite.items.status": status},
               {"sqlite.items.status": "on_loan"})


def row_already_on_loan_rejected(world):
    """已有在借则拒：物 on_loan 且已有 active，再借 409 且整单不写。"""
    row = "已有在借则拒"
    drill = world.add_item("电钻", "老周")
    lid = world.add_loan(drill, "邻居甲", "active", FUTURE)
    world.set_item_status(drill, "on_loan")

    pre = layer_facts(world.snapshot())
    pure_decision(world, drill, row, "被挡前", False, "item_not_available", "on_loan", 1)

    res = world.lend(drill, "邻居乙", FUTURE)
    expect_http(row, "被挡", res, 409, {"detail": "item_not_available"})

    # 挡住时整单不写：期望列抄挡住前提交层落地的粒度
    expect_aligned(row, "被挡后", world.snapshot(), {
        "sqlite.active_ids": [lid],
        "sqlite.可借ids": [],
        "sqlite.每物active行数": {drill: 1},
        "顶条.在借数": 1, "顶条.逾期数": 0,
    }, pre=pre)
    fail_table(row, "被挡后", {"sqlite.items.status": world.item_status(drill)},
               {"sqlite.items.status": "on_loan"})


def row_returned_not_overdue(world):
    """已还不算逾期：应还日在过去，returned 后不进逾期栏、顶条逾期为 0。"""
    row = "已还不算逾期"
    drill = world.add_item("电钻", "老周")
    lid = world.add_loan(drill, "邻居甲", "active", PAST)
    world.set_item_status(drill, "on_loan")
    assert is_overdue(PAST, TODAY, "active") is True

    res = world.ret(lid)
    expect_http(row, "归还", res, 200, {"ok": True})
    # 同一应还日，状态一改口纯函数立刻不算逾期
    assert is_overdue(PAST, TODAY, "returned") is False

    expect_aligned(row, "归还后", world.snapshot(), {
        "sqlite.可借ids": [drill],
        "sqlite.active_ids": [],
        "sqlite.逾期ids": [],
        "sqlite.已还ids": [lid],
        "分栏.左栏ids": [drill],
        "分栏.右栏ids": [],
        "顶条.可借数": 1, "顶条.在借数": 0, "顶条.逾期数": 0,
    })


def row_overdue(world):
    """应还早于今天的在借算逾期：落右栏逾期段、顶条逾期 1、引擎/HTTP 同步。"""
    row = "应还早于今天的在借算逾期"
    drill = world.add_item("电钻", "老周")
    lid = world.add_loan(drill, "邻居甲", "active", PAST)
    world.set_item_status(drill, "on_loan")
    assert is_overdue(PAST, TODAY, "active") is True
    assert is_overdue(FUTURE, TODAY, "active") is False

    snap = world.snapshot()
    expect_aligned(row, "看板取证", snap, {
        "sqlite.可借ids": [],
        "sqlite.active_ids": [lid],
        "sqlite.逾期ids": [lid],
        "分栏.左栏ids": [],
        "分栏.右栏ids": [lid],
        "顶条.可借数": 0, "顶条.在借数": 0, "顶条.逾期数": 1,
        "引擎.逾期ids": [lid],
        "借还HTTP.逾期ids": [lid],
    })
    # 右栏顺序：逾期必须排在在借之前（Board.vue: [...overdue, ...active]）
    assert snap["right_ids"] == [lid]
    assert snap["board"]["overdue"][0]["overdue"] is True


def row_dirty_ownerless_does_not_block_drill(world):
    """脏数据-无主不得挡电钻：无主脏物可借，电钻借出不受其影响。"""
    row = "脏数据-无主不得挡电钻"
    drill = world.add_item("电钻", "老周", "available", "clean")
    ghost = world.add_item("脏数据-无主", "", "available", "dirty")
    table = world.add_item("折叠桌", "小陈", "available", "clean")

    # 资格公式只认 status/active：空 owner 与 dirty 标记不进判定
    pure_decision(world, ghost, row, "脏物借出前", True, "", "available", 0)
    pure_decision(world, drill, row, "电钻借出前", True, "", "available", 0)

    r1 = world.lend(drill, "邻居甲", FUTURE)
    r2 = world.lend(ghost, "邻居乙", FUTURE)
    expect_http(row, "电钻借出", r1, 200, {"loan_id": r1["body"]["loan_id"]})
    expect_http(row, "无主脏物借出", r2, 200, {"loan_id": r2["body"]["loan_id"]})

    snap = world.snapshot()
    lid_drill, lid_ghost = sorted(snap["sqlite_active_ids"])
    expect_aligned(row, "两笔借出后", snap, {
        "sqlite.可借ids": [table],
        "sqlite.每物active行数": {drill: 1, ghost: 1},
        "顶条.可借数": 1, "顶条.在借数": 2, "顶条.逾期数": 0,
        "分栏.左栏ids": [table],
        "分栏.右栏ids": [lid_drill, lid_ghost],
    })
    statuses = {i["id"]: i["status"] for i in snap["sqlite_items"]}
    fail_table(row, "两笔借出后", {"sqlite.items.status各物": statuses},
               {"sqlite.items.status各物": {
                   drill: "on_loan", ghost: "on_loan", table: "available"}})


def row_interleave_single_active(world):
    """借出与归还交错后同一物不得两笔 active；被挡的一笔整单不写。"""
    row = "借出归还交错同一物唯一active"
    drill = world.add_item("电钻", "老周")

    s1 = world.lend(drill, "邻居甲", FUTURE)
    expect_http(row, "第一次借出", s1, 200, {"loan_id": s1["body"]["loan_id"]})
    lid1 = s1["body"]["loan_id"]
    expect_aligned(row, "第一次借出", world.snapshot(),
                   {"sqlite.每物active行数": {drill: 1}, "顶条.在借数": 1})

    pre = layer_facts(world.snapshot())
    s2 = world.lend(drill, "邻居乙", FUTURE)
    expect_http(row, "在借期间再借", s2, 409, {"detail": "item_not_available"})
    expect_aligned(row, "再借被挡", world.snapshot(), {
        "sqlite.active_ids": [lid1], "顶条.在借数": 1}, pre=pre)

    r = world.ret(lid1)
    expect_http(row, "归还", r, 200, {"ok": True})
    expect_aligned(row, "归还后", world.snapshot(), {
        "sqlite.active_ids": [], "sqlite.已还ids": [lid1],
        "sqlite.可借ids": [drill], "顶条.在借数": 0})

    s3 = world.lend(drill, "邻居丙", FUTURE)
    expect_http(row, "归还后再借", s3, 200, {"loan_id": s3["body"]["loan_id"]})
    lid3 = s3["body"]["loan_id"]
    snap = world.snapshot()
    expect_aligned(row, "再次借出", snap, {
        "sqlite.active_ids": [lid3],
        "sqlite.已还ids": [lid1],
        "sqlite.每物active行数": {drill: 1},
        "顶条.在借数": 1,
    })
    # 被挡那笔不留任何借据：全表只有 lid1（已还）与 lid3（active）
    all_ids = sorted(L["id"] for L in snap["sqlite_loans"])
    fail_table(row, "再次借出", {"sqlite.全部loan_ids": all_ids},
               {"sqlite.全部loan_ids": sorted([lid1, lid3])})


def row_concurrent_double_lend(world):
    """并发：两笔借出叠在电钻上，只允许留下一种世界（一笔 200、一笔 409）。"""
    row = "并发两笔借出只留一种世界"

    # 1) 提交层直证：两个事务都通过演算后抢插 active，第二写必须撞部分唯一索引
    probe = world.add_item("电钻-落库层探针", "老周")
    err = world.commit_layer_double_insert(probe, FUTURE)
    assert err and "UNIQUE" in err.upper(), \
        f"部分唯一索引未在提交层兜底，第二写结果：{err!r}"
    # 探针验证本身不得留行，验证完删掉该物
    fail_table(row, "落库层探针", {
        "sqlite.probe残留active": world.snapshot()["sqlite_active_ids"],
        "sqlite.probe物状态": world.item_status(probe),
    }, {"sqlite.probe残留active": [], "sqlite.probe物状态": "available"})
    world.remove_item(probe)

    # 2) HTTP 层真实并发：连开三轮，每轮两笔同时打到一把新鲜电钻
    for round_no in range(3):
        drill = world.add_item(f"电钻-并发{round_no}", "老周")
        pure_decision(world, drill, row, f"第{round_no}轮开闸前", True, "", "available", 0)
        pre = layer_facts(world.snapshot())

        out = world.concurrent_lend_once(drill, FUTURE)
        statuses = sorted(c["http_status"] for c in out["calls"])
        fail_table(row, f"第{round_no}轮HTTP",
                   {"并发.两笔状态码": statuses},
                   {"并发.两笔状态码": [200, 409]})

        winner = next(c for c in out["calls"] if c["http_status"] == 200)
        loser = next(c for c in out["calls"] if c["http_status"] == 409)
        snap = world.snapshot()

        # 输家整单不写：该物只有一笔 active，且借用人正是赢家；物状态 on_loan
        active_rows = [L for L in snap["sqlite_loans"]
                       if L["item_id"] == drill and L["status"] == "active"]
        all_borrowers = [L["borrower"] for L in snap["sqlite_loans"] if L["item_id"] == drill]
        fail_table(row, f"第{round_no}轮收敛", {
            "并发.该物active笔数": len(active_rows),
            "并发.active借用人": [L["borrower"] for L in active_rows],
            "并发.输家有无借据": all_borrowers,
            "sqlite.items.status": world.item_status(drill),
            "并发.赢家回包": winner["body"],
        }, {
            "并发.该物active笔数": 1,
            "并发.active借用人": [winner["borrower"]],
            "并发.输家有无借据": [winner["borrower"]],
            "sqlite.items.status": "on_loan",
            "并发.赢家回包": {"loan_id": active_rows[0]["id"]},
        })

        # 除「本物恰好多出一笔 active、本物 available→on_loan」外，世界无其他变化
        expect_aligned(row, f"第{round_no}轮总账", snap, {
            "sqlite.可借ids": sorted(set(pre["sqlite.可借ids"]) - {drill}),
            "顶条.在借数": round_no + 1,
            "顶条.可借数": 0,
            "顶条.逾期数": 0,
        })

        # 核第二笔同时打来时读到的 status。赢家事务对 loan 插入与 item
        # 翻状态是一次 commit，所以探针那一拍只可能读到两种一致世界之一：
        #   (available, 0) —— 赢家尚未提交，第二笔演算放行后撞唯一索引 → already_on_loan
        #   (on_loan , 1) —— 赢家已提交，第二笔演算被 status 挡住     → item_not_available
        # 探针读与端点内部 SELECT 是相邻两拍，故只约束探针自身一致、
        # detail 合法，二者组合允许上述任一世界，最终收敛只有一种。
        assert loser["body"] in ({"detail": "already_on_loan"}, {"detail": "item_not_available"})
        assert (loser["saw_item_status"], loser["saw_active_rows"]) in (
            ("available", 0), ("on_loan", 1))
        assert (winner["saw_item_status"], winner["saw_active_rows"]) in (
            ("available", 0), ("on_loan", 1))
        # 赢家必须是在自己读到 (available,0) 的那个世界里抢先落地的
        assert winner["saw_item_status"] == "available" or winner["saw_active_rows"] == 0

    # 三轮总账：三个并发物各自恰好一笔 active，没有任何物两笔
    snap = world.snapshot()
    expect_aligned(row, "并发收尾总账", snap, {
        "顶条.在借数": 3, "顶条.逾期数": 0, "顶条.可借数": 0,
        "sqlite.每物active行数": {i: 1 for i in
                                 (L["item_id"] for L in snap["sqlite_loans"]
                                  if L["status"] == "active")},
    })


def row_broken_status_without_loan(world):
    """残局：items.status 已 on_loan 但 loans 未插入。

    可借栏不得把它当成成功可借（左栏没有它），第二笔 HTTP 必须 409 且整单不写；
    右栏/顶条在借也不得凭 status 虚构出借据。
    """
    row = "残局status_on_loan但loan缺失"
    drill = world.add_item("电钻", "老周")
    world.set_item_status(drill, "on_loan")  # 故意不插 loan

    snap0 = world.snapshot()
    expect_aligned(row, "残局看板", snap0, {
        "sqlite.可借ids": [],
        "sqlite.active_ids": [],
        "分栏.左栏ids": [],
        "分栏.右栏ids": [],
        "顶条.可借数": 0, "顶条.在借数": 0, "顶条.逾期数": 0,
    })
    pre = layer_facts(snap0)
    # 第二笔读到：status 已 on_loan、active 行 0 —— 资格公式先被 status 挡住
    pure_decision(world, drill, row, "第二笔读取", False, "item_not_available", "on_loan", 0)

    res = world.lend(drill, "邻居乙", FUTURE)
    expect_http(row, "第二笔借出", res, 409, {"detail": "item_not_available"})

    expect_aligned(row, "第二笔被挡后", world.snapshot(), {
        "sqlite.可借ids": [],
        "sqlite.active_ids": [],
        "分栏.左栏ids": [],
        "顶条.可借数": 0,
        "顶条.在借数": 0,
    }, pre=pre)
    fail_table(row, "第二笔被挡后", {
        "sqlite.items.status": world.item_status(drill),
        "sqlite.loans总行数": world.count_loans(),
    }, {"sqlite.items.status": "on_loan", "sqlite.loans总行数": 0})


ROWS = {
    "空闲可放出": row_free_available,
    "已有在借则拒": row_already_on_loan_rejected,
    "已还不算逾期": row_returned_not_overdue,
    "应还早于今天的在借算逾期": row_overdue,
    "脏数据-无主不得挡电钻": row_dirty_ownerless_does_not_block_drill,
    "借出归还交错同一物唯一active": row_interleave_single_active,
    "并发两笔借出只留一种世界": row_concurrent_double_lend,
    "残局status_on_loan但loan缺失": row_broken_status_without_loan,
}


@pytest.mark.parametrize("row_name", list(ROWS), ids=list(ROWS))
def test_mutex_overdue_cross_layer(world, row_name):
    ROWS[row_name](world)

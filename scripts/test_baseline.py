#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基线选取逻辑单元测试（pick_baseline）"""
import sys, importlib.util

spec = importlib.util.spec_from_file_location(
    "bx", "/home/mjx/repos/xubao-kanban/scripts/build_xubao.py")
bx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bx)

PERIOD = "26-27学年 秋下续寒春"


def mk(updated, rate=50.0, period=PERIOD):
    return (updated, {
        "meta": {"updated": updated, "completeRate": rate, "period": period},
        "subjects": [], "groups": [], "classTeachers": [],
    })


def case(name, candidates, cur_updated, cur_period, expect_updated):
    m, d = bx.pick_baseline(candidates, cur_updated, cur_period)
    got = m.get("updated") if m else None
    ok = got == expect_updated
    print(f"{'✅' if ok else '❌'} {name}: 期望 {expect_updated}, 实得 {got}")
    return ok


results = []
# 1. 同一天多个版本 → 取最新的不同时间戳
results.append(case(
    "同天取最新不同时间戳",
    [mk("2026-11-20 15:00"), mk("2026-11-20 14:00"), mk("2026-11-20 12:00")],
    "2026-11-20 16:00", PERIOD, "2026-11-20 15:00"))

# 2. 当前版本就是最新 → 跳过自己
results.append(case(
    "跳过当前版本(自己)",
    [mk("2026-11-20 14:00"), mk("2026-11-20 12:00")],
    "2026-11-20 14:00", PERIOD, "2026-11-20 12:00"))

# 3. 同天无其他 → 退回前一天
results.append(case(
    "同天无其他则退回前一天",
    [mk("2026-11-19 15:00"), mk("2026-11-19 12:00")],
    "2026-11-20 16:00", PERIOD, "2026-11-19 15:00"))

# 4. 周期不一致 → 跳过
results.append(case(
    "跨周期跳过",
    [mk("2026-11-20 15:00", period="26-27学年 春夏续"),
     mk("2026-11-20 14:00")],
    "2026-11-20 16:00", PERIOD, "2026-11-20 14:00"))

# 5. 只有自己 → 无基线
results.append(case(
    "只有自己则无基线",
    [mk("2026-11-20 16:00")],
    "2026-11-20 16:00", PERIOD, None))

# 6. 同一天、同时间戳但多个 commit（重复发布）→ 应跳过同时间戳
results.append(case(
    "跳过重复时间戳",
    [mk("2026-11-20 14:00"), mk("2026-11-20 14:00"), mk("2026-11-20 12:00")],
    "2026-11-20 14:00", PERIOD, "2026-11-20 12:00"))

# 7. 顺序反了（新→旧），应仍取最新
results.append(case(
    "候选顺序为新到旧",
    [mk("2026-11-20 15:00"), mk("2026-11-20 14:00")],
    "2026-11-20 15:00", PERIOD, "2026-11-20 14:00"))

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板 · 教学组战报生成器（markdown 表格版）

结构：
  1. 头部：组整体关键指标
  2. 伙伴明细表（按今日新增降序），含各年级今日新增列

口径（2026-11-24 用户要求）：
  ⚠️ **不再计算时段增长**（不再跟上一版数据做差）
  → 只展示「今日新增」= 底表的「续报人次日增 / 联报人次日增」逐行取 max 后求和
  → 增量对比（时段增长）相关的 prev facts / delta 逻辑全部移除

年级维度按「班主任 × 年级」行级事实聚合（口径与看板筛选器一致）。

用法：
  python3 scripts/gen_group_report.py 缪家新
  python3 scripts/gen_group_report.py 缪家新 --json
"""

import os
import sys
import json
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
DATA_FILE = os.path.join(REPO_DIR, "xubao_data.json")

BOARD_URL = "https://mjx0217.github.io/xubao-kanban/"
GRADE_ORDER = {"初一": 0, "初二": 1, "初三": 2}


def load():
    with open(DATA_FILE, encoding="utf-8") as f:
        cur = json.load(f)
    return cur


def agg_by_grade(facts, fields, ctids):
    """按 ctid → grade 聚合行级事实 → {ctid: {grade: [inClass, lianbao, daily, target]}}"""
    i = {k: n for n, k in enumerate(fields)}
    out = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
    for r in facts:
        if r[i["ctid"]] not in ctids:
            continue
        a = out[r[i["ctid"]]][r[i["grade"]]]
        a[0] += r[i["inClass"]]
        a[1] += r[i["lianbao"]]
        a[2] += r[i["daily"]]
        a[3] += r[i["target"]]
    return {k: dict(v) for k, v in out.items()}


def rate(lb, tg):
    return (lb / tg * 100) if tg else 0.0


def build_report(group_name):
    cur = load()
    meta = cur["meta"]

    grp = next((g for g in cur["groups"] if g["name"] == group_name), None)
    if grp is None:
        raise SystemExit(f"❌ 找不到教学组「{group_name}」")
    cts = [c for c in cur["classTeachers"] if c["group"] == group_name]
    ctids = {c["ctid"] for c in cts}
    ctmap = {c["ctid"]: c for c in cts}

    cur_g = agg_by_grade(cur["facts"], cur["factFields"], ctids)

    # ---------- 班主任维度 ----------
    cts_rows = []
    for ctid, c in ctmap.items():
        gd = cur_g.get(ctid, {})
        inn = sum(v[0] for v in gd.values())
        lb = sum(v[1] for v in gd.values())
        dl = sum(v[2] for v in gd.values())   # 今日新增
        tg = sum(v[3] for v in gd.values())

        per_grade = []
        for g, (gi, gl, gdl, gt) in gd.items():
            per_grade.append({
                "grade": g, "inClass": gi, "lianbao": gl, "target": gt,
                "completeRate": rate(gl, gt), "gap": gt - gl,
                "todayNew": gdl,
            })
        per_grade.sort(key=lambda x: (GRADE_ORDER.get(x["grade"], 9), x["grade"]))

        cts_rows.append({
            "name": c["name"], "ctid": ctid,
            "subjects": c.get("subjects", []),
            "inClass": inn, "lianbao": lb, "target": tg,
            "completeRate": rate(lb, tg), "gap": tg - lb,
            "todayNew": dl,
            "per_grade": per_grade,
        })
    # 主排序：按今日新增降序
    cts_rows.sort(key=lambda x: (-x["todayNew"], -x["completeRate"]))

    # ---------- 年级维度 ----------
    grade_rows = []
    g_all = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    g_cts = defaultdict(set)
    for ctid, gd in cur_g.items():
        for g, (inn, lb, dl, tg) in gd.items():
            a = g_all[g]
            a[0] += inn; a[1] += lb; a[2] += dl; a[3] += tg
            g_cts[g].add(ctid)
    for g, (inn, lb, dl, tg) in g_all.items():
        grade_rows.append({
            "grade": g, "inClass": inn, "lianbao": lb, "target": tg,
            "completeRate": rate(lb, tg), "gap": tg - lb,
            "todayNew": dl, "ctCount": len(g_cts[g]),
        })
    grade_rows.sort(key=lambda x: (GRADE_ORDER.get(x["grade"], 9), x["grade"]))

    # ---------- 组整体 ----------
    t_in = sum(r["inClass"] for r in cts_rows)
    t_lb = sum(r["lianbao"] for r in cts_rows)
    t_tg = sum(r["target"] for r in cts_rows)
    t_new = sum(r["todayNew"] for r in cts_rows)

    return {
        "group": group_name, "meta": meta,
        "group_total": {
            "ctCount": len(cts_rows), "inClass": t_in, "lianbao": t_lb,
            "target": t_tg, "completeRate": rate(t_lb, t_tg), "gap": t_tg - t_lb,
            "todayNew": t_new,
            "subjects": grp.get("subjects", []),
        },
        "classTeachers": cts_rows,
        "grades": grade_rows,
    }


def render_text(r):
    m = r["meta"]
    t = r["group_total"]
    grades = [g["grade"] for g in r["grades"]]

    L = []
    L.append(f"**📊 续报战报 · {r['group']}组**")
    L.append(f"数据时间 {m['updated'][5:]}")
    L.append("")
    L.append(f"完成率 **{t['completeRate']:.2f}%**"
             f"　今日新增 **{t['todayNew']:.0f}**"
             f"　距完标 **{t['gap']:.0f}**")
    L.append("")

    # 伙伴明细表（窄列：无千分位，短列名）
    L.append("**【伙伴】**（按今日新增人数降序）")
    head = ["伙伴", "今日新增", "完成率"] + grades + ["距完标"]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    for c in r["classTeachers"]:
        pmap = {pg["grade"]: pg["todayNew"] for pg in c["per_grade"]}
        gcols = [f"{pmap.get(g, 0):.0f}" for g in grades]
        cells = [c["name"], f"{c['todayNew']:.0f}", f"{c['completeRate']:.2f}%"] \
            + gcols + [f"{c['gap']:.0f}"]
        L.append("| " + " | ".join(cells) + " |")
    L.append("")
    L.append(f"🔗 {BOARD_URL}")
    return "\n".join(L)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 scripts/gen_group_report.py <教学组名> [--json]")
    name = sys.argv[1]
    r = build_report(name)
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(render_text(r))


if __name__ == "__main__":
    main()

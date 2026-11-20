#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板 · 教学组战报生成器（markdown 表格版）

结构：
  1. 头部：组整体关键指标
  2. 年级汇总表
  3. 伙伴明细表（按新增联报人数降序），含各年级增量列

增量对比对象 = 上一版数据（基线），不是今日累计。
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
PREV_FACTS_FILE = os.path.join(REPO_DIR, "xubao_prev_facts.json")

BOARD_URL = "https://mjx0217.github.io/xubao-kanban/"
GRADE_ORDER = {"初一": 0, "初二": 1, "初三": 2}


def load():
    with open(DATA_FILE, encoding="utf-8") as f:
        cur = json.load(f)
    prev = None
    if os.path.exists(PREV_FACTS_FILE):
        with open(PREV_FACTS_FILE, encoding="utf-8") as f:
            prev = json.load(f)
    return cur, prev


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
    cur, prev = load()
    meta = cur["meta"]

    grp = next((g for g in cur["groups"] if g["name"] == group_name), None)
    if grp is None:
        raise SystemExit(f"❌ 找不到教学组「{group_name}」")
    cts = [c for c in cur["classTeachers"] if c["group"] == group_name]
    ctids = {c["ctid"] for c in cts}
    ctmap = {c["ctid"]: c for c in cts}

    cur_g = agg_by_grade(cur["facts"], cur["factFields"], ctids)
    prev_g = (agg_by_grade(prev["facts"], prev["factFields"], ctids) if prev else {})
    prev_upd = prev.get("updated") if prev else None

    # ---------- 班主任维度 ----------
    cts_rows = []
    for ctid, c in ctmap.items():
        gd = cur_g.get(ctid, {})
        p_gd = prev_g.get(ctid, {})
        inn = sum(v[0] for v in gd.values())
        lb = sum(v[1] for v in gd.values())
        tg = sum(v[3] for v in gd.values())
        p_lb = sum(v[1] for v in p_gd.values())
        p_tg = sum(v[3] for v in p_gd.values())
        cr, pr = rate(lb, tg), rate(p_lb, p_tg)

        per_grade = []
        for g, (gi, gl, gd_, gt) in gd.items():
            pg = p_gd.get(g)
            pl = pg[1] if pg else 0.0
            pt = pg[3] if pg else 0.0
            per_grade.append({
                "grade": g, "inClass": gi, "lianbao": gl, "target": gt,
                "completeRate": rate(gl, gt), "gap": gt - gl,
                "prevLianbao": pl, "deltaLianbao": gl - pl,
                "deltaRate": rate(gl, gt) - rate(pl, pt),
            })
        per_grade.sort(key=lambda x: (GRADE_ORDER.get(x["grade"], 9), x["grade"]))

        cts_rows.append({
            "name": c["name"], "ctid": ctid,
            "subjects": c.get("subjects", []),
            "inClass": inn, "lianbao": lb, "target": tg,
            "completeRate": cr, "gap": tg - lb,
            "prevRate": pr, "prevLianbao": p_lb,
            "deltaLianbao": lb - p_lb, "deltaRate": cr - pr,
            "per_grade": per_grade,
        })
    # 主排序：按总增量降序（用户要求）
    cts_rows.sort(key=lambda x: (-x["deltaLianbao"], -x["completeRate"]))

    # ---------- 年级维度 ----------
    grade_rows = []
    g_all = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    g_cts = defaultdict(set)
    for ctid, gd in cur_g.items():
        for g, (inn, lb, dl, tg) in gd.items():
            a = g_all[g]
            a[0] += inn; a[1] += lb; a[2] += tg
            p = prev_g.get(ctid, {}).get(g)
            if p:
                a[3] += p[1]; a[4] += p[3]
            g_cts[g].add(ctid)
    for g, (inn, lb, tg, p_lb, p_tg) in g_all.items():
        cr, pr = rate(lb, tg), rate(p_lb, p_tg)
        grade_rows.append({
            "grade": g, "inClass": inn, "lianbao": lb, "target": tg,
            "completeRate": cr, "prevRate": pr,
            "deltaRate": cr - pr, "deltaLianbao": lb - p_lb,
            "gap": tg - lb, "ctCount": len(g_cts[g]),
        })
    grade_rows.sort(key=lambda x: (GRADE_ORDER.get(x["grade"], 9), x["grade"]))

    # ---------- 组整体 ----------
    t_in = sum(r["inClass"] for r in cts_rows)
    t_lb = sum(r["lianbao"] for r in cts_rows)
    t_tg = sum(r["target"] for r in cts_rows)
    p_lb_t = sum(r["prevLianbao"] for r in cts_rows)
    p_tg_t = 0
    if prev:
        i = {k: n for n, k in enumerate(prev["factFields"])}
        p_tg_t = sum(r[i["target"]] for r in prev["facts"] if r[i["ctid"]] in ctids)

    return {
        "group": group_name, "meta": meta, "prevUpdated": prev_upd,
        "group_total": {
            "ctCount": len(cts_rows), "inClass": t_in, "lianbao": t_lb,
            "target": t_tg, "completeRate": rate(t_lb, t_tg), "gap": t_tg - t_lb,
            "prevLianbao": p_lb_t, "prevRate": rate(p_lb_t, p_tg_t),
            "deltaLianbao": t_lb - p_lb_t,
            "deltaRate": rate(t_lb, t_tg) - rate(p_lb_t, p_tg_t),
            "subjects": grp.get("subjects", []),
        },
        "classTeachers": cts_rows,
        "grades": grade_rows,
    }


def render_text(r):
    m = r["meta"]
    t = r["group_total"]
    prev_upd = r["prevUpdated"] or "—"
    pu = prev_upd[5:] if len(prev_upd) >= 16 else prev_upd   # MM-DD HH:MM
    grades = [g["grade"] for g in r["grades"]]

    L = []
    L.append(f"**📊 续报战报 · {r['group']}组**")
    L.append(f"{m['updated'][5:]} ｜ 较 {pu} 增量")
    L.append("")
    L.append(f"完成率 **{t['completeRate']:.2f}%**（{t['deltaRate']:+.2f}%）"
             f"　新增联报 **{t['deltaLianbao']:+.0f}**"
             f"　距完标 **{t['gap']:.0f}**")
    L.append("")

    # 伙伴明细表（窄列：无千分位，短列名）
    L.append("**【伙伴】**（按新增联报人数降序）")
    head = ["伙伴", "新增", "完成率"] + grades + ["距完标"]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "---|" * len(head))
    for c in r["classTeachers"]:
        pmap = {pg["grade"]: pg["deltaLianbao"] for pg in c["per_grade"]}
        gcols = [f"{pmap.get(g, 0):+.0f}" for g in grades]
        cells = [c["name"], f"+{c['deltaLianbao']:.0f}", f"{c['completeRate']:.2f}%"] \
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

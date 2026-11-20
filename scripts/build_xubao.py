#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板数据更新脚本 v3（重建版）

底表：
  26-27学年秋续寒春_实时续报底表-YYYY-MM-DD HH_MM_SS.xlsx
  路径 /mnt/d/Downloads/，取最新一份（按文件名时间戳排序）

指标口径（已与领券班级明细表交叉验证）：
  - 在班人次    = 底表「在班人次」
  - 续报人次    = 底表「续报人次」
  - 联报人次    = 底表「联报人次」
  - 达成人次    = 续报人次 + 联报人次      ← 相当于领券表的「领券人次_已联报计入领券」
  - 目标人次    = 底表「联报人次目标」      ← 续报+联报的合并目标（目标/在班 与领券表一致 68.7%）
  - 完成率      = 达成人次 / 目标人次
  - 续报率      = 续报人次 / 在班人次
  - 联报率      = 联报人次 / 在班人次
  - 目标覆盖率  = 目标人次 / 在班人次
  - 缺口人次    = 目标人次 - 达成人次
  - 日增人次    = 续报人次日增 + 联报人次日增

对比逻辑：
  上次 = GitHub 上已发布的 xubao_data.json（仅当 period 一致才对比，否则不展示环比）

Usage:
    python3 build_xubao.py [--dry-run]
"""
import json, os, re, glob, sys, base64, argparse
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
import pandas as pd

REPO_DIR     = "/home/mjx/repos/xubao-kanban"
DOWNLOAD_DIR = "/mnt/d/Downloads"
DATA_FILE    = "xubao_data.json"
LAST_FILE    = "xubao_data_last.json"
GITHUB_REPO  = "mjx0217/xubao-kanban"
BASE         = "沈阳"

# 教学组别名归一：底表字段值改名时，历史值统一映射到现名
GROUP_ALIAS = {
    "牟春宇": "郭佳",
    "\\N": "未知",
    "nan": "未知",
    "None": "未知",
}

# ── GitHub API 推送（WSL 无法 git push，参考服务看板脚本）────────────────
def github_api_push(repo_dir, msg, token, files):
    """files: [(相对路径, bytes内容), ...]"""
    repo = GITHUB_REPO

    def api(path, data=None, method=None):
        url = f"https://api.github.com/repos/{repo}{path}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": e.code, "body": e.read().decode()[:300]}

    ref = api("/git/refs/heads/main")
    if "object" not in ref:
        print(f"[GIT-API] 获取 ref 失败: {ref}"); return False
    parent = ref["object"]["sha"]
    commit = api(f"/git/commits/{parent}")
    tree = commit["tree"]["sha"]

    entries = []
    for rel, content in files:
        blob = api("/git/blobs", json.dumps({
            "content": base64.b64encode(content).decode(),
            "encoding": "base64"}).encode())
        if "sha" not in blob:
            print(f"[GIT-API] blob 创建失败({rel}): {blob}"); return False
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    new_tree = api("/git/trees", json.dumps({"tree": entries, "base_tree": tree}).encode())
    if "sha" not in new_tree:
        print(f"[GIT-API] tree 创建失败: {new_tree}"); return False
    new_commit = api("/git/commits", json.dumps({
        "message": msg, "tree": new_tree["sha"], "parents": [parent]}).encode())
    if "sha" not in new_commit:
        print(f"[GIT-API] commit 创建失败: {new_commit}"); return False
    result = api("/git/refs/heads/main",
                 json.dumps({"sha": new_commit["sha"]}).encode(), method="PATCH")
    if "object" in result:
        print(f"[GIT-API] 推送成功: {new_commit['sha'][:12]}"); return True
    print(f"[GIT-API] 分支更新失败: {result}"); return False


def get_github_token():
    cred = Path.home() / ".git-credentials"
    if cred.exists():
        m = re.search(r"ghp_[A-Za-z0-9]+", cred.read_text())
        if m:
            return m.group(0)
    env = os.environ.get("GITHUB_TOKEN")
    if env:
        return env
    raise SystemExit("❌ 找不到 GitHub token（~/.git-credentials 或 GITHUB_TOKEN）")


def fetch_github_json(token, path=DATA_FILE):
    """从 GitHub 拉取已发布的 json（用于环比）"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            content = json.loads(r.read())
        return json.loads(base64.b64decode(content["content"]).decode())
    except Exception as e:
        print(f"[GITHUB] 拉取 {path} 失败（无环比）: {e}")
        return None


# ── 找底表 ───────────────────────────────────────────────────────────
def find_base_table():
    pats = [
        os.path.join(DOWNLOAD_DIR, "*实时续报底表*.xlsx"),
        os.path.join(DOWNLOAD_DIR, "*续报底表*.xlsx"),
    ]
    files = []
    for p in pats:
        for f in glob.glob(p):
            name = os.path.basename(f)
            if name.startswith("~$"):       # Excel 锁文件
                continue
            if "离线" in name:              # 离线底表口径不同，不用
                continue
            files.append(f)
    if not files:
        raise SystemExit(f"❌ 在 {DOWNLOAD_DIR} 找不到续报底表")

    def ts(f):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2})_(\d{2})_(\d{2})", Path(f).stem)
        return datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %H %M %S") if m else datetime.min

    files.sort(key=lambda f: (ts(f), os.path.getmtime(f)))
    return files[-1]


def parse_time(path):
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2})_(\d{2})_(\d{2})", Path(path).stem)
    if m:
        return f"{m.group(1)} {m.group(2)}:{m.group(3)}"
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def to_num(v):
    if v is None:
        return 0.0
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if v in ("", "\\N", "nan", "None"):
                return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def clean_group(g):
    g = str(g).strip()
    return GROUP_ALIAS.get(g, g)


def r2(x):
    return round(x + 0.0, 2)


# ── 主流程 ───────────────────────────────────────────────────────────
def build():
    src = find_base_table()
    updated = parse_time(src)
    print(f"📊 底表: {os.path.basename(src)}")
    print(f"   数据时间: {updated}")

    # 底表可能有多个 sheet（第一个常是空壳），挑含「基地」列的那个
    xl = pd.ExcelFile(src)
    df = None
    for sn in xl.sheet_names:
        try:
            cand = pd.read_excel(src, sheet_name=sn)
        except Exception:
            continue
        if "基地" in cand.columns and "在班人次" in cand.columns:
            df = cand
            print(f"   工作表: {sn}")
            break
    if df is None:
        raise SystemExit("❌ 底表里找不到含「基地/在班人次」的表头行")
    print(f"   {BASE} 行数: {len(df)}")

    df = df[df["基地"].astype(str).str.strip() == BASE].copy()
    df["教学组"] = df["教学组"].map(clean_group)
    df["年级"] = df["年级"].astype(str).str.strip()
    df["学科"] = df["学科"].astype(str).str.strip()
    df["学科汇总2"] = df["学科汇总2"].astype(str).str.strip()
    df["班主任名"] = df["班主任名"].astype(str).str.strip()
    df["班主任id"] = df["班主任id"].astype(str).str.strip()
    for c in ["在班人次", "续报人次", "续报人次日增", "联报人次", "联报人次日增", "联报人次目标"]:
        df[c] = df[c].map(to_num)

    df["达成人次"] = df["续报人次"] + df["联报人次"]
    # 日增：续报日增与联报日增高度重合（同一批人两种计数），直接相加会双计
    # → 逐行取较大值，再求和
    df["日增人次"] = df[["续报人次日增", "联报人次日增"]].max(axis=1)

    NUM = ["在班人次", "续报人次", "联报人次", "达成人次", "日增人次", "联报人次目标"]

    # 学员类型/购课时间类型也带上，便于后续扩展
    for c in ["学员类型", "购课时间类型", "课程类型", "排期"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()

    # ── 班主任维度 ──
    ct_rows = []
    for ctid, g in df.groupby("班主任id", sort=False):
        g = g[g["在班人次"] > 0]
        if g.empty:
            continue
        inclass = g["在班人次"].sum()
        xubao   = g["续报人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = xubao + lianbao
        ct_rows.append({
            "ctid": ctid,
            "name": g["班主任名"].iloc[0],
            "group": g["教学组"].iloc[0],
            "subjects": sorted(set(g["学科"]) - {""}),
            "subj2": sorted(set(g["学科汇总2"]) - {""}),
            "grades": sorted(set(g["年级"]) - {""}),
            "inClass": r2(inclass),
            "xubao": r2(xubao),
            "lianbao": r2(lianbao),
            "achieved": r2(achieved),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "xubaoRate": r2(xubao / inclass * 100) if inclass else 0.0,
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    ct_rows.sort(key=lambda x: -x["achieved"])

    # ── 教学组维度 ──
    group_rows = []
    for name, g in df.groupby("教学组", sort=False):
        inclass = g["在班人次"].sum()
        xubao   = g["续报人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = xubao + lianbao
        group_rows.append({
            "name": name,
            "ctCount": int(g["班主任id"].nunique()),
            "subjects": sorted(set(g["学科"]) - {""}),
            "inClass": r2(inclass),
            "xubao": r2(xubao),
            "lianbao": r2(lianbao),
            "achieved": r2(achieved),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "xubaoRate": r2(xubao / inclass * 100) if inclass else 0.0,
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    group_rows.sort(key=lambda x: -x["achieved"])

    # ── 学科维度 ──
    subject_rows = []
    for name, g in df.groupby("学科", sort=False):
        inclass = g["在班人次"].sum()
        xubao   = g["续报人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = xubao + lianbao
        subject_rows.append({
            "name": name,
            "subj2": sorted(set(g["学科汇总2"]) - {""}),
            "ctCount": int(g["班主任id"].nunique()),
            "inClass": r2(inclass),
            "xubao": r2(xubao),
            "lianbao": r2(lianbao),
            "achieved": r2(achieved),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "xubaoRate": r2(xubao / inclass * 100) if inclass else 0.0,
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    subject_rows.sort(key=lambda x: -x["achieved"])

    # ── 年级维度 ──
    grade_rows = []
    for name, g in df.groupby("年级", sort=False):
        inclass = g["在班人次"].sum()
        xubao   = g["续报人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = xubao + lianbao
        grade_rows.append({
            "name": name,
            "inClass": r2(inclass), "xubao": r2(xubao), "lianbao": r2(lianbao),
            "achieved": r2(achieved), "target": r2(target), "daily": r2(g["日增人次"].sum()),
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "gap": r2(target - achieved),
        })
    grade_rows.sort(key=lambda x: -x["achieved"])

    # ── 总计 ──
    tot_in    = df["在班人次"].sum()
    tot_xb    = df["续报人次"].sum()
    tot_lb    = df["联报人次"].sum()
    tot_tg    = df["联报人次目标"].sum()
    tot_ach   = tot_xb + tot_lb
    tot_daily = df["日增人次"].sum()

    period = "26-27学年 秋下续寒春"

    meta = {
        "base": BASE,
        "period": period,
        "updated": updated,
        "sourceFile": os.path.basename(src),
        "inClass": r2(tot_in),
        "xubao": r2(tot_xb),
        "lianbao": r2(tot_lb),
        "achieved": r2(tot_ach),
        "target": r2(tot_tg),
        "gap": r2(tot_tg - tot_ach),
        "daily": r2(tot_daily),
        "xubaoRate": r2(tot_xb / tot_in * 100) if tot_in else 0.0,
        "lianbaoRate": r2(tot_lb / tot_in * 100) if tot_in else 0.0,
        "completeRate": r2(tot_ach / tot_tg * 100) if tot_tg else 0.0,
        "targetRate": r2(tot_tg / tot_in * 100) if tot_in else 0.0,
        "ctCount": len(ct_rows),
        "groupCount": len(group_rows),
        "subjectCount": len(subject_rows),
    }

    out = {
        "meta": meta,
        "subjects": subject_rows,
        "groups": group_rows,
        "grades": grade_rows,
        "classTeachers": ct_rows,
    }

    # ── 环比：拉 GitHub 上次发布的数据 ──
    token = get_github_token()
    prev = fetch_github_json(token)
    last_rates = {"subjects": {}, "groups": {}, "classTeachers": {}}
    if prev and prev.get("meta", {}).get("period") == period:
        pm = prev["meta"]
        print(f"🔗 环比基线: {pm.get('updated')} (完成率 {pm.get('completeRate')}%)")
        prev_subj = {s["name"]: s for s in prev.get("subjects", [])}
        prev_grp  = {g["name"]: g for g in prev.get("groups", [])}
        prev_ct   = {c["ctid"]: c for c in prev.get("classTeachers", [])}

        def delta(cur, old, key="completeRate"):
            if old is None or old.get(key) is None:
                return None
            return r2(cur.get(key, 0) - old.get(key, 0))

        for s in subject_rows:
            last_rates["subjects"][s["name"]] = {
                "lastCompleteRate": prev_subj.get(s["name"], {}).get("completeRate"),
                "lastAchieved": prev_subj.get(s["name"], {}).get("achieved"),
                "delta": delta(s, prev_subj.get(s["name"])),
            }
        for g in group_rows:
            last_rates["groups"][g["name"]] = {
                "lastCompleteRate": prev_grp.get(g["name"], {}).get("completeRate"),
                "lastAchieved": prev_grp.get(g["name"], {}).get("achieved"),
                "delta": delta(g, prev_grp.get(g["name"])),
            }
        for c in ct_rows:
            last_rates["classTeachers"][c["ctid"]] = {
                "lastCompleteRate": prev_ct.get(c["ctid"], {}).get("completeRate"),
                "lastAchieved": prev_ct.get(c["ctid"], {}).get("achieved"),
                "delta": delta(c, prev_ct.get(c["ctid"])),
            }
        meta["prevUpdated"] = pm.get("updated")
        meta["prevCompleteRate"] = pm.get("completeRate")
        meta["prevAchieved"] = pm.get("achieved")
        meta["prevGap"] = pm.get("gap")
        meta["prevDaily"] = pm.get("daily")
    else:
        reason = "周期不一致" if prev else "无上次数据"
        print(f"ℹ️ 暂不展示环比（{reason}）")

    out["__lastRates"] = last_rates

    return out, updated, token


def clean_json(obj):
    """递归把 NaN/Inf 转 0，保证输出标准 JSON（allow_nan=False）"""
    import math
    if isinstance(obj, dict):
        return {k: clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只生成不推送")
    args = ap.parse_args()

    out, updated, token = build()
    out = clean_json(out)

    data_path = os.path.join(REPO_DIR, DATA_FILE)
    last_path = os.path.join(REPO_DIR, LAST_FILE)

    # 存档上一版
    if os.path.exists(data_path):
        try:
            old = json.load(open(data_path, encoding="utf-8"))
            if old.get("meta", {}).get("period") == out["meta"]["period"]:
                json.dump(old, open(last_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                print(f"🗂  已存档上一版到 {LAST_FILE}")
        except Exception as e:
            print(f"⚠️  存档上一版失败: {e}")

    content = json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)
    with open(data_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 自检：标准 JSON 可解析
    json.loads(content)
    print(f"✅ 已生成 {DATA_FILE}  ({len(content)/1024:.0f} KB)")

    m = out["meta"]
    print(f"   在班 {m['inClass']:,.0f} | 续报 {m['xubao']:,.0f} | 联报 {m['lianbao']:,.0f} | "
          f"达成 {m['achieved']:,.0f} / 目标 {m['target']:,.0f}")
    print(f"   完成率 {m['completeRate']}% | 缺口 {m['gap']:,.0f} | 日增 {m['daily']:,.0f}")
    print(f"   学科 {m['subjectCount']} | 教学组 {m['groupCount']} | 班主任 {m['ctCount']}")

    if args.dry_run:
        print("🛑 dry-run，未推送")
        return

    # 推送 index.html + json
    html_path = os.path.join(REPO_DIR, "index.html")
    files = [(DATA_FILE, content.encode("utf-8"))]
    if os.path.exists(html_path):
        files.append(("index.html", open(html_path, "rb").read()))
    for wf in [".github/workflows/pages.yml"]:
        p = os.path.join(REPO_DIR, wf)
        if os.path.exists(p):
            files.append((wf, open(p, "rb").read()))

    msg = f"续报看板数据更新 {updated}"
    if github_api_push(REPO_DIR, msg, token, files):
        print(f"📤 已推送到 GitHub: {msg}")
    else:
        print("❌ GitHub 推送失败")


if __name__ == "__main__":
    main()

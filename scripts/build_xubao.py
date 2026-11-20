#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板数据更新脚本 v3（重建版）

底表：
  26-27学年秋续寒春_实时续报底表-YYYY-MM-DD HH_MM_SS.xlsx
  路径 /mnt/d/Downloads/，取最新一份（按文件名时间戳排序）

指标口径（已用底表自带透视表 Sheet2 官方公式逐行验证，误差 < 1e-14）：
  - 在班人次    = 底表「在班人次」
  - 联报人次    = 底表「联报人次」
  - 达成人次    = 联报人次
      ⚠️ 不能用「续报人次 + 联报人次」。实测 sheet1 里续报与联报在 ~96.6% 的行数值完全相同
         （同一批人从两个产品视角各记一次），相加等于把同一批人重复计一遍。
  - 目标人次    = 底表「联报人次目标」
  - 完成率      = 联报人次 / 联报人次目标
      ⚠️ 这是官方「目标完成率」的定义。已对 Sheet2 的 81 个明细行 + 10 个汇总行逐行比对，
         「联报人次 ÷ 联报人次目标」与官方值最大误差 3.11e-15，完全一致。
  - 联报率      = 联报人次 / 在班人次（官方「联报率」，误差 5e-16）
  - 目标覆盖率  = 目标人次 / 在班人次
  - 缺口人次    = 目标人次 - 达成人次
  - 日增人次    = 逐行 max(续报人次日增, 联报人次日增) 后求和
      ⚠️ 不能直接相加，两列同样是同一批人的两种计数（99% 的行相同）。

注：「续报人次」已按需求从看板移除（不展示、不输出），
   但底表的「续报人次日增」仍需读取以支撑日增口径。

对比逻辑：
  上次 = GitHub 上已发布的 xubao_data.json（仅当 period 一致才对比，否则不展示环比）

Usage:
    python3 build_xubao.py [--dry-run]
"""
import json, os, re, glob, sys, base64, argparse, time
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
import pandas as pd

REPO_DIR     = "/home/mjx/repos/xubao-kanban"
HERE         = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = "/mnt/d/Downloads"
DATA_FILE    = "xubao_data.json"
PREV_FACTS_FILE = "xubao_prev_facts.json"   # 基线版事实表（单独存，避免 json 体积翻倍）
HISTORY_DIR    = "history"                 # 本地历史版本存档目录（本地基线来源）
HISTORY_KEEP   = 10                       # 保留最近几版
LAST_FILE    = "xubao_data_last.json"
GITHUB_REPO  = "mjx0217/xubao-kanban"
BASE         = "沈阳"

# 行级事实表（out["facts"]）的字段顺序，前端按此索引解析
# 刻意精简：姓名/教学组可由 ctid 关联 classTeachers 得到（已验证二者对 ctid 均唯一），
# 「达成」恒等于「联报」，无需重复存 → 体积减少约 1/4
FACT_FIELDS = ["ctid", "subject", "grade", "version", "difficulty",
               "inClass", "lianbao", "daily", "target"]

# 难度展示顺序（从难到易）；不在表里的排在后面
DIFFICULTY_ORDER = ["S（5星）", "S+", "A+（4星）", "A（3星）"]

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
        # WSL 网络对大请求体（~270KB base64）偶发 SSL write timeout / EOF，
        # 属瞬时故障，重试即可成功。
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method or ("POST" if data else "GET"))
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return {"error": e.code, "body": e.read().decode()[:300]}
            except Exception as e:          # URLError / ssl timeout / EOF
                last_err = e
                time.sleep(4 * (attempt + 1))
        print(f"[GIT-API] 请求失败({path}): {last_err}")
        return {"error": "network", "body": str(last_err)}

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


def _gh_get(url, token, timeout=60, retries=2):
    """GET GitHub API，返回解析后的 json；失败返回 None

    超时要短：单个请求卡 120s × 3 次重试 = 6 分钟，脚本会看起来像挂了。
    """
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json",
        "User-Agent": "xubao-kanban"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    print(f"[GITHUB] GET 失败 {url}: {last_err}")
    return None


def fetch_github_json(token, path=DATA_FILE, ref=None):
    """从 GitHub 拉取已发布的 json（指定 ref 可取历史版本）

    ⚠️ 文件 > 1MB 时 contents API 会返回 encoding=none、content 为空，
    必须改用 Git blob API（raw.githubusercontent 同样可行）。
    """
    if ref:
        # 有 ref（历史版本）→ 直接用 blob API，规避 1MB 限制
        sha = _gh_get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={ref}", token)
        if not sha or not sha.get("sha"):
            return None
        # 体积过大的历史版本（曾因嵌套 prevFacts 涨到 1.6MB）跳过，
        # 否则每次拉基线要等几十秒，脚本看起来像卡住
        size = sha.get("size", 0)
        if size > 1_200_000:
            print(f"[GITHUB] 跳过过大的历史版本 {ref[:8]}（{size//1024}KB）")
            return None
        blob = _gh_get(f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{sha['sha']}", token)
        if not blob or not blob.get("content"):
            return None
        raw = base64.b64decode(blob["content"]).decode()
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"[GITHUB] 解析 {path}@{ref[:8]} 失败: {e}")
            return None

    # 无 ref：先试 contents API，超过 1MB 则回退 blob API
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    content = _gh_get(url, token)
    if not content:
        return None
    raw = ""
    if content.get("encoding") == "base64" and content.get("content"):
        raw = base64.b64decode(content["content"]).decode()
    elif content.get("size", 0) > 1000000 and content.get("sha"):
        blob = _gh_get(f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{content['sha']}", token)
        if blob and blob.get("content"):
            raw = base64.b64decode(blob["content"]).decode()
    if not raw:
        print(f"[GITHUB] 拉取 {path} 失败（内容为空，size={content.get('size')}）")
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[GITHUB] 解析 {path} 失败: {e}")
        return None


def load_local_baseline(cur_updated, cur_period):
    """从本地文件取基线（快，不依赖网络）

    候选顺序（新的优先）：
      1. history/*.json      ← 历史版本存档目录（保留最近 N 版）★最准确
      2. xubao_data.json     ← 上一次发布的版本（build() 时尚未覆盖）
      3. xubao_data_last.json← 再往前一版（兼容旧存档）

    ⚠️ 踩坑：只读 LAST_FILE 会导致基线永远差两版
    （例：17:00 那轮基线取到 15:00，而不是 16:00）；
    只留一版存档又会导致同版本重跑后本地历史被覆盖 → 只能退回网络兜底。
    → 用 history/ 目录保留多版，从新到旧挑第一个合法的。
    规则与 pick_baseline 一致：排除与当前同时间戳的版本（那就是自己）。
    """
    cands = []
    hist_dir = os.path.join(REPO_DIR, HISTORY_DIR)
    if os.path.isdir(hist_dir):
        for fn in sorted(os.listdir(hist_dir), reverse=True):   # 文件名含时间戳，倒序=从新到旧
            if fn.endswith(".json"):
                cands.append(os.path.join(hist_dir, fn))
    cands += [os.path.join(REPO_DIR, DATA_FILE), os.path.join(REPO_DIR, LAST_FILE)]

    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"[本地基线] 读取 {os.path.basename(p)} 失败: {e}")
            continue
        m = d.get("meta", {})
        if m.get("period") != cur_period:
            continue
        if not m.get("updated") or m.get("updated") == cur_updated:
            continue          # 同时间戳 = 自己，不能当基线
        return d
    return None


def fetch_github_history(token, path=DATA_FILE, max_commits=20):
    """列出所有改过该文件的 commit（从新到旧），返回 [(sha, 提交时间), ...]"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path={path}&per_page={max_commits}"
    commits = _gh_get(url, token)
    if not commits:
        return []
    out = []
    for c in commits:
        sha = c.get("sha")
        ctime = (c.get("commit") or {}).get("committer", {}).get("date", "")
        if sha:
            out.append((sha, ctime))
    return out


def pick_baseline(candidates, cur_updated, cur_period):
    """
    从历史版本候选中挑「环比基线」。

    candidates: [(updated, data), ...] 已按时间从新到旧排列

    规则（避免同一天重跑导致基线 = 自己、delta 归零）：
      1. 只考虑 period 一致的版本（防跨周期串台）
      2. 排除 updated == 当前 updated 的版本（那就是自己）
      3. 优先取「同一天」里最新的不同时间戳版本
      4. 同一天没有 → 退回最近的不同时间戳版本
    返回 (基线元数据, 基线data) 或 (None, None)
    """
    def same_day(a, b):
        try:
            return str(a)[:10] == str(b)[:10]
        except Exception:
            return False

    same_day_cands = []
    other_cands = []
    for updated, data in candidates:
        if not data:
            continue
        m = data.get("meta", {})
        if m.get("period") != cur_period:
            continue
        if not updated or updated == cur_updated:
            continue
        (same_day_cands if same_day(updated, cur_updated) else other_cands).append((updated, data, m))

    # candidates 已按时间从新到旧排序，取第一个即可
    for cands in (same_day_cands, other_cands):
        if cands:
            updated, data, m = cands[0]
            return m, data
    return None, None


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
def build(src_override=None):
    prev_facts_payload = None
    src = src_override or find_base_table()
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
    df["版本"] = df["版本"].astype(str).str.strip()
    df["难度"] = df["难度"].astype(str).str.strip()
    # 「续报人次」本身不在看板展示，但「续报人次日增」仍需读取
    # （日增口径是逐行 max(续报日增, 联报日增)，见下）
    for c in ["在班人次", "续报人次日增", "联报人次", "联报人次日增", "联报人次目标"]:
        df[c] = df[c].map(to_num)

    # 达成人次 = 联报人次（续报与联报是同一批人，相加会双计）
    df["达成人次"] = df["联报人次"]
    # 日增：续报日增与联报日增高度重合（同一批人两种计数），直接相加会双计
    # → 逐行取较大值，再求和
    df["日增人次"] = df[["续报人次日增", "联报人次日增"]].max(axis=1)

    NUM = ["在班人次", "联报人次", "达成人次", "日增人次", "联报人次目标"]

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
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = lianbao
        ct_rows.append({
            "ctid": ctid,
            "name": g["班主任名"].iloc[0],
            "group": g["教学组"].iloc[0],
            "subjects": sorted(set(g["学科"]) - {""}),
            "subj2": sorted(set(g["学科汇总2"]) - {""}),
            "grades": sorted(set(g["年级"]) - {""}),
            "inClass": r2(inclass),
            "lianbao": r2(lianbao),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    ct_rows.sort(key=lambda x: -x["lianbao"])

    # ── 行级事实表（供前端按 版本/难度 精确筛选并重新聚合）──
    # 一名班主任常带多个版本/难度（299 名班主任 → 549 个 班主任×版本×难度 组合），
    # 只筛班主任会导致数字虚高，必须按明细重新聚合。
    # 字段用短名以压缩体积。
    fact_rows = []
    for rec in df[["班主任id", "学科", "年级", "版本", "难度",
                   "在班人次", "联报人次", "日增人次", "联报人次目标"]].itertuples(index=False):
        fact_rows.append([
            rec[0], rec[1], rec[2], rec[3], rec[4],
            r2(rec[5]), r2(rec[6]), r2(rec[7]), r2(rec[8]),
        ])
    fact_rows.sort(key=lambda x: (x[0], x[3], x[4]))

    # ── 教学组维度 ──
    group_rows = []
    for name, g in df.groupby("教学组", sort=False):
        inclass = g["在班人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = lianbao
        group_rows.append({
            "name": name,
            "ctCount": int(g["班主任id"].nunique()),
            "subjects": sorted(set(g["学科"]) - {""}),
            "inClass": r2(inclass),
            "lianbao": r2(lianbao),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    group_rows.sort(key=lambda x: -x["lianbao"])

    # ── 学科维度 ──
    subject_rows = []
    for name, g in df.groupby("学科", sort=False):
        inclass = g["在班人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = lianbao
        subject_rows.append({
            "name": name,
            "subj2": sorted(set(g["学科汇总2"]) - {""}),
            "ctCount": int(g["班主任id"].nunique()),
            "inClass": r2(inclass),
            "lianbao": r2(lianbao),
            "target": r2(target),
            "daily": r2(g["日增人次"].sum()),
            "lianbaoRate": r2(lianbao / inclass * 100) if inclass else 0.0,
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "targetRate": r2(target / inclass * 100) if inclass else 0.0,
            "gap": r2(target - achieved),
        })
    subject_rows.sort(key=lambda x: -x["lianbao"])

    # ── 年级维度 ──
    grade_rows = []
    for name, g in df.groupby("年级", sort=False):
        inclass = g["在班人次"].sum()
        lianbao = g["联报人次"].sum()
        target  = g["联报人次目标"].sum()
        achieved = lianbao
        grade_rows.append({
            "name": name,
            "inClass": r2(inclass), "lianbao": r2(lianbao),
            "target": r2(target), "daily": r2(g["日增人次"].sum()),
            "completeRate": r2(achieved / target * 100) if target else 0.0,
            "gap": r2(target - achieved),
        })
    grade_rows.sort(key=lambda x: -x["lianbao"])

    # ── 总计 ──
    tot_in    = df["在班人次"].sum()
    tot_lb    = df["联报人次"].sum()
    tot_tg    = df["联报人次目标"].sum()
    tot_ach   = tot_lb
    tot_daily = df["日增人次"].sum()

    period = "26-27学年 秋下续寒春"

    meta = {
        "base": BASE,
        "period": period,
        "updated": updated,
        "sourceFile": os.path.basename(src),
        "inClass": r2(tot_in),
        "lianbao": r2(tot_lb),
        "target": r2(tot_tg),
        "gap": r2(tot_tg - tot_ach),
        "daily": r2(tot_daily),
        "lianbaoRate": r2(tot_lb / tot_in * 100) if tot_in else 0.0,
        "completeRate": r2(tot_ach / tot_tg * 100) if tot_tg else 0.0,
        "targetRate": r2(tot_tg / tot_in * 100) if tot_in else 0.0,
        # 口径自证：完成率 = 联报率 / 目标覆盖率（供前端/外部核对）
        "completeRateCheck": r2((tot_lb / tot_in) / (tot_tg / tot_in) * 100) if tot_in and tot_tg else 0.0,
        "ctCount": len(ct_rows),
        "groupCount": len(group_rows),
        "subjectCount": len(subject_rows),
        # 筛选项：版本 / 难度（难度按从难到易排序）
        "versions": sorted(set(df["版本"]) - {""}),
        "difficulties": sorted(
            set(df["难度"]) - {""},
            key=lambda d: (DIFFICULTY_ORDER.index(d) if d in DIFFICULTY_ORDER else 99, d)),
    }

    out = {
        "meta": meta,
        "subjects": subject_rows,
        "groups": group_rows,
        "grades": grade_rows,
        "classTeachers": ct_rows,
        # 行级事实表：字段顺序见 FACT_FIELDS
        "facts": fact_rows,
        "factFields": FACT_FIELDS,
    }

    # ── 环比：基线取「同一天最近的不同时间戳版本」，避免重跑时基线=自己 ──
    token = get_github_token()
    prev = None
    prev_meta = None

    # 策略 A（快）：本地存档 xubao_data_last.json —— 上一次运行产出的上一版
    #   本地读文件几乎不耗时间，且不受 GitHub API 大文件慢/断流的影响
    local_prev = load_local_baseline(updated, period)
    if local_prev:
        prev = local_prev
        prev_meta = prev.get("meta", {})
        print(f"🔗 环比基线(本地存档): {prev_meta.get('updated')} (完成率 {prev_meta.get('completeRate')}%)")

    # 策略 B（兜底）：GitHub commit 历史
    #   本地存档缺失（如换了机器 / 首次部署）时才走网络
    if not prev:
        history = fetch_github_history(token)
        if history:
            cands = []
            seen_updated = set()
            for sha, _ctime in history:
                d = fetch_github_json(token, ref=sha)
                if not d:
                    continue
                u = d.get("meta", {}).get("updated")
                if not u or u in seen_updated:
                    continue          # 同一时间戳的多个 commit，只需一份
                seen_updated.add(u)
                cands.append((u, d))
                # 同一天已有「不同于当前」的候选就不必再翻更老的 commit（省 API 调用）
                # 注意：u == cur_updated 的那个是自己，不能作为停止信号
                if (u and updated and u != updated
                        and str(u)[:10] == str(updated)[:10]):
                    break
                if len(cands) >= 10:
                    break
            prev_meta, prev = pick_baseline(cands, updated, period)
            if prev_meta:
                print(f"🔗 环比基线(GitHub): {prev_meta.get('updated')} (完成率 {prev_meta.get('completeRate')}%)")
    last_rates = {"subjects": {}, "groups": {}, "classTeachers": {}}
    if prev and prev.get("meta", {}).get("period") == period:
        pm = prev["meta"]
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
                "delta": delta(s, prev_subj.get(s["name"])),
            }
        for g in group_rows:
            last_rates["groups"][g["name"]] = {
                "lastCompleteRate": prev_grp.get(g["name"], {}).get("completeRate"),
                "delta": delta(g, prev_grp.get(g["name"])),
            }
        for c in ct_rows:
            last_rates["classTeachers"][c["ctid"]] = {
                "lastCompleteRate": prev_ct.get(c["ctid"], {}).get("completeRate"),
                "delta": delta(c, prev_ct.get(c["ctid"])),
            }
        meta["prevUpdated"] = pm.get("updated")
        meta["prevCompleteRate"] = pm.get("completeRate")
        meta["prevGap"] = pm.get("gap")
        meta["prevDaily"] = pm.get("daily")
        # 基线版的事实表：前端筛选后据此重算「上一版完成率」，环比才准确
        # （基线版本可能是老脚本产出的，没有 facts → 筛选后环比会缺失，属可接受降级）
        prev_facts = prev.get("facts")
        if prev_facts:
            # 基线事实表单独存文件，不嵌套进本版 json
            # （嵌套会让每版 json 体积翻倍，实测从 750KB 涨到 1.6MB，
            #   且超过 1MB 后 GitHub contents API 会返回空内容）
            out["__prevFactsRef"] = PREV_FACTS_FILE
            out["__prevUpdated"] = pm.get("updated")
            out["__prevFactFields"] = prev.get("factFields") or FACT_FIELDS
            prev_facts_payload = {
                "updated": pm.get("updated"),
                "factFields": out["__prevFactFields"],
                "facts": prev_facts,
            }
        else:
            prev_facts_payload = None
            print("ℹ️  基线版本无 facts，筛选后的环比将不可用")
    else:
        reason = "周期不一致" if prev else "无上次数据"
        print(f"ℹ️ 暂不展示环比（{reason}）")

    out["__lastRates"] = last_rates

    return out, updated, token, prev_facts_payload


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


def archive_current_version():
    """把当前 xubao_data.json 存档到 history/（保留最近 N 版）

    ⚠️ 必须在 build() 之前调用！
    因为 build() 内部会通过 load_local_baseline() 读 history/ 找基线，
    如果存档发生在 build 之后，本轮就看不到「上一版」，基线会差一版。

    另外：只存一版会「丢历史」——
      第一轮跑完 data.json=17:00
      第二轮再跑，存档时读 data.json(=17:00) → 写进 history/17-00.json
      → 同一时间戳被覆盖，本地再没有比当前更早的版本 → 基线只能退回 GitHub 兜底
    修法：文件名带时间戳 + 保留最近 N 版。

    同时间戳重跑时用已存在的历史文件覆盖（内容相同，无害）。
    """
    data_path = os.path.join(REPO_DIR, DATA_FILE)
    if not os.path.exists(data_path):
        return
    try:
        with open(data_path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        print(f"⚠️  读取当前版本失败: {e}")
        return
    om = old.get("meta", {})
    u = om.get("updated")
    if not u:
        return
    hist_dir = os.path.join(REPO_DIR, HISTORY_DIR)
    os.makedirs(hist_dir, exist_ok=True)
    hp = os.path.join(hist_dir, f"{u.replace(':', '-').replace(' ', '_')}.json")
    with open(hp, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # 只保留最近 N 版，避免目录无限膨胀
    hs = sorted(os.listdir(hist_dir), reverse=True)
    for fn in hs[HISTORY_KEEP:]:
        try:
            os.remove(os.path.join(hist_dir, fn))
        except OSError:
            pass
    print(f"🗂  已存档当前版本到 {HISTORY_DIR}/{os.path.basename(hp)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只生成不推送")
    ap.add_argument("--src", help="指定底表文件路径（默认取最新一份）")
    ap.add_argument("--no-push", action="store_true", help="只生成不推送（同 --dry-run）")
    args = ap.parse_args()

    # ⚠️ 必须先存档，再 build
    # build() 会通过 load_local_baseline() 读 history/ 找基线，
    # 存档晚一步就会让基线差一版
    archive_current_version()

    out, updated, token, prev_facts_payload = build(src_override=args.src)
    out = clean_json(out)

    data_path = os.path.join(REPO_DIR, DATA_FILE)

    # （存档已移到 main() 开头、在 build() 之前执行，见 archive_current_version()）
    # 不缩进：indent=2 会让 json 体积翻倍（705KB → 360KB），
    # GitHub Pages 传输慢，用户打开看板会长时间白屏
    content = json.dumps(out, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with open(data_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 自检：标准 JSON 可解析
    json.loads(content)
    print(f"✅ 已生成 {DATA_FILE}  ({len(content)/1024:.0f} KB)")

    m = out["meta"]
    print(f"   在班 {m['inClass']:,.0f} | 联报 {m['lianbao']:,.0f} | "
          f"目标 {m['target']:,.0f}")
    print(f"   完成率 {m['completeRate']}% | 缺口 {m['gap']:,.0f} | 日增 {m['daily']:,.0f}")
    print(f"   学科 {m['subjectCount']} | 教学组 {m['groupCount']} | 班主任 {m['ctCount']}")

    if args.dry_run or args.no_push:
        print("🛑 dry-run，未推送")
        return

    # 推送 index.html + json
    html_path = os.path.join(REPO_DIR, "index.html")
    files = [(DATA_FILE, content.encode("utf-8"))]
    if os.path.exists(html_path):
        files.append(("index.html", open(html_path, "rb").read()))
    for wf in [".github/workflows/pages.yml", "README.md"]:
        p = os.path.join(REPO_DIR, wf)
        if os.path.exists(p):
            files.append((wf, open(p, "rb").read()))
    # 构建脚本也推上去（便于换机器后重建）
    for sp in ["build_xubao.py", "auto_update.py", "gen_group_report.py",
               "push_group_report.py", "test_baseline.py", "seed_history.py"]:
        p = os.path.join(HERE, sp)
        if os.path.exists(p):
            files.append((f"scripts/{sp}", open(p, "rb").read()))

    # 基线版事实表单独一个文件（供前端筛选后重算环比）
    if prev_facts_payload:
        pf = clean_json(prev_facts_payload)
        pf_content = json.dumps(pf, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        json.loads(pf_content)
        files.append((PREV_FACTS_FILE, pf_content.encode("utf-8")))
        with open(os.path.join(REPO_DIR, PREV_FACTS_FILE), "w", encoding="utf-8") as f:
            f.write(pf_content)
        print(f"✅ 已生成 {PREV_FACTS_FILE}  ({len(pf_content)/1024:.0f} KB, 基线 {pf['updated']})")

    msg = f"续报看板数据更新 {updated}"
    if github_api_push(REPO_DIR, msg, token, files):
        print(f"📤 已推送到 GitHub: {msg}")
    else:
        print("❌ GitHub 推送失败")


if __name__ == "__main__":
    main()

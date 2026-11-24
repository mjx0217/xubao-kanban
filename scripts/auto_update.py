#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板自动更新包装脚本（由 OpenClaw cron 调用）

职责：
  1. 时间窗守卫 —— 只在计划槽位（10/12/14/16/18/20/22 点的 :05~:45）内干活，
     错过的补跑直接静默退出（业务规则：错过即作废）
  2. 等文件落地 —— 底表可能还在下载（Chrome 写文件是流式的）
  3. 判重 —— 目标槽位跟已发布版本相同则跳过，不做无意义的重复提交
  4. 调 build_xubao.py 构建 + 推送

用法：python3 scripts/auto_update.py [--dry-run]
退出码：0=已更新 / 10=跳过（无新数据或不在窗口）/ 1=失败
"""

import os
import re
import sys
import glob
import time
import json
import subprocess
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
DOWNLOAD_DIR = "/mnt/d/Downloads"
DATA_FILE = "xubao_data.json"
BUILD_SCRIPT = os.path.join(HERE, "build_xubao.py")

# 计划槽位（小时）：与 Windows 计划任务 DownloadPush3143 的触发点一一对应
# ⚠️ 2026-11-22 用户要求去掉早盘 10 点档：服务端 10:00 前不给新底表，
#    10:05 推送只会拿旧数据/空跑。改为 12 点起，6 个槽位。
SLOT_HOURS = [12, 14, 16, 18, 20, 22]
# 窗口：从 slot:04 开始（下载在 slot:03 触发，留 1 分钟给浏览器启动）
#      到 slot+45 分钟为止（覆盖下载耗时 + 构建耗时）
WINDOW_MIN = 4
WINDOW_MAX = 45
# 合法 xlsx 的最小体积（正常底表约 1.3MB；错误 JSON 只有 ~110 字节）
MIN_XLSX_BYTES = 50 * 1024
# 等待底表落地的最长时间（秒）
WAIT_FOR_FILE = 900          # 15 分钟
FILE_POLL = 20                # 轮询间隔

# 需要推送教学组战报的组（群映射见 push_group_report.GROUP_CHANNELS）
PUSH_GROUPS = ["缪家新", "朱太阳"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def list_base_tables():
    """列出候选底表（排除 Excel 锁文件与「离线」底表）"""
    out = []
    for pat in ("*实时续报底表*.xlsx", "*续报底表*.xlsx"):
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, pat)):
            name = os.path.basename(f)
            if name.startswith("~$"):
                continue
            if "离线" in name:
                continue
            out.append(f)
    return sorted(set(out))


def slot_ts(path):
    """从文件名解析数据快照时间，如 '2026-11-20 19_00_00' → datetime"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2})_(\d{2})_(\d{2})", Path(path).stem)
    if not m:
        return None
    return datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %H %M %S")


def published_slot():
    """当前已发布版本的数据时间"""
    p = os.path.join(REPO_DIR, DATA_FILE)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        u = d.get("meta", {}).get("updated")
        return datetime.strptime(u, "%Y-%m-%d %H:%M") if u else None
    except Exception:
        return None


def file_is_stable(path):
    """文件是否已写完且是合法的 xlsx（大小连续两次不变，且没有 ~$ 锁文件）

    ⚠️ 2026-11-21 20:05 踩坑：上游导出接口登录态过期时，下载到的「xlsx」
    其实是一个 110 字节的 JSON 错误体（{"code":10021,"msg":"未登录！"}）。
    旧逻辑只看大小/时间稳定性 → 坏文件被当成正常底表 → build 阶段才炸。
    现在额外校验 xlsx 文件头（PK\x03\x04）和最小体积，坏文件一律当作「还没写好」。
    """
    lock = os.path.join(os.path.dirname(path), "~$" + os.path.basename(path))
    if os.path.exists(lock):
        return False
    try:
        s1 = os.path.getsize(path)
        time.sleep(3)
        s2 = os.path.getsize(path)
    except OSError:
        return False
    if s1 <= 0 or s1 != s2:
        return False
    # 校验是真正的 xlsx（zip 容器），而不是服务端错误 JSON
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    if magic != b"PK\x03\x04":
        log(f"⚠️ 不是合法 xlsx（疑似服务端错误体，{s2} 字节），跳过: {os.path.basename(path)}")
        return False
    if s2 < MIN_XLSX_BYTES:
        log(f"⚠️ xlsx 体积过小（{s2} 字节 < {MIN_XLSX_BYTES}），跳过: {os.path.basename(path)}")
        return False
    return True


def in_window(now):
    """当前是否在某个计划槽位的执行窗口内

    ⚠️ 终点用 second=59，避免把「slot:45:00.367」这种整分钟末尾的时刻
    误判为超出窗口（replace(minute=X, second=0) 会截断到 45:00.000）
    """
    for h in SLOT_HOURS:
        start = now.replace(hour=h, minute=WINDOW_MIN, second=0, microsecond=0)
        end = now.replace(hour=h, minute=WINDOW_MAX, second=59, microsecond=999999)
        if start <= now <= end:
            return h
    return None


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now()

    # ---- 1. 时间窗守卫 ----
    slot = in_window(now)
    if slot is None:
        log(f"⏭ 不在执行窗口（{now.strftime('%H:%M')}），静默跳过（错过即作废）")
        return 10
    log(f"✅ 进入执行窗口（槽位 {slot}:05 ~ {slot}:45）")

    # ---- 2. 找目标底表 ----
    # 优先等「当前槽位那一小时」的文件；等不到就退回「比已发布版本新的最新文件」
    expect_prefix = f"{now.strftime('%Y-%m-%d')} {slot:02d}_"
    deadline = time.time() + WAIT_FOR_FILE
    chosen = None

    while time.time() < deadline:
        files = list_base_tables()
        hit = [f for f in files if expect_prefix in os.path.basename(f)]
        if hit:
            cand = max(hit, key=slot_ts)
            if file_is_stable(cand):
                chosen = cand
                break
            log(f"⏳ 目标底表仍在写入，稍后重试: {os.path.basename(cand)}")
        else:
            # 没有本槽位的文件（可能 Windows 任务也被错过了）
            newer = [f for f in files
                     if slot_ts(f) and published_slot() and slot_ts(f) > published_slot()]
            if newer:
                cand = max(newer, key=slot_ts)
                if file_is_stable(cand):
                    log(f"ℹ️ 本槽位底表缺失，改用更新的已有文件: {os.path.basename(cand)}")
                    chosen = cand
                    break
        time.sleep(FILE_POLL)

    if not chosen:
        log("❌ 等待底表超时，未找到可用的新底表")
        return 10

    ts = slot_ts(chosen)
    log(f"📊 使用底表: {os.path.basename(chosen)}  (数据时间 {ts:%Y-%m-%d %H:%M})")

    # ---- 3. 判重 ----
    pub = published_slot()
    if pub and ts and ts <= pub:
        log(f"⏭ 该版本已发布过（已发布 {pub:%Y-%m-%d %H:%M}），跳过")
        return 10

    # ---- 4. 构建 + 推送 ----
    cmd = [sys.executable, BUILD_SCRIPT]
    if dry:
        cmd.append("--dry-run")
    log(f"🔨 执行: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=REPO_DIR, timeout=1800)
    except subprocess.TimeoutExpired:
        log("❌ 构建超时")
        return 1
    if r.returncode != 0:
        log(f"❌ 构建失败，退出码 {r.returncode}")
        return 1

    # ---- 5. 读回结果 ----
    try:
        with open(os.path.join(REPO_DIR, DATA_FILE), encoding="utf-8") as f:
            d = json.load(f)
        m = d["meta"]
        log(f"✅ 已更新: 在班 {m.get('inClass', 0):,.0f} | 联报 {m.get('lianbao', 0):,.0f} | "
            f"完成率 {m.get('completeRate', 0):.2f}% | 基线 {m.get('prevUpdated')}")
    except Exception as e:
        log(f"⚠️ 读回结果失败: {e}")

    if dry:
        log("（dry-run，未推送）")
        return 0

    # ---- 6. 推送教学组战报到知音楼群 ----
    # 只推配置了群映射的组（当前：缪家新、朱太阳）
    pushed = []
    try:
        import gen_group_report as grp
        import push_group_report as pgr
        for gname in PUSH_GROUPS:
            if gname not in pgr.GROUP_CHANNELS:
                log(f"⚠️  {gname} 没有群映射，跳过")
                continue
            try:
                report = grp.build_report(gname)
                text = grp.render_text(report)
            except Exception as e:
                log(f"⚠️  {gname} 生成报告失败: {e}")
                continue
            ok, out = pgr.send(text, f"group:{pgr.GROUP_CHANNELS[gname]}")
            if ok:
                t = report["group_total"]
                log(f"📨 {gname} → group:{pgr.GROUP_CHANNELS[gname]} "
                    f"完成率 {t['completeRate']:.2f}% 今日新增 {t['todayNew']:.0f}")
                pushed.append(gname)
            else:
                log(f"❌ {gname} 推送失败: {out[-200:]}")
    except Exception as e:
        log(f"⚠️  战报推送异常（不影响看板）: {e}")

    # ---- 7. 私发用户一条简短通知 ----
    msg = ("📊 续报看板已更新\n"
           f"🔗 https://mjx0217.github.io/xubao-kanban/\n"
           f"数据时间 {ts:%m-%d %H:%M}｜完成率 {m.get('completeRate', 0):.2f}%")
    if pushed:
        msg += "\n已推送战报：" + "、".join(pushed)
    try:
        subprocess.run(
            ["openclaw", "message", "send", "--channel", "yach",
             "--target", "user:285353371458822154", "--message", msg],
            timeout=30, capture_output=True)
        log("📨 已通知用户")
    except Exception as e:
        log(f"⚠️ 通知失败（不影响看板）: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

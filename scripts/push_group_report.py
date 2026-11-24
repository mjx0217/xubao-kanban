#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续报看板 · 教学组战报推送

用法：
  python3 scripts/push_group_report.py 缪家新 [朱太阳 ...]
  python3 scripts/push_group_report.py --all
  python3 scripts/push_group_report.py 缪家新 --dry-run

群映射写在 GROUP_CHANNELS 里，与 MEMORY.md 的「教学组与群ID对照」一致。
"""

import os
import sys
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gen_group_report as grp   # noqa: E402

# 教学组 → 知音楼群ID（来源：MEMORY.md「教学组与群ID对照」）
GROUP_CHANNELS = {
    "缪家新": "63951999989",   # 【政史】家新组-全员起飞🛫️
    "朱太阳": "63952326786",   # 生地登峰搭子🧗‍♂️
}

YACH_ACCOUNT = "5ed0859a08e2bebe"


def send(text, target):
    r = subprocess.run(
        ["openclaw", "message", "send", "--channel", "yach",
         "--target", target, "--message", text],
        capture_output=True, text=True, timeout=60)
    ok = "Sent via Yach" in (r.stdout or "")
    return ok, (r.stdout or "") + (r.stderr or "")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        raise SystemExit("用法: python3 scripts/push_group_report.py <教学组名...> [--dry-run]")

    for gname in args:
        if gname not in GROUP_CHANNELS:
            print(f"⚠️  教学组「{gname}」没有配置群映射，跳过")
            continue
        gid = GROUP_CHANNELS[gname]
        try:
            report = grp.build_report(gname)
        except SystemExit as e:
            print(f"⚠️  {e}")
            continue
        text = grp.render_text(report)
        t = report["group_total"]
        print(f"\n===== {gname} → group:{gid} =====")
        print(f"完成率 {t['completeRate']:.2f}%  今日新增 {t['todayNew']:.0f}  "
              f"距完标 {t['gap']:.0f}  ({len(report['classTeachers'])}人)")
        if dry:
            print(text)
            continue
        ok, out = send(text, f"group:{gid}")
        print(f"{'✅ 已发送' if ok else '❌ 发送失败'}")
        if not ok:
            print(out[-500:])


if __name__ == "__main__":
    main()

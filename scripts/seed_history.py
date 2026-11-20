#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把指定底表构建成历史版本并存入 history/（不推送）

用法: python3 scripts/seed_history.py "底表路径1" "底表路径2" ...
"""
import os
import sys
import json
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bx", os.path.join(HERE, "build_xubao.py"))
bx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bx)

for src in sys.argv[1:]:
    out, updated, token, _pf = bx.build(src_override=src)
    out = bx.clean_json(out)
    hist = os.path.join(bx.REPO_DIR, bx.HISTORY_DIR)
    os.makedirs(hist, exist_ok=True)
    fn = updated.replace(":", "-").replace(" ", "_") + ".json"
    p = os.path.join(hist, fn)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    print(f"✅ 已存档 {fn}  ({os.path.getsize(p)//1024} KB)")

# 沈阳基地 · 续报完成率看板

在线地址：https://mjx0217.github.io/xubao-kanban/

## 数据源

底表（Windows 下载目录 `D:\Downloads`）：

```
26-27学年秋续寒春_实时续报底表-YYYY-MM-DD HH_MM_SS.xlsx
```

脚本自动取文件名时间戳最新的一份，排除 `~$` 开头的 Excel 锁文件和「离线」底表。

## 指标口径

| 指标 | 公式 | 说明 |
|---|---|---|
| 在班人次 | 底表 `在班人次` | |
| 续报人次 | 底表 `续报人次` | |
| 联报人次 | 底表 `联报人次` | |
| 达成人次 | 续报人次 + 联报人次 | 相当于领券表的「领券人次_已联报计入领券」 |
| 目标人次 | 底表 `联报人次目标` | 续报+联报的合并目标 |
| **完成率** | 达成人次 ÷ 目标人次 | 看板核心指标 |
| 续报率 | 续报人次 ÷ 在班人次 | |
| 目标覆盖率 | 目标人次 ÷ 在班人次 | 沈阳当前约 68.7% |
| 缺口人次 | 目标人次 − 达成人次 | |
| 今日新增 | 逐行取 `max(续报人次日增, 联报人次日增)` 后求和 | 两个日增列高度重合，直接相加会双计 |

口径已与 `领券班级明细` 表交叉验证：两表「目标 ÷ 在班」均为 68.7%。

## 维度

- 学科（7 个）
- 教学组（14 个，不含底表教学组为空的脏数据）
- 班主任（299 人）
- 年级（初一 / 初二 / 初三 / 预初）

## 环比

`__lastRates` 字段记录与上一版数据的完成率差值（pp）。
仅当两版 `meta.period` 一致时才计算，避免跨周期（秋续寒春 ↔ 春夏续）串台。

## 更新流程

```bash
cd /home/mjx/repos/xubao-kanban
python3 scripts/build_xubao.py --dry-run   # 只生成不推送
python3 scripts/build_xubao.py             # 生成 + 推送 GitHub
```

推送走 GitHub REST API（WSL 环境 `git push` 会挂死），脚本会自动从 `~/.git-credentials` 读取 token。

生成文件：
- `xubao_data.json` — 看板数据
- `xubao_data_last.json` — 上一版存档

## 目录结构

```
index.html              看板前端
xubao_data.json         当前数据
xubao_data_last.json    上一版存档
scripts/build_xubao.py  数据构建 + 推送脚本
.github/workflows/pages.yml  GitHub Pages 部署
```

## 已知问题

- 「未知」教学组（底表 `教学组` 为 `\N`）只有 1 条脏数据，已在看板中排除，不影响总量。

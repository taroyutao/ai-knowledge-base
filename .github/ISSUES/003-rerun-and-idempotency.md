---
title: "Agent Pipeline: 按日期重跑与幂等去重策略"
labels: ["enhancement"]
---

## Parent

spec/agents-prd.md — 开放问题：重跑策略？

## What to build

管道支持按日期参数手动重跑，并确保幂等——重复运行同一天不产生重复条目。

- CLI 入口：`python -m pipeline run --date 2026-06-15`（默认当天）
- 幂等机制：Organizer 写入前按 `url` 去重（已存在于 `knowledge/articles/` 的同 URL 条目跳过）
- 重跑模式选项：
  - `--force`：覆盖当日所有已存在条目
  - `--skip-existing`（默认）：跳过已入库条目
- Collector 的 `knowledge/raw/` 数据允许覆盖写入（同日期文件直接覆盖）

## Acceptance criteria

- [ ] 支持 `--date YYYYMMDD` 指定采集日期
- [ ] 同一日期多次运行不产生重复 articles 条目（URL 去重生效）
- [ ] `--force` 模式下覆盖已有条目
- [ ] 无 `--force` 时跳过已入库条目

## Blocked by

- #1 Agent Pipeline: 上游节点失败时的下游容错与降级机制
- #2 Agent Pipeline: Agent 间数据传递协议

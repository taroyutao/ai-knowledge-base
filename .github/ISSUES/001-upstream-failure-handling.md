---
title: "Agent Pipeline: 上游节点失败时的下游容错与降级机制"
labels: ["enhancement"]
---

## Parent

spec/agents-prd.md — 开放问题：上游失败下游怎么办？

## What to build

为 LangGraph 三 Agent 串行管道实现失败处理。每个节点（collector/analyzer/organizer）执行失败时，管道应优雅降级而非静默崩溃。

**容错规则**：

- Collector 失败 → 管道终止，记录错误日志，不触发后续节点
- Analyzer 失败 → 保留 Collector 已写入的 `knowledge/raw/` 数据，支持手动/定时重试 Analyzer 节点
- Organizer 失败 → 保留 Analyzer 输出，支持从断点续跑
- 每次运行产生 `knowledge/pipeline-runs/{date}_status.json` 记录各节点状态

**重试策略**：失败节点指数退避重试，最多 3 次。

## Acceptance criteria

- [ ] Collector 失败时管道不执行 Analyzer/Organizer
- [ ] Analyzer 失败时 raw 数据不丢失
- [ ] Organizer 失败时可从 Analyzer 输出断点续跑
- [ ] 每次运行生成 status.json 记录各阶段状态（success / failed / skipped）
- [ ] 失败节点有指数退避重试（最多 3 次）

## Blocked by

None — 可立即开始

---
title: "Agent Pipeline: Agent 间数据传递协议（文件 JSON + Schema 校验）"
labels: ["enhancement"]
---

## Parent

spec/agents-prd.md — 开放问题：数据怎么传？文件 or 消息？

## What to build

采用文件 JSON 传递方案（与 AGENTS.md 第 5 节格式一致），定义三个阶段的传输 Schema：

- **Stage 1**（Collector → Analyzer）：写入 `knowledge/raw/{source}-{date}.json`，格式见 `.opencode/agents/collector.md`
- **Stage 2**（Analyzer → Organizer）：输出写入临时文件 `knowledge/intermediate/{date}_analyzed.json`，格式见 `.opencode/agents/analyzer.md`
- **Stage 3**（Organizer → 输出）：写入 `knowledge/articles/{date}-{source}-{slug}.json`，格式见 AGENTS.md §5

每个节点读取上游文件时执行基本 Schema 校验（必填字段存在、类型正确），校验失败则报错终止。

## Acceptance criteria

- [ ] Collector 产物文件 Schema 定义完成（JSON Schema 或 Pydantic model）
- [ ] Analyzer 输出 Schema 定义完成
- [ ] Organizer 输出 Schema 定义完成（已有标准，只需引用 AGENTS.md）
- [ ] LangGraph 节点间通过文件路径传递数据
- [ ] 上游文件缺失或字段残缺时节点报错并记录

## Blocked by

- #1 Agent Pipeline: 上游节点失败时的下游容错与降级机制

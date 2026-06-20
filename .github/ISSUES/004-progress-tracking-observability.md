---
title: "Agent Pipeline: 运行时进度追踪与可观测性（日志 + 状态文件 + 通知）"
labels: ["enhancement"]
---

## Parent

spec/agents-prd.md — 开放问题：进度追踪？

## What to build

为管道增加全程可观测性，使用标准 `logging` 模块（符合 AGENTS.md 红线第 6 条）。

- 每个节点开始/结束时输出 INFO 级别日志（含条目数量、耗时）
- `knowledge/pipeline-runs/{date}_status.json` 记录详细状态：
  ```json
  {
    "pipeline_run_id": "20260615T000000Z",
    "date": "2026-06-15",
    "stages": {
      "collector": { "status": "success", "items": 50, "duration_ms": 3200, "started_at": "...", "finished_at": "..." },
      "analyzer": { "status": "success", "items": 35, "duration_ms": 45000, "started_at": "...", "finished_at": "..." },
      "organizer": { "status": "success", "published": 28, "rejected": 7, "duration_ms": 1200, "started_at": "...", "finished_at": "..." }
    },
    "overall_status": "success"
  }
  ```
- DEBUG 级别日志记录每次 WebFetch 请求的 URL 和响应时间
- 管道结束后打印一行摘要（如"采集 50 条，分析 35 条，发布 28 条，拒绝 7 条，耗时 52s"）

## Acceptance criteria

- [ ] 所有节点日志使用 `logging` 模块（禁用 `print()`）
- [ ] 每次管道运行产生独立 status.json
- [ ] 日志级别可通过环境变量 `LOG_LEVEL` 控制（默认 INFO）
- [ ] 管道结束时输出一行可读摘要
- [ ] 异常日志包含完整 traceback

## Blocked by

- #1 Agent Pipeline: 上游节点失败时的下游容错与降级机制

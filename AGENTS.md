# AI Knowledge Base Assistant — AGENTS.md

## 1. 项目概述

AI 知识库助手是一个自动化的技术情报聚合与分析系统。它每日从 GitHub Trending 和 Hacker News 等渠道自动采集 AI/LLM/Agent 领域的项目与讨论动态，由 AI Agent 对原始数据进行分类、摘要和去重分析，然后将结构化结果持久化为 JSON 格式的知识条目，最终通过 Telegram、飞书等多渠道分发推送，帮助团队高效追踪技术前沿。

## 2. 技术栈

| 层级 | 技术 |
|------|------|
| 运行环境 | Python 3.12 |
| Agent 编排 | OpenCode + 国产大模型 |
| 工作流引擎 | LangGraph |
| 多渠道分发 | OpenClaw |
| 数据持久化 | JSON 文件存储（`knowledge/` 目录） |

## 3. 编码规范

- **PEP 8**：严格遵循 Python PEP 8 编码风格。
- **命名**：变量、函数、文件名一律使用 `snake_case`；类名使用 `PascalCase`；常量使用 `UPPER_SNAKE_CASE`。
- **类型注解**：所有公共函数和 Agent 节点的输入/输出必须包含完整的类型注解。
- **文档字符串**：采用 Google 风格 docstring，对模块、类、函数进行简洁清晰的描述。

  ```python
  def fetch_trending(lang: str, since: str) -> list[dict]:
      """Fetch trending repositories from GitHub.

      Args:
          lang: Programming language filter (e.g. 'python').
          since: Time range, one of 'daily', 'weekly', 'monthly'.

      Returns:
          A list of raw repository dicts with keys 'name', 'url', 'description'.

      Raises:
          ValueError: If `since` is not a valid time range.
      """
  ```

- **日志取代 print**：**绝对禁止**在任何 Agent 或模块代码中使用裸 `print()`。所有输出必须通过标准库 `logging` 模块完成，日志级别遵循：`DEBUG`（调试信息）、`INFO`（正常流程记录）、`WARNING`（可恢复异常）、`ERROR`（需人工介入的错误）。

  ```python
  # 正确
  logger = logging.getLogger(__name__)
  logger.info("采集完成: %d 条原始数据", len(items))

  # 错误
  print("采集完成:", len(items))
  ```

- **配置管理**：所有密钥、Token、Webhook 地址等敏感信息必须通过环境变量（`os.environ`）读取，**严禁硬编码**。

## 4. 项目结构

```
ai-knowledge-base/
├── AGENTS.md                      # 本文件
├── .opencode/
│   ├── agents/                    # OpenCode Agent 定义
│   │   ├── collector/             # 采集 Agent
│   │   ├── analyzer/              # 分析 Agent
│   │   └── organizer/            # 整理 Agent
│   └── skills/                    # 可复用 Skill 模块
│       ├── github_trending/       # GitHub Trending 采集
│       ├── hackernews/            # Hacker News 采集
│       ├── summarizer/            # AI 摘要生成
│       └── distributor/           # 多渠道分发
├── knowledge/
│   ├── raw/                       # 原始采集数据（原始 HTML/JSON）
│   └── articles/                  # 结构化分析后的知识条目（JSON）
├── pyproject.toml                 # 项目配置与依赖
└── uv.lock                        # 依赖锁定文件
```

## 5. 知识条目 JSON 格式

每条分析后的知识条目存储在 `knowledge/articles/` 下，文件名为 `{source}_{date}_{id}.json`，单文件内容格式如下：

```json
{
  "id": "gh-20260114-001",
  "title": "OpenManus: 开源 Manus AI 智能体框架",
  "source": "github_trending",
  "source_url": "https://github.com/mannaandpoem/OpenManus",
  "author": "mannaandpoem",
  "published_at": "2026-01-14T08:30:00Z",
  "fetched_at": "2026-01-14T12:00:00Z",
  "language": "zh",
  "summary": "OpenManus 是 Manus AI 智能体的开源复现版本，支持多工具调用与任务规划。",
  "tags": ["agent", "llm", "tool-use", "open-source"],
  "category": "agent_framework",
  "difficulty": "advanced",
  "relevance_score": 0.92,
  "status": "published",
  "distributions": {
    "telegram": "sent",
    "feishu": "pending"
  }
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | ✅ | 唯一标识，格式 `{source}-{date}-{seq}` |
| `title` | `str` | ✅ | 知识条目标题 |
| `source` | `str` | ✅ | 来源：`github_trending` / `hackernews` |
| `source_url` | `str` | ✅ | 原始链接 |
| `author` | `str` | ❌ | 作者/提交者 |
| `published_at` | `str` | ❌ | 原文发布时间（ISO 8601） |
| `fetched_at` | `str` | ✅ | 采集时间（ISO 8601） |
| `language` | `str` | ✅ | 摘要语言（`zh` / `en`） |
| `summary` | `str` | ✅ | AI 生成的中文摘要（≤ 200 字） |
| `tags` | `list[str]` | ✅ | 标签列表 |
| `category` | `str` | ✅ | 分类：`agent_framework` / `llm_model` / `tool_library` / `research_paper` / `industry_news` / `other` |
| `difficulty` | `str` | ❌ | 难度：`beginner` / `intermediate` / `advanced` |
| `relevance_score` | `float` | ✅ | 与 AI/Agent 领域的相关度评分（0.0 - 1.0） |
| `status` | `str` | ✅ | 状态：`draft` / `published` / `rejected` |
| `distributions` | `dict` | ✅ | 各渠道分发状态：`pending` / `sent` / `failed` |

## 6. Agent 角色概览

| Agent | 职责 | 输入 | 输出 | 关键 Skill |
|-------|------|------|------|------------|
| **Collector** | 从 GitHub Trending 和 Hacker News 定时拉取 AI/LLM/Agent 相关原始内容 | 定时触发 / 手动触发 | `knowledge/raw/` 下的原始数据 | `github_trending`, `hackernews` |
| **Analyzer** | 对原始数据进行去重、分类、摘要生成、相关度打分，输出结构化 JSON | `knowledge/raw/` 中的待处理数据 | `knowledge/articles/` 下的知识条目 | `summarizer` |
| **Organizer** | 将已发布的知识条目按渠道规则格式化，分发到 Telegram/飞书，记录分发状态 | `knowledge/articles/` 中 `status=published` 的条目 | 各渠道消息 + 更新 `distributions` 状态 | `distributor` |

### 工作流（LangGraph 编排）

```
[定时/手动触发]
       │
       ▼
  ┌──────────┐
  │ Collector │ ──► knowledge/raw/
  └──────────┘
       │
       ▼
  ┌──────────┐
  │ Analyzer  │ ──► knowledge/articles/  (draft → published/rejected)
  └──────────┘
       │
       ▼
  ┌───────────┐
  │ Organizer  │ ──► Telegram / 飞书
  └───────────┘
```

## 7. 红线（绝对禁止）

1. **禁止硬编码任何密钥或 Token**：所有密钥、API Key、Webhook URL、Bot Token 等必须通过环境变量或 `.env` 文件加载，`.env` 文件必须列入 `.gitignore`。
2. **禁止将原始数据（`knowledge/raw/`）提交到版本控制**：该目录已在 `.gitignore` 中排除，仅结构化后的 `articles/` 可纳入版本追踪。
3. **禁止在任何代码或日志中输出用户身份信息或隐私数据**：包括但不限于 IP 地址、手机号、邮箱、Telegram 用户 ID 等。
4. **禁止对第三方接口进行高频无节制请求**：所有外部请求必须设置合理的间隔（≥ 2s）和重试策略（指数退避，最多 3 次）。
5. **禁止绕过 LangGraph 工作流直接修改数据库或 JSON 文件**：所有数据写入必须通过 Agent 节点完成，确保状态一致性与可追溯性。
6. **禁止在 Agent 逻辑中使用裸 `print()` 进行调试**：统一使用 `logging` 模块并设定合适的日志级别。
7. **禁止在未通过 Analyzer 验证的情况下发布条目**：所有知识条目必须先经过 `relevance_score >= 0.6` 的阈值校验才能进入 `published` 状态。

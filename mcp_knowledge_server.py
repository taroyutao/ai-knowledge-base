"""MCP Server for searching the local AI knowledge base.

Reads JSON article files from ``knowledge/articles/`` and exposes three
tools via JSON-RPC 2.0 over stdio:

- **search_articles**: keyword search across titles and summaries.
- **get_article**: fetch full article content by ID.
- **knowledge_stats**: aggregate statistics (counts, sources, tags).

Zero third-party dependencies — only Python 3.12+ stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ARTICLES_DIR = Path(__file__).resolve().parent / "knowledge" / "articles"

SERVER_NAME = "mcp-knowledge-server"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Article loading
# ---------------------------------------------------------------------------


def _load_articles(articles_dir: Path) -> list[dict[str, Any]]:
    """Load all JSON article files from *articles_dir*.

    Args:
        articles_dir: Directory containing ``*.json`` article files.

    Returns:
        A list of parsed article dicts. Malformed files are silently skipped.
    """
    articles: list[dict[str, Any]] = []
    if not articles_dir.is_dir():
        logger.warning("文章目录不存在: %s", articles_dir)
        return articles

    for filepath in sorted(articles_dir.glob("*.json")):
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Normalize: ensure score field exists from relevance_score if needed
                if "score" not in data and "relevance_score" in data:
                    data["score"] = data["relevance_score"]
                articles.append(data)
        except (json.JSONDecodeError, OSError):
            logger.debug("跳过无效文件: %s", filepath)

    return articles


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _search_articles(
    articles: list[dict[str, Any]], keyword: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Search articles by keyword in title and summary.

    Case-insensitive substring matching.

    Args:
        articles: Loaded article list.
        keyword: Search term.
        limit: Maximum results to return.

    Returns:
        A list of matching article dicts with fields ``id``, ``title``,
        ``source``, ``summary``, ``tags``, and ``score``.
    """
    if not keyword:
        return []

    kw = keyword.lower()
    matched: list[dict[str, Any]] = []

    for art in articles:
        title = str(art.get("title", "")).lower()
        summary = str(art.get("summary", "")).lower()
        tags = " ".join(str(t) for t in art.get("tags", [])).lower()

        if kw in title or kw in summary or kw in tags:
            matched.append({
                "id": art.get("id", ""),
                "title": art.get("title", ""),
                "source": art.get("source", ""),
                "source_url": art.get("source_url", ""),
                "summary": (art.get("summary") or "")[:300],
                "tags": art.get("tags", []),
                "score": art.get("score"),
            })

    # Sort by score descending if available, then return limit
    matched.sort(
        key=lambda a: (a.get("score") or 0),
        reverse=True,
    )
    return matched[:limit]


def _get_article(
    articles: list[dict[str, Any]], article_id: str
) -> Optional[dict[str, Any]]:
    """Fetch a single article by its ID.

    Args:
        articles: Loaded article list.
        article_id: The ``id`` field value to match.

    Returns:
        The full article dict, or ``None`` if not found.
    """
    for art in articles:
        if art.get("id") == article_id:
            return art
    return None


def _knowledge_stats(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate statistics for the knowledge base.

    Args:
        articles: Loaded article list.

    Returns:
        A dict with ``total``, ``by_source``, ``by_category``,
        ``by_status``, ``top_tags``, and ``avg_score``.
    """
    total = len(articles)
    if total == 0:
        return {
            "total": 0,
            "by_source": {},
            "by_category": {},
            "by_status": {},
            "top_tags": [],
            "avg_score": 0.0,
        }

    source_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    scores: list[float] = []

    for art in articles:
        src = art.get("source", "unknown")
        source_counter[src] += 1

        cat = art.get("category", "other")
        category_counter[cat] += 1

        st = art.get("status", "draft")
        status_counter[st] += 1

        for tag in art.get("tags", []):
            if isinstance(tag, str):
                tag_counter[tag] += 1

        score = art.get("score") or art.get("relevance_score")
        if isinstance(score, (int, float)):
            scores.append(float(score))

    return {
        "total": total,
        "by_source": dict(source_counter.most_common()),
        "by_category": dict(category_counter.most_common()),
        "by_status": dict(status_counter.most_common()),
        "top_tags": [
            {"tag": tag, "count": count}
            for tag, count in tag_counter.most_common(10)
        ],
        "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
    }


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 handlers
# ---------------------------------------------------------------------------


def _make_response(rpc_id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response.

    Args:
        rpc_id: The request ``id`` field.
        result: The result payload.

    Returns:
        A JSON-RPC response dict.
    """
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _make_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response.

    Args:
        rpc_id: The request ``id`` field.
        code: Error code.
        message: Human-readable error message.

    Returns:
        A JSON-RPC error response dict.
    """
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(rpc_id: Any) -> dict[str, Any]:
    """Respond to the ``initialize`` method.

    Returns:
        Server capabilities and metadata.
    """
    return _make_response(rpc_id, {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
    })


def _handle_tools_list(rpc_id: Any) -> dict[str, Any]:
    """Respond to ``tools/list`` with available tool definitions.

    Returns:
        A list of tool descriptors.
    """
    tools = [
        {
            "name": "search_articles",
            "description": (
                "按关键词搜索知识库文章。在标题和摘要中执行不区分大小写的"
                "子串匹配，按相关度评分降序返回。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大返回条数 (默认 5)",
                        "default": 5,
                    },
                },
                "required": ["keyword"],
            },
        },
        {
            "name": "get_article",
            "description": (
                "按文章 ID 获取完整内容。返回文章的全部字段，"
                "包括标题、摘要、标签、来源链接、评分等。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "article_id": {
                        "type": "string",
                        "description": "文章唯一 ID，如 gh-20260620-001",
                    },
                },
                "required": ["article_id"],
            },
        },
        {
            "name": "knowledge_stats",
            "description": (
                "返回知识库的统计概览：文章总数、来源分布、分类分布、"
                "发布状态分布、热门标签 Top 10、平均评分。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    return _make_response(rpc_id, {"tools": tools})


def _handle_tools_call(
    rpc_id: Any,
    params: dict[str, Any],
    articles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch a ``tools/call`` request to the appropriate handler.

    Args:
        rpc_id: The request ID.
        params: Must contain ``name`` and ``arguments``.
        articles: Loaded article list.

    Returns:
        A JSON-RPC response with the tool result or error.
    """
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    if tool_name == "search_articles":
        keyword = str(arguments.get("keyword", ""))
        limit = int(arguments.get("limit", 5))
        limit = max(1, min(limit, 50))
        results = _search_articles(articles, keyword, limit)
        return _make_response(rpc_id, {
            "content": [
                {"type": "text", "text": json.dumps(results, ensure_ascii=False)}
            ]
        })

    if tool_name == "get_article":
        article_id = str(arguments.get("article_id", ""))
        article = _get_article(articles, article_id)
        if article is None:
            return _make_response(rpc_id, {
                "content": [
                    {"type": "text",
                     "text": json.dumps({"error": f"未找到文章: {article_id}"},
                                        ensure_ascii=False)},
                ]
            })
        return _make_response(rpc_id, {
            "content": [
                {"type": "text", "text": json.dumps(article, ensure_ascii=False)}
            ]
        })

    if tool_name == "knowledge_stats":
        stats = _knowledge_stats(articles)
        return _make_response(rpc_id, {
            "content": [
                {"type": "text", "text": json.dumps(stats, ensure_ascii=False)}
            ]
        })

    return _make_error(rpc_id, -32601, f"未知工具: {tool_name}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _process_request(
    request: dict[str, Any],
    articles: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Process a single JSON-RPC request.

    Returns ``None`` for notifications (no ``id`` field).

    Args:
        request: Parsed JSON-RPC request dict.
        articles: Loaded article list.

    Returns:
        A JSON-RPC response dict, or ``None`` for notifications.
    """
    rpc_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return _handle_initialize(rpc_id)

    if method == "notifications/initialized":
        return None  # Notification, no response

    if method == "tools/list":
        return _handle_tools_list(rpc_id)

    if method == "tools/call":
        return _handle_tools_call(rpc_id, params, articles)

    return _make_error(rpc_id, -32601, f"未知方法: {method}")


def _resolve_articles_dir() -> Path:
    """Determine the articles directory from env or fallback.

    Returns:
        Resolved Path to the articles directory.
    """
    env_dir = os.environ.get("KNOWLEDGE_ARTICLES_DIR", "")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_ARTICLES_DIR


def main() -> None:
    """Run the MCP server main loop (JSON-RPC 2.0 over stdio).

    Reads one JSON-RPC request per line from stdin, writes one response
    per line to stdout.  Logs go to stderr.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    articles_dir = _resolve_articles_dir()
    logger.info("加载文章目录: %s", articles_dir)

    articles = _load_articles(articles_dir)
    logger.info("已加载 %d 篇文章", len(articles))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug("无效 JSON: %s", exc)
            continue

        if not isinstance(request, dict):
            continue

        response = _process_request(request, articles)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

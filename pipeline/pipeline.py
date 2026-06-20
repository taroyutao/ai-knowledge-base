"""Four-step knowledge base automation pipeline.

Usage:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10
    python pipeline/pipeline.py --sources github --limit 5 --dry-run
    python pipeline/pipeline.py --verbose

Steps:
    1. Collect  — GitHub Search API / RSS feeds → knowledge/raw/
    2. Analyze  — LLM summarization, scoring, tagging
    3. Organize — deduplication, format validation
    4. Save     — individual JSON files → knowledge/articles/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Try relative import (package context), fall back to direct import (CLI)
try:
    from .model_client import chat_with_retry, get_provider
except ImportError:
    from model_client import chat_with_retry, get_provider  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT_DIR / "knowledge" / "raw"
ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_QUERY = "topic:agent+topic:llm"

RSS_FEEDS: dict[str, str] = {
    "arxiv_ai": "https://export.arxiv.org/rss/cs.AI",
    "hn_frontpage": "https://hnrss.org/frontpage?q=ai+OR+llm+OR+agent",
}

LLM_ANALYSIS_SYSTEM_PROMPT = (
    "你是一个 AI/LLM 技术分析专家。分析给定的开源项目或文章，"
    "返回严格 JSON 格式的分析结果，不要包含任何额外文本。"
)

LLM_ANALYSIS_USER_PROMPT = """分析以下项目并返回 JSON：

- title: 优化后的中文标题 (≤30字)
- summary: 中文摘要 (≤200字)，突出技术亮点
- tags: 3-5个标签 (小写英文，用-连接多词)
- category: 分类，必须为以下之一：agent_framework / llm_model / tool_library / research_paper / industry_news / other
- difficulty: 难度，必须为以下之一：beginner / intermediate / advanced
- relevance_score: AI/Agent领域相关度 (0.0-1.0)

项目信息：
- 名称: {title}
- URL: {url}
- 原文摘要: {summary}

仅返回 JSON，不包含 ```json``` 标记或任何解释："""

REQUIRED_ARTICLE_FIELDS = frozenset({
    "id", "title", "source", "source_url", "fetched_at",
    "language", "summary", "tags", "category", "relevance_score",
    "status", "distributions",
})

VALID_CATEGORIES = frozenset({
    "agent_framework", "llm_model", "tool_library",
    "research_paper", "industry_news", "other",
})
VALID_DIFFICULTIES = frozenset({"beginner", "intermediate", "advanced"})

HTTP_TIMEOUT = 30.0
MAX_CONCURRENT_LLM = 3

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RawItem:
    """Raw collected item before LLM analysis.

    Attributes:
        title: Original title from source.
        url: Source URL.
        source: Origin name (``github`` / ``rss``).
        summary: Original description or abstract.
        popularity: Optional popularity metric.
    """

    title: str
    url: str
    source: str
    summary: str = ""
    popularity: int = 0


@dataclass
class Article:
    """Structured knowledge article after analysis.

    Attributes:
        id: Unique identifier (source-YYYYMMDD-NNN).
        title: Optimized Chinese title.
        source: Origin source type.
        source_url: Original URL.
        summary: AI-generated Chinese summary.
        tags: Tag list.
        category: Article category.
        difficulty: Difficulty level.
        relevance_score: Relevance to AI/Agent domain.
        status: Publication status.
        raw_item: Original collected item for traceability.
    """

    id: str = ""
    title: str = ""
    source: str = ""
    source_url: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "other"
    difficulty: str = ""
    relevance_score: float = 0.0
    status: str = "draft"
    raw_item: Optional[RawItem] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict matching the knowledge article format."""
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "source_url": self.source_url,
            "author": None,
            "published_at": None,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "language": "zh",
            "summary": self.summary,
            "tags": self.tags,
            "category": self.category,
            "difficulty": self.difficulty,
            "relevance_score": self.relevance_score,
            "status": self.status,
            "distributions": {
                "telegram": "pending",
                "feishu": "pending",
            },
        }


@dataclass
class PipelineStats:
    """Aggregated statistics for a pipeline run.

    Attributes:
        collected: Number of items collected.
        analyzed: Number successfully analyzed by LLM.
        deduped: Number removed as duplicates.
        validated: Number that passed validation.
        saved: Number saved to disk.
        errors: List of error messages.
    """

    collected: int = 0
    analyzed: int = 0
    deduped: int = 0
    validated: int = 0
    saved: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: PipelineStats) -> None:
        """Merge another stats object into this one."""
        self.collected += other.collected
        self.analyzed += other.analyzed
        self.deduped += other.deduped
        self.validated += other.validated
        self.saved += other.saved
        self.errors.extend(other.errors)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


_id_counter: dict[str, int] = {}


def _make_id(source: str) -> str:
    """Generate a unique article ID with auto-incrementing sequence.

    Args:
        source: Short source prefix (e.g. ``gh``, ``rs``).

    Returns:
        An ID like ``gh-20260620-001``.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = _id_counter.get(source, 0) + 1
    _id_counter[source] = seq
    return f"{source}-{today}-{seq:03d}"


def _parse_rss_text(xml_text: str) -> list[dict[str, str]]:
    """Parse RSS/Atom XML using simple regex (no XML library).

    Extracts <title> and <link> from each <item> or <entry>.

    Args:
        xml_text: Raw RSS or Atom feed content.

    Returns:
        List of dicts with ``title``, ``url``, and ``summary`` keys.
    """
    items: list[dict[str, str]] = []

    # Atom: <entry> ... <title>...</title> ... <link href="..."/> ... </entry>
    atom_entries = re.findall(r"<entry>(.*?)</entry>", xml_text, re.DOTALL)
    for block in atom_entries:
        m_title = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
        m_link = re.search(r'<link[^>]*href="([^"]*)"', block)
        m_summary = re.search(
            r"<(?:summary|content)[^>]*>(.*?)</(?:summary|content)>",
            block,
            re.DOTALL,
        )
        title = _strip_html(m_title.group(1)) if m_title else ""
        url = m_link.group(1) if m_link else ""
        summary = _strip_html(m_summary.group(1)) if m_summary else ""
        if title and url:
            items.append({"title": title, "url": url, "summary": summary})

    # RSS 2.0: <item> ... <title>...</title> <link>...</link> ... </item>
    rss_items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)
    for block in rss_items:
        m_title = re.search(r"<title[^>]*>(.*?)</title>", block, re.DOTALL)
        m_link = re.search(r"<link[^>]*>(.*?)</link>", block)
        m_desc = re.search(
            r"<description[^>]*>(.*?)</description>", block, re.DOTALL
        )
        title = _strip_html(m_title.group(1)) if m_title else ""
        url = _strip_html(m_link.group(1)) if m_link else ""
        summary = _strip_html(m_desc.group(1)) if m_desc else ""
        if title and url:
            items.append({"title": title, "url": url, "summary": summary})

    return items


def _strip_html(text: str) -> str:
    """Remove HTML tags, CDATA wrappers, and decode common entities.

    Args:
        text: Raw HTML/XML string, possibly containing CDATA sections.

    Returns:
        Plain text with HTML tags, CDATA wrappers, and entities removed.
    """
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Step 1: Collect
# ---------------------------------------------------------------------------


async def collect_github(limit: int = 20) -> list[RawItem]:
    """Collect trending AI/agent repositories from GitHub Search API.

    Args:
        limit: Maximum number of items to return.

    Returns:
        List of :class:`RawItem` objects.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    per_page = min(limit, 100)
    url = (
        f"{GITHUB_SEARCH_URL}?q={GITHUB_QUERY}"
        f"&sort=stars&order=desc&per_page={per_page}"
    )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("GitHub API 请求失败: %s", exc)
            return []

    items: list[RawItem] = []
    for repo in data.get("items", [])[:limit]:
        items.append(RawItem(
            title=repo.get("full_name", ""),
            url=repo.get("html_url", ""),
            source="github",
            summary=repo.get("description") or "",
            popularity=repo.get("stargazers_count", 0),
        ))

    logger.info("GitHub 采集完成: %d 条", len(items))
    return items


async def collect_rss(limit: int = 20) -> list[RawItem]:
    """Collect AI-related items from configured RSS feeds.

    Args:
        limit: Maximum total items across all feeds.

    Returns:
        List of :class:`RawItem` objects.
    """
    all_items: list[RawItem] = []

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for feed_name, feed_url in RSS_FEEDS.items():
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                entries = _parse_rss_text(resp.text)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("RSS 获取失败 %s: %s", feed_name, exc)
                continue

            for entry in entries:
                all_items.append(RawItem(
                    title=entry["title"],
                    url=entry["url"],
                    source="rss",
                    summary=entry.get("summary", ""),
                ))

    # Deduplicate by URL within RSS results
    seen: set[str] = set()
    unique: list[RawItem] = []
    for item in all_items:
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)

    result = unique[:limit]
    logger.info("RSS 采集完成: %d 条 (去重后)", len(result))
    return result


async def save_raw(items: list[RawItem], source: str) -> Path:
    """Save raw collected items to ``knowledge/raw/`` as JSON.

    Args:
        items: Collected raw items.
        source: Collection source name for filename.

    Returns:
        Path to the saved file.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    filepath = RAW_DIR / f"{source}_{today}.json"

    data = [
        {
            "title": it.title,
            "url": it.url,
            "source": it.source,
            "popularity": it.popularity,
            "summary": it.summary,
        }
        for it in items
    ]
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("原始数据已保存: %s (%d 条)", filepath, len(items))
    return filepath


# ---------------------------------------------------------------------------
# Step 2: Analyze
# ---------------------------------------------------------------------------


def _build_analysis_prompt(item: RawItem) -> list[dict[str, str]]:
    """Build the chat messages for LLM analysis of a single raw item.

    Args:
        item: Raw collected item.

    Returns:
        A list of message dicts with ``role`` and ``content`` keys.
    """
    user_prompt = LLM_ANALYSIS_USER_PROMPT.format(
        title=item.title,
        url=item.url,
        summary=item.summary[:300],
    )
    return [
        {"role": "system", "content": LLM_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _parse_llm_json(raw_text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from LLM response text.

    Handles responses wrapped in ```json fences or with extraneous text.

    Args:
        raw_text: Raw LLM output.

    Returns:
        Parsed JSON dict.

    Raises:
        json.JSONDecodeError: If no valid JSON object can be extracted.
    """
    # Try extracting JSON from ```json ... ``` fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    # Try finding the outermost { ... }
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if m:
        return json.loads(m.group(0))

    # Last resort: try the whole text
    return json.loads(raw_text)


async def analyze_items(
    items: list[RawItem], *, dry_run: bool = False
) -> tuple[list[Article], PipelineStats]:
    """Analyze raw items using LLM to produce structured articles.

    Each item is sent to the LLM for summarization, tagging, categorization,
    and relevance scoring. Items are processed with limited concurrency.

    Args:
        items: Raw collected items.
        dry_run: If True, generate placeholder articles without LLM calls.

    Returns:
        A tuple of (analyzed articles, pipeline statistics).
    """
    stats = PipelineStats()
    if not items:
        return [], stats

    if dry_run:
        articles: list[Article] = []
        for item in items:
            articles.append(Article(
                id=_make_id("dr"),
                title=item.title,
                source=item.source,
                source_url=item.url,
                summary=f"[DRY-RUN] {item.summary[:150]}",
                tags=["ai"],
                category="other",
                relevance_score=0.5,
            ))
        stats.analyzed = len(articles)
        return articles, stats

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)

    async def _analyze_one(item: RawItem) -> Optional[Article]:
        async with semaphore:
            try:
                messages = _build_analysis_prompt(item)
                response = await chat_with_retry(
                    messages, temperature=0.3, max_tokens=1024
                )
                llm_data = _parse_llm_json(response.content)
            except (json.JSONDecodeError, RuntimeError, Exception) as exc:
                logger.warning("LLM 分析失败 [%s]: %s", item.title[:40], exc)
                stats.errors.append(f"analysis:{item.title[:40]}:{exc}")
                return None

            source_prefix = "gh" if item.source == "github" else "rs"
            article = Article(
                id=_make_id(source_prefix),
                title=str(llm_data.get("title", item.title))[:60],
                source=item.source,
                source_url=item.url,
                summary=str(llm_data.get("summary", ""))[:200],
                tags=[
                    t.lower().replace(" ", "-")
                    for t in llm_data.get("tags", [])
                    if isinstance(t, str)
                ][:5],
                category=(
                    llm_data["category"]
                    if llm_data.get("category") in VALID_CATEGORIES
                    else "other"
                ),
                difficulty=(
                    llm_data["difficulty"]
                    if llm_data.get("difficulty") in VALID_DIFFICULTIES
                    else ""
                ),
                relevance_score=float(llm_data.get("relevance_score", 0.5)),
                raw_item=item,
            )
            article.relevance_score = max(0.0, min(1.0, article.relevance_score))
            return article

    tasks = [_analyze_one(item) for item in items]
    results = await asyncio.gather(*tasks)

    articles = [a for a in results if a is not None]
    stats.analyzed = len(articles)
    logger.info("LLM 分析完成: %d/%d 成功", len(articles), len(items))
    return articles, stats


# ---------------------------------------------------------------------------
# Step 3: Organize
# ---------------------------------------------------------------------------


def deduplicate(articles: list[Article]) -> tuple[list[Article], int]:
    """Remove duplicate articles by source_url.

    Args:
        articles: List of articles, possibly with duplicates.

    Returns:
        Tuple of (deduplicated list, number of items removed).
    """
    seen: set[str] = set()
    unique: list[Article] = []
    removed = 0
    for article in articles:
        url = article.source_url.strip().rstrip("/")
        if url in seen:
            removed += 1
            continue
        seen.add(url)
        unique.append(article)
    logger.info("去重: 移除 %d 条, 保留 %d 条", removed, len(unique))
    return unique, removed


def validate_article(article: Article) -> list[str]:
    """Validate a single article's required fields and types.

    Args:
        article: The article to validate.

    Returns:
        A list of error message strings (empty if valid).
    """
    errors: list[str] = []
    d = article.to_dict()

    if not d.get("id") or not isinstance(d["id"], str):
        errors.append("缺少或无效的 id")
    if not d.get("title") or not isinstance(d["title"], str):
        errors.append("缺少或无效的 title")
    if not d.get("source_url") or not isinstance(d["source_url"], str):
        errors.append("缺少或无效的 source_url")
    if not re.match(r"^https?://", d.get("source_url", "")):
        errors.append("source_url 格式无效")
    if not d.get("summary") or not isinstance(d["summary"], str):
        errors.append("缺少或无效的 summary")
    if len(d.get("summary", "")) < 20:
        errors.append(f"summary 过短 ({len(d['summary'])} 字)")
    if not isinstance(d.get("tags"), list) or len(d["tags"]) < 1:
        errors.append("tags 不能为空")
    if d.get("category") not in VALID_CATEGORIES:
        errors.append(f"无效的 category: {d.get('category')}")
    if not isinstance(d.get("relevance_score"), (int, float)):
        errors.append("缺少或无效的 relevance_score")
    else:
        score = float(d["relevance_score"])
        if score < 0.0 or score > 1.0:
            errors.append(f"relevance_score 超出范围: {score}")
    if d.get("status") not in ("draft", "published", "rejected"):
        errors.append(f"无效的 status: {d.get('status')}")

    return errors


def organize(
    articles: list[Article], *, threshold: float = 0.5
) -> tuple[list[Article], PipelineStats]:
    """Deduplicate, validate, and filter articles.

    Articles with relevance_score below *threshold* are rejected (status=rejected)
    but not removed.

    Args:
        articles: Analyzed articles.
        threshold: Minimum relevance_score to keep as draft.

    Returns:
        Tuple of (final articles, stats including dedup/validation counts).
    """
    stats = PipelineStats()
    unique, stats.deduped = deduplicate(articles)

    validated: list[Article] = []
    for article in unique:
        errs = validate_article(article)
        if errs:
            article.status = "rejected"
            for e in errs:
                stats.errors.append(f"validate:{article.id}:{e}")
            logger.warning("校验失败 [%s]: %s", article.id, ", ".join(errs))
            validated.append(article)
            continue

        if article.relevance_score < threshold:
            article.status = "rejected"
            logger.info(
                "低相关度 [%s]: %.2f < %.2f → rejected",
                article.id, article.relevance_score, threshold,
            )

        validated.append(article)
        stats.validated += 1

    logger.info("整理完成: %d 条通过校验", stats.validated)
    return validated, stats


# ---------------------------------------------------------------------------
# Step 4: Save
# ---------------------------------------------------------------------------


def save_articles(
    articles: list[Article], *, dry_run: bool = False
) -> int:
    """Save each article as an individual JSON file.

    Files are written to ``knowledge/articles/`` with the naming scheme
    ``{YYYYMMDD}-{source}-{slug}.json``.

    Args:
        articles: Articles to save.
        dry_run: If True, skip writing and only log.

    Returns:
        Number of articles saved.
    """
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    saved = 0

    for article in articles:
        slug = re.sub(r"[^a-z0-9-]", "", article.title.lower().replace(" ", "-")[:40])
        filename = f"{today}-{article.source}-{slug}.json"
        filepath = ARTICLES_DIR / filename

        if dry_run:
            logger.info("[DRY-RUN] 将保存: %s", filepath)
            saved += 1
            continue

        data = article.to_dict()
        filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved += 1
        logger.debug("已保存: %s", filepath)

    logger.info("保存完成: %d 篇文章", saved)
    return saved


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(
    *,
    sources: list[str],
    limit: int = 20,
    dry_run: bool = False,
    threshold: float = 0.5,
) -> PipelineStats:
    """Execute the full 4-step pipeline.

    Args:
        sources: List of source names (``github``, ``rss``).
        limit: Maximum items to collect per source.
        dry_run: If True, skip LLM calls and file writes.
        threshold: Minimum relevance_score for publishing.

    Returns:
        Aggregated :class:`PipelineStats` across all steps.
    """
    total_stats = PipelineStats()
    all_raw: list[RawItem] = []

    # ── Step 1: Collect ──
    logger.info("=" * 50)
    logger.info("Step 1/4: 采集 (Collect)")
    logger.info("=" * 50)

    if "github" in sources:
        gh_items = await collect_github(limit=limit)
        all_raw.extend(gh_items)
        if gh_items:
            await save_raw(gh_items, "github")

    if "rss" in sources:
        rss_items = await collect_rss(limit=limit)
        all_raw.extend(rss_items)
        if rss_items:
            await save_raw(rss_items, "rss")

    total_stats.collected = len(all_raw)
    if not all_raw:
        logger.warning("未采集到任何数据")
        return total_stats

    # ── Step 2: Analyze ──
    logger.info("=" * 50)
    logger.info("Step 2/4: 分析 (Analyze)")
    logger.info("=" * 50)

    articles, analyze_stats = await analyze_items(all_raw, dry_run=dry_run)
    total_stats.merge(analyze_stats)

    if not articles:
        logger.warning("分析后无有效文章")
        return total_stats

    # ── Step 3: Organize ──
    logger.info("=" * 50)
    logger.info("Step 3/4: 整理 (Organize)")
    logger.info("=" * 50)

    final_articles, org_stats = organize(articles, threshold=threshold)
    total_stats.merge(org_stats)

    # ── Step 4: Save ──
    logger.info("=" * 50)
    logger.info("Step 4/4: 保存 (Save)")
    logger.info("=" * 50)

    saved = save_articles(final_articles, dry_run=dry_run)
    total_stats.saved = saved

    # ── Summary ──
    logger.info("=" * 50)
    logger.info("流水线完成")
    logger.info("  采集:   %d 条", total_stats.collected)
    logger.info("  分析:   %d 条", total_stats.analyzed)
    logger.info("  去重:   %d 条", total_stats.deduped)
    logger.info("  校验:   %d 条", total_stats.validated)
    logger.info("  保存:   %d 篇", total_stats.saved)
    if total_stats.errors:
        logger.info("  错误:   %d 条", len(total_stats.errors))
        for err in total_stats.errors[:10]:
            logger.warning("    - %s", err)
    logger.info("=" * 50)

    return total_stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description="AI 知识库自动化流水线 — 采集、分析、整理、保存",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="github",
        help="数据源, 逗号分隔: github, rss (默认: github)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="每个数据源的最大采集数量 (默认: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式: 跳过 LLM 调用和文件写入",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="最低相关度阈值 (默认: 0.5)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="详细日志输出 (DEBUG 级别)",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    sources = [s.strip().lower() for s in args.sources.split(",")]
    valid_sources = [s for s in sources if s in ("github", "rss")]
    if not valid_sources:
        logger.error("无效的数据源: %s (仅支持 github, rss)", args.sources)
        sys.exit(1)

    logger.info("启动流水线: sources=%s limit=%d dry_run=%s",
                valid_sources, args.limit, args.dry_run)

    try:
        stats = asyncio.run(run_pipeline(
            sources=valid_sources,
            limit=args.limit,
            dry_run=args.dry_run,
            threshold=args.threshold,
        ))
    except KeyboardInterrupt:
        logger.warning("用户中断")
        sys.exit(130)
    except Exception as exc:
        logger.exception("流水线异常: %s", exc)
        sys.exit(1)

    has_errors = len(stats.errors) > 0
    if has_errors:
        logger.warning("流水线完成但有 %d 个错误", len(stats.errors))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

"""Quality scoring for knowledge article JSON files.

Usage:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py knowledge/articles/*.json

Five-dimension weighted scoring (total 100 pts), with grade A/B/C.
Exit 0 if all files grade >= B, exit 1 if any file grades C.
"""

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ID_PATTERN = re.compile(r"^[a-z]+-\d{8}-\d{3}$")
URL_PATTERN = re.compile(r"^https?://")
ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

VALID_STATUSES = frozenset({"draft", "review", "published", "archived"})

SUMMARY_FULL_LENGTH = 50
SUMMARY_BASE_LENGTH = 20

TECH_KEYWORDS: list[str] = [
    # Architecture & patterns
    "transformer", "attention", "encoder", "decoder", "embedding",
    "fine-tuning", "fine-tun", "pre-train", "pretrain", "instruction",
    "RLHF", "DPO", "PPO", "GRPO", "LoRA", "QLoRA", "quantization",
    "distillation", "mixture of experts", "MoE", "multimodal",
    "vision-language", "retrieval-augmented", "retrieval augmented",
    # Agent & tooling
    "agent", "multi-agent", "tool-use", "tool call", "function call",
    "planning", "reasoning", "chain-of-thought", "CoT", "ReAct",
    "memory", "vector database", "RAG", "knowledge graph",
    "workflow", "orchestration", "MCP", "A2A",
    # Models & inference
    "LLM", "SLM", "GPT", "Claude", "Gemini", "DeepSeek", "Qwen",
    "Llama", "Mistral", "Mixtral", "diffusion", "token",
    "inference", "context window", "context length",
    # Engineering
    "benchmark", "evaluation", "hallucination", "alignment",
    "safety", "guardrail", "prompt", "open-source", "API",
    "latency", "throughput", "GPU", "TPU", "deployment",
    # Data
    "dataset", "corpus", "annotation", "synthetic data",
    "training", "zero-shot", "few-shot", "transfer learning",
]

STANDARD_TAGS = frozenset({
    "agent", "llm", "slm", "skill", "tool-use", "tool-call",
    "open-source", "workflow", "framework", "library", "api",
    "best-practices", "engineering", "benchmark", "evaluation",
    "dataset", "training", "fine-tuning", "inference",
    "deployment", "orchestration", "mcp", "a2a",
    "rag", "knowledge-graph", "vector-database", "embedding",
    "memory", "context-engineering", "token-optimization",
    "compression", "quantization", "distillation",
    "prompt-engineering", "prompt", "chain-of-thought",
    "reasoning", "planning", "reliability", "safety",
    "alignment", "guardrail", "security", "privacy",
    "design", "frontend", "ui", "ux", "cli",
    "automation", "content-creation", "video-generation",
    "image-generation", "code-generation", "testing",
    "devops", "ci-cd", "monitoring", "logging",
    "research-paper", "survey", "tutorial",
    "product-management", "industry-news",
    "ai-application", "tool_library", "agent_framework",
    "llm_model", "research_paper", "industry_news",
})

BUZZWORDS_ZH: list[str] = [
    "赋能", "抓手", "闭环", "打通", "全链路", "底层逻辑",
    "颗粒度", "对齐", "拉通", "沉淀", "强大的", "革命性的",
]

BUZZWORDS_EN: list[str] = [
    "groundbreaking", "revolutionary", "game-changing", "cutting-edge",
    "disruptive", "paradigm shift", "synergy", "best-in-class",
    "world-class", "state-of-the-art", "unprecedented", "next-generation",
    "bleeding-edge", "innovative", "holistic", "robust",
]

# Scoring weights
MAX_SUMMARY = 25
MAX_DEPTH = 25
MAX_FORMAT = 20
MAX_TAGS = 15
MAX_BUZZ = 15

GRADE_THRESHOLD_A = 80
GRADE_THRESHOLD_B = 60

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DimensionScore:
    """Score for a single quality dimension.

    Attributes:
        name: Human-readable dimension name.
        score: Actual score (0 to max_score).
        max_score: Maximum possible score for this dimension.
        details: Brief explanation of how the score was computed.
    """

    name: str
    score: float
    max_score: int
    details: str = ""


@dataclass
class QualityReport:
    """Complete quality assessment for one article.

    Attributes:
        filepath: Path to the JSON file evaluated.
        dimensions: Per-dimension score breakdown.
        total_score: Weighted total (0-100).
        grade: Letter grade A/B/C.
    """

    filepath: Path
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: float = 0.0
    grade: str = "C"


# ---------------------------------------------------------------------------
# File gathering
# ---------------------------------------------------------------------------


def _gather_files(paths: list[str]) -> list[Path]:
    """Resolve glob patterns and collect unique JSON file paths.

    Args:
        paths: A list of file paths or glob patterns.

    Returns:
        A sorted list of unique Path objects.
    """
    files: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_absolute():
            glob_path = p
        else:
            glob_path = Path.cwd() / p

        if "*" in raw or "?" in raw:
            parent = glob_path.parent
            pattern = glob_path.name
            matched = list(parent.glob(pattern)) if parent.exists() else []
            files.update(f for f in matched if f.is_file())
        elif glob_path.is_file():
            files.add(glob_path)
        else:
            logger.warning("文件不存在或无法读取: %s", raw)
    return sorted(files)


# ---------------------------------------------------------------------------
# Dimension 1: summary quality (25 pts)
# ---------------------------------------------------------------------------


def _score_summary(data: dict[str, Any]) -> DimensionScore:
    """Score summary quality based on length and tech keywords.

    Args:
        data: Parsed JSON dict.

    Returns:
        A DimensionScore with name '摘要质量'.
    """
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        return DimensionScore("摘要质量", 0, MAX_SUMMARY, "summary 类型错误")

    length = len(summary)
    if length >= SUMMARY_FULL_LENGTH:
        base = 20.0
        detail = f"长度 {length}/{SUMMARY_FULL_LENGTH} 字 (满分)"
    elif length >= SUMMARY_BASE_LENGTH:
        base = 10.0
        detail = f"长度 {length}/{SUMMARY_FULL_LENGTH} 字 (基本分)"
    else:
        base = 0.0
        detail = f"长度 {length} 字 (不足 {SUMMARY_BASE_LENGTH})"

    summary_lower = summary.lower()
    keyword_hits = sum(
        1 for kw in TECH_KEYWORDS if kw.lower() in summary_lower
    )
    bonus = min(keyword_hits * 1.0, 5.0)
    if bonus > 0:
        detail += f", 技术关键词 +{bonus:.0f} ({keyword_hits} 个)"

    total = min(base + bonus, 25.0)
    return DimensionScore("摘要质量", total, MAX_SUMMARY, detail)


# ---------------------------------------------------------------------------
# Dimension 2: technical depth (25 pts)
# ---------------------------------------------------------------------------


def _score_technical_depth(data: dict[str, Any]) -> DimensionScore:
    """Score technical depth from the score/relevance_score field.

    Uses ``score`` (1-10) if present, otherwise maps ``relevance_score``
    (0.0-1.0) to a 1-10 scale.

    Args:
        data: Parsed JSON dict.

    Returns:
        A DimensionScore with name '技术深度'.
    """
    if "score" in data and isinstance(data["score"], (int, float)):
        raw = float(data["score"])
        if raw < 1 or raw > 10:
            return DimensionScore("技术深度", 0, MAX_DEPTH,
                                  f"score 超出 1-10 范围 ({raw})")
        mapped = (raw / 10.0) * 25.0
        return DimensionScore("技术深度", mapped, MAX_DEPTH,
                              f"score={raw:.1f} → {mapped:.1f}/25")
    if "relevance_score" in data and isinstance(data["relevance_score"], (int, float)):
        raw = float(data["relevance_score"])
        if raw < 0.0 or raw > 1.0:
            return DimensionScore("技术深度", 0, MAX_DEPTH,
                                  f"relevance_score 超出 0-1 范围 ({raw})")
        mapped = raw * 25.0
        return DimensionScore("技术深度", mapped, MAX_DEPTH,
                              f"relevance_score={raw:.2f} → {mapped:.1f}/25")
    return DimensionScore("技术深度", 0, MAX_DEPTH, "缺少 score / relevance_score 字段")


# ---------------------------------------------------------------------------
# Dimension 3: format compliance (20 pts)
# ---------------------------------------------------------------------------


def _is_valid_iso8601(value: str) -> bool:
    """Check whether a string looks like ISO 8601 datetime."""
    return bool(ISO8601_PATTERN.match(value))


def _score_format(data: dict[str, Any]) -> DimensionScore:
    """Score format compliance for id, title, source_url, status, timestamp.

    Each of the five items is worth 4 points.

    Args:
        data: Parsed JSON dict.

    Returns:
        A DimensionScore with name '格式规范'.
    """
    score = 0.0
    parts: list[str] = []

    # id
    rid = data.get("id")
    if isinstance(rid, str) and ID_PATTERN.match(rid):
        score += 4
        parts.append("id ✓")
    elif isinstance(rid, str):
        parts.append(f"id ✗ (格式错误: {rid})")
    else:
        parts.append("id ✗ (缺失/类型错误)")

    # title
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        score += 4
        parts.append("title ✓")
    else:
        parts.append("title ✗ (缺失/为空)")

    # source_url
    url = data.get("source_url")
    if isinstance(url, str) and URL_PATTERN.match(url):
        score += 4
        parts.append("source_url ✓")
    elif isinstance(url, str):
        parts.append("source_url ✗ (格式无效)")
    else:
        parts.append("source_url ✗ (缺失/类型错误)")

    # status
    status = data.get("status")
    if isinstance(status, str) and status in VALID_STATUSES:
        score += 4
        parts.append("status ✓")
    elif isinstance(status, str):
        parts.append(f"status ✗ ({status})")
    else:
        parts.append("status ✗ (缺失/类型错误)")

    # timestamp (fetched_at or published_at)
    fetched = data.get("fetched_at")
    published = data.get("published_at")
    ts_ok = (isinstance(fetched, str) and _is_valid_iso8601(fetched)) or \
            (isinstance(published, str) and _is_valid_iso8601(published))
    if ts_ok:
        score += 4
        parts.append("timestamp ✓")
    else:
        parts.append("timestamp ✗ (无有效 ISO 8601 时间戳)")

    detail = ", ".join(parts)
    return DimensionScore("格式规范", score, MAX_FORMAT, detail)


# ---------------------------------------------------------------------------
# Dimension 4: tag precision (15 pts)
# ---------------------------------------------------------------------------


def _score_tags(data: dict[str, Any]) -> DimensionScore:
    """Score tag precision based on count and standard tag coverage.

    - Count: 1-3 tags → 5 pts base, 4-5 → 3 pts, 6+ → 1 pt, 0 → 0 pts.
    - Coverage: proportion of tags in STANDARD_TAGS × 10 bonus pts.

    Args:
        data: Parsed JSON dict.

    Returns:
        A DimensionScore with name '标签精度'.
    """
    tags = data.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return DimensionScore("标签精度", 0, MAX_TAGS, "tags 缺失或格式错误")

    count = len(tags)
    if count == 0:
        base = 0.0
    elif 1 <= count <= 3:
        base = 5.0
    elif 4 <= count <= 5:
        base = 3.0
    else:
        base = 1.0

    matched = sum(1 for t in tags if t.lower() in STANDARD_TAGS)
    coverage = matched / count if count > 0 else 0.0
    bonus = round(coverage * 10.0, 1)

    total = base + bonus
    detail = (
        f"{count} 个标签 (base={base:.0f}), "
        f"标准命中 {matched}/{count} (bonus={bonus:.1f})"
    )
    return DimensionScore("标签精度", total, MAX_TAGS, detail)


# ---------------------------------------------------------------------------
# Dimension 5: buzzword detection (15 pts)
# ---------------------------------------------------------------------------


def _score_buzzwords(data: dict[str, Any]) -> DimensionScore:
    """Detect buzzwords in title and summary; deduct for each hit.

    Starts at 15 and subtracts 2 points per unique buzzword found.

    Args:
        data: Parsed JSON dict.

    Returns:
        A DimensionScore with name '空洞词检测'.
    """
    title = data.get("title", "")
    summary = data.get("summary", "")
    if not isinstance(title, str):
        title = ""
    if not isinstance(summary, str):
        summary = ""
    combined = f"{title}\n{summary}"

    found: list[str] = []
    for word in BUZZWORDS_ZH:
        if word in combined:
            found.append(word)
    combined_lower = combined.lower()
    for word in BUZZWORDS_EN:
        if word.lower() in combined_lower:
            found.append(word)

    deduped = list(dict.fromkeys(found))
    penalty = len(deduped) * 2
    score = max(15.0 - penalty, 0.0)

    if deduped:
        detail = f"发现 {len(deduped)} 个空洞词: {', '.join(deduped[:5])}"
    else:
        detail = "未发现空洞词"

    return DimensionScore("空洞词检测", score, MAX_BUZZ, detail)


# ---------------------------------------------------------------------------
# Main scoring logic
# ---------------------------------------------------------------------------


def _make_progress_bar(score: float, max_score: float = 100.0,
                       width: int = 20) -> str:
    """Build a text progress bar string.

    Args:
        score: Current score.
        max_score: Maximum score.
        width: Total character width of the bar.

    Returns:
        A string like ``[████████░░░░░░] 75/100``.
    """
    ratio = max(min(score / max_score, 1.0), 0.0)
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:.1f}/{max_score:.0f}"


def _determine_grade(total: float) -> str:
    """Map total score to grade A/B/C.

    Args:
        total: Weighted total score (0-100).

    Returns:
        'A' for >= 80, 'B' for >= 60, 'C' otherwise.
    """
    if total >= GRADE_THRESHOLD_A:
        return "A"
    if total >= GRADE_THRESHOLD_B:
        return "B"
    return "C"


def assess_file(filepath: Path) -> QualityReport:
    """Run all five quality dimensions against a single JSON file.

    Args:
        filepath: Path to the JSON file.

    Returns:
        A QualityReport with scores, total, and grade.
    """
    report = QualityReport(filepath=filepath)

    try:
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        report.dimensions = [
            DimensionScore(
                "摘要质量", 0, MAX_SUMMARY, f"文件读取失败: {exc}"
            ),
            DimensionScore("技术深度", 0, MAX_DEPTH, ""),
            DimensionScore("格式规范", 0, MAX_FORMAT, ""),
            DimensionScore("标签精度", 0, MAX_TAGS, ""),
            DimensionScore("空洞词检测", 0, MAX_BUZZ, ""),
        ]
        report.total_score = 0.0
        report.grade = "C"
        return report

    if not isinstance(data, dict):
        report.dimensions = [
            DimensionScore(
                "摘要质量", 0, MAX_SUMMARY, "JSON 顶层不是 dict"
            )
        ]
        report.total_score = 0.0
        report.grade = "C"
        return report

    dims = [
        _score_summary(data),
        _score_technical_depth(data),
        _score_format(data),
        _score_tags(data),
        _score_buzzwords(data),
    ]
    report.dimensions = dims
    report.total_score = sum(d.score for d in dims)
    report.grade = _determine_grade(report.total_score)
    return report


def main() -> None:
    """Entry point: parse args, score files, display report, set exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    if len(sys.argv) < 2:
        logger.info("用法: python hooks/check_quality.py <json_file> [json_file2 ...]")
        sys.exit(1)

    raw_paths = sys.argv[1:]
    files = _gather_files(raw_paths)

    if not files:
        logger.info("未找到任何 JSON 文件")
        sys.exit(1)

    logger.info("质量评分 — %d 个文件", len(files))
    logger.info("=" * 55)

    has_c_grade = False
    grade_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    score_sum = 0.0

    for filepath in files:
        report = assess_file(filepath)
        grade_counts[report.grade] += 1
        score_sum += report.total_score
        if report.grade == "C":
            has_c_grade = True

        bar = _make_progress_bar(report.total_score)
        logger.info("")
        logger.info("  📄 %s", filepath.name)
        logger.info("  %s  %s", bar, report.grade)
        logger.info("  ───────────────────────────────────────")
        for dim in report.dimensions:
            dim_bar = _make_progress_bar(dim.score, dim.max_score, 12)
            logger.info("  %s %s", dim_bar, dim.name)
            if dim.details:
                logger.info("           %s", dim.details)

    avg_score = score_sum / len(files) if files else 0.0

    logger.info("")
    logger.info("=" * 55)
    logger.info("汇总统计")
    logger.info("  文件总数: %d", len(files))
    logger.info("  平均分:   %.1f / 100", avg_score)
    logger.info("  A 级:     %d (≥%d)", grade_counts["A"], GRADE_THRESHOLD_A)
    logger.info("  B 级:     %d (≥%d)", grade_counts["B"], GRADE_THRESHOLD_B)
    logger.info("  C 级:     %d (<%d)", grade_counts["C"], GRADE_THRESHOLD_B)
    logger.info("=" * 55)

    if has_c_grade:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()

"""Validate knowledge article JSON files.

Usage:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py knowledge/articles/*.json

Exit 0 if all files pass, exit 1 if any file has errors.
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES = frozenset({"draft", "review", "published", "archived"})
VALID_AUDIENCES = frozenset({"beginner", "intermediate", "advanced"})
ID_PATTERN = re.compile(r"^[a-z]+-\d{8}-\d{3}$")
URL_PATTERN = re.compile(r"^https?://")
SUMMARY_MIN_LENGTH = 20
SCORE_MIN = 1
SCORE_MAX = 10


def _gather_files(paths: list[str]) -> list[Path]:
    """Resolve glob patterns and collect unique JSON file paths.

    Args:
        paths: A list of file paths or glob patterns.

    Returns:
        A sorted list of unique :class:`Path` objects.
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


def _validate_field_types(data: dict[str, Any], filepath: Path) -> list[str]:
    """Check required fields exist and have correct types.

    Args:
        data: Parsed JSON dict.
        filepath: Source file path for error messages.

    Returns:
        A list of error message strings.
    """
    errors: list[str] = []
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"{filepath}: 缺少必填字段 '{field}'")
        elif not isinstance(data[field], expected_type):
            actual = type(data[field]).__name__
            expected = expected_type.__name__
            errors.append(
                f"{filepath}: 字段 '{field}' 类型错误 "
                f"(期望 {expected}, 实际 {actual})"
            )
    return errors


def _validate_id(value: str, filepath: Path) -> list[str]:
    """Validate ID format: {source}-{YYYYMMDD}-{NNN}.

    Args:
        value: The id field value.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if not ID_PATTERN.match(value):
        return [f"{filepath}: ID 格式错误 '{value}' (期望格式: source-YYYYMMDD-NNN)"]
    return []


def _validate_status(value: str, filepath: Path) -> list[str]:
    """Validate status is one of the allowed values.

    Args:
        value: The status field value.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if value not in VALID_STATUSES:
        allowed = "/".join(sorted(VALID_STATUSES))
        return [f"{filepath}: status 值无效 '{value}' (允许: {allowed})"]
    return []


def _validate_url(value: str, filepath: Path) -> list[str]:
    """Validate source_url starts with http:// or https://.

    Args:
        value: The source_url field value.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if not URL_PATTERN.match(value):
        return [f"{filepath}: source_url 格式无效 '{value}' (必须以 http:// 或 https:// 开头)"]
    return []


def _validate_summary(value: str, filepath: Path) -> list[str]:
    """Validate summary meets minimum length.

    Args:
        value: The summary field value.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if len(value) < SUMMARY_MIN_LENGTH:
        return [
            f"{filepath}: summary 过短 ({len(value)} 字, 最少 {SUMMARY_MIN_LENGTH} 字)"
        ]
    return []


def _validate_tags(value: list[str], filepath: Path) -> list[str]:
    """Validate tags is a non-empty list of strings.

    Args:
        value: The tags field value.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if len(value) < 1:
        return [f"{filepath}: tags 不能为空 (至少需要 1 个标签)"]
    return []


def _validate_score(data: dict[str, Any], filepath: Path) -> list[str]:
    """Validate optional 'score' field is in 1-10 range.

    Args:
        data: Parsed JSON dict.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if "score" not in data:
        return []
    score = data["score"]
    if not isinstance(score, (int, float)):
        return [f"{filepath}: score 类型错误 (期望 int/float)"]
    if score < SCORE_MIN or score > SCORE_MAX:
        return [
            f"{filepath}: score 超出范围 ({score}, 允许 {SCORE_MIN}-{SCORE_MAX})"
        ]
    return []


def _validate_audience(data: dict[str, Any], filepath: Path) -> list[str]:
    """Validate optional 'audience' field value.

    Args:
        data: Parsed JSON dict.
        filepath: Source file path.

    Returns:
        A list of error message strings.
    """
    if "audience" not in data:
        return []
    audience = data["audience"]
    if audience not in VALID_AUDIENCES:
        allowed = "/".join(sorted(VALID_AUDIENCES))
        return [
            f"{filepath}: audience 值无效 '{audience}' (允许: {allowed})"
        ]
    return []


def validate_file(filepath: Path) -> list[str]:
    """Run all validation rules against a single JSON file.

    Args:
        filepath: Path to the JSON file.

    Returns:
        A list of error message strings (empty means pass).
    """
    errors: list[str] = []

    try:
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        return [f"{filepath}: JSON 解析失败 - {exc}"]
    except OSError as exc:
        return [f"{filepath}: 文件读取失败 - {exc}"]

    if not isinstance(data, dict):
        return [f"{filepath}: 顶层元素应为 dict, 实际为 {type(data).__name__}"]

    errors.extend(_validate_field_types(data, filepath))
    # Only validate field values if the fields exist and have correct types
    if "id" in data and isinstance(data["id"], str):
        errors.extend(_validate_id(data["id"], filepath))
    if "status" in data and isinstance(data["status"], str):
        errors.extend(_validate_status(data["status"], filepath))
    if "source_url" in data and isinstance(data["source_url"], str):
        errors.extend(_validate_url(data["source_url"], filepath))
    if "summary" in data and isinstance(data["summary"], str):
        errors.extend(_validate_summary(data["summary"], filepath))
    if "tags" in data and isinstance(data["tags"], list):
        errors.extend(_validate_tags(data["tags"], filepath))

    errors.extend(_validate_score(data, filepath))
    errors.extend(_validate_audience(data, filepath))

    return errors


def main() -> None:
    """Entry point: parse args, validate files, print report, set exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if len(sys.argv) < 2:
        logger.error("用法: python hooks/validate_json.py <json_file> [json_file2 ...]")
        sys.exit(1)

    raw_paths = sys.argv[1:]
    files = _gather_files(raw_paths)

    if not files:
        logger.error("未找到任何 JSON 文件")
        sys.exit(1)

    logger.info("校验 %d 个文件...", len(files))

    total_files = len(files)
    passed = 0
    failed = 0
    all_errors: list[str] = []

    for filepath in files:
        file_errors = validate_file(filepath)
        if file_errors:
            failed += 1
            all_errors.extend(file_errors)
        else:
            passed += 1
            logger.debug("通过: %s", filepath)

    logger.info("")
    logger.info("=" * 50)
    logger.info("校验完成")

    if all_errors:
        logger.info("")
        logger.info("错误列表:")
        for err in all_errors:
            logger.error("  - %s", err)
        logger.info("")

    logger.info("文件总数:  %d", total_files)
    logger.info("通过:      %d", passed)
    logger.info("失败:      %d", failed)
    logger.info("错误总数:  %d", len(all_errors))
    logger.info("=" * 50)

    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()

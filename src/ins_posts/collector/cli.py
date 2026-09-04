"""Command-line entry point for the Instagram post collector."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import os
import random
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from ..core.discovery import (
    DEFAULT_DISCOVERY_SOURCES,
    MAX_DISCOVERY_SOURCES,
    MIN_DISCOVERY_SOURCES,
    DiscoveryPlan,
    create_discovery_plan,
    shuffle_discovered_posts,
)
from ..core.filters import FilterSpec, FilterValidationError, apply_filters
from ..core.normalize import merge_posts, normalize_events

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._]{1,30}")
SHORTCODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
DELAY_PATTERN = re.compile(r"(?P<low>\d+(?:\.\d+)?)(?:-(?P<high>\d+(?:\.\d+)?))?")
ALLOWED_INCLUDES = {"posts", "reels"}
SUPPORTED_BROWSERS = (
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "floorp",
    "librewolf",
    "opera",
    "thorium",
    "vivaldi",
    "zen",
)
if sys.platform == "darwin":
    SUPPORTED_BROWSERS += ("orion", "safari")


class CollectorError(RuntimeError):
    """A user-facing collector error."""


def _canonical_instagram_path(path: str) -> str | None:
    """Allow only the post-oriented Instagram routes this tool promises."""
    if "\\" in path:
        return None
    stripped = path.strip("/")
    parts = stripped.split("/") if stripped else []
    if not parts or any(not part for part in parts):
        return None

    if len(parts) == 1 and USERNAME_PATTERN.fullmatch(parts[0]):
        return f"/{parts[0]}/"
    if (
        len(parts) == 2
        and USERNAME_PATTERN.fullmatch(parts[0])
        and parts[1] in {"posts", "reels"}
    ):
        return f"/{parts[0]}/{parts[1]}/"
    if (
        len(parts) == 2
        and parts[0] in {"p", "reel", "reels", "tv"}
        and SHORTCODE_PATTERN.fullmatch(parts[1])
    ):
        return f"/{parts[0]}/{parts[1]}/"
    if (
        len(parts) == 3
        and USERNAME_PATTERN.fullmatch(parts[0])
        and parts[1] in {"p", "reel", "reels", "tv"}
        and SHORTCODE_PATTERN.fullmatch(parts[2])
    ):
        return f"/{parts[0]}/{parts[1]}/{parts[2]}/"
    if len(parts) == 3 and parts[:2] == ["explore", "tags"]:
        tag = unquote(parts[2]).strip()
        if tag and len(tag) <= 100 and not any(
            character.isspace() or ord(character) < 32 or character in "/\\"
            for character in tag
        ):
            return f"/explore/tags/{quote(tag, safe='')}/"
    if parts[0] == "share":
        if len(parts) == 2 and SHORTCODE_PATTERN.fullmatch(parts[1]):
            return f"/share/{parts[1]}/"
        if (
            len(parts) == 3
            and parts[1] in {"p", "reel", "reels", "tv"}
            and SHORTCODE_PATTERN.fullmatch(parts[2])
        ):
            return f"/share/{parts[1]}/{parts[2]}/"
    return None


def normalize_target(value: str) -> str:
    value = value.strip()
    value = value.removeprefix("@")
    if USERNAME_PATTERN.fullmatch(value):
        return f"https://www.instagram.com/{value}/"

    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise CollectorError("Instagram 目标 URL 格式无效。") from exc
    path = _canonical_instagram_path(parsed.path)
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in {"instagram.com", "www.instagram.com", "m.instagram.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or path is None
    ):
        raise CollectorError(
            "目标必须是 Instagram 用户名，或 Instagram 帖子、Reel、Hashtag URL。"
        )
    return f"https://www.instagram.com{path}"


def validate_delay(value: str) -> str:
    match = DELAY_PATTERN.fullmatch(value.strip())
    if not match:
        raise argparse.ArgumentTypeError("请求间隔格式应为 6、6.5 或 6-12。")
    low = float(match.group("low"))
    high = float(match.group("high") or low)
    if low < 1 or high < low:
        raise argparse.ArgumentTypeError(
            "请求间隔下限至少为 1 秒，且上限不能小于下限。"
        )
    return value.strip()


def validate_include(value: str) -> str:
    values = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not values or not set(values).issubset(ALLOWED_INCLUDES):
        allowed = ", ".join(sorted(ALLOWED_INCLUDES))
        raise argparse.ArgumentTypeError(f"--include 仅支持：{allowed}。")
    return ",".join(dict.fromkeys(values))


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数。") from exc
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("允许范围为 1 到 1000。")
    return number


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数。") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0。")
    return number


def discovery_sources_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数。") from exc
    if not MIN_DISCOVERY_SOURCES <= number <= MAX_DISCOVERY_SOURCES:
        raise argparse.ArgumentTypeError(
            f"随机来源数必须在 {MIN_DISCOVERY_SOURCES} 到 "
            f"{MAX_DISCOVERY_SOURCES} 之间。"
        )
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ins-posts",
        description="低频采集 Instagram 帖子基础元数据，并可选择下载媒体文件。",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="高级模式：用户名、@用户名，或 Instagram 帖子、Reel、Hashtag URL",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="随机发现模式；未填写 target 时默认启用",
    )
    parser.add_argument(
        "--random-sources",
        type=discovery_sources_int,
        default=DEFAULT_DISCOVERY_SOURCES,
        help=f"随机抽取的内置主题来源数（默认：{DEFAULT_DISCOVERY_SOURCES}）",
    )
    parser.add_argument(
        "--random-seed",
        type=nonnegative_int,
        help="复现随机来源和结果顺序的种子；默认使用系统随机数",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出目录；默认写入 ./data/random-<时间> 或 ./data/<目标>-<时间>",
    )
    parser.add_argument(
        "-n",
        "--max-posts",
        type=positive_int,
        default=20,
        help="随机模式每来源、指定账号每类最多扫描的候选数（默认：20）",
    )
    parser.add_argument(
        "--include",
        type=validate_include,
        default="posts",
        help="指定账号的内容类型：posts、reels 或 posts,reels；随机模式忽略",
    )
    filters = parser.add_argument_group("结果筛选（不同条件之间为 AND）")
    filters.add_argument("--since", metavar="YYYY-MM-DD", help="发布日期下限（含）")
    filters.add_argument("--until", metavar="YYYY-MM-DD", help="发布日期上限（含）")
    filters.add_argument("--min-likes", type=nonnegative_int, help="最低点赞数（含）")
    filters.add_argument("--max-likes", type=nonnegative_int, help="最高点赞数（含）")
    filters.add_argument(
        "--keywords", help="文案关键词，逗号分隔；按字面匹配，不接受正则表达式"
    )
    filters.add_argument(
        "--keyword-mode",
        choices=("any", "all"),
        default="any",
        help="关键词命中任意一个或全部（默认：any）",
    )
    filters.add_argument(
        "--hashtags", help="Hashtag，逗号分隔，可省略 #，忽略大小写"
    )
    filters.add_argument(
        "--hashtag-mode",
        choices=("any", "all"),
        default="any",
        help="Hashtag 命中任意一个或全部（默认：any）",
    )
    filters.add_argument(
        "--media-type",
        choices=("all", "image", "video"),
        default="all",
        help="全部、包含图片或包含视频（默认：all）",
    )
    filters.add_argument(
        "--max-results",
        type=positive_int,
        help="筛选后最多保留多少条；不填则保留所有命中结果",
    )
    parser.add_argument(
        "--download-media",
        action="store_true",
        help="同时下载图片和视频；使用本地 archive 避免重复下载",
    )
    parser.add_argument("--cookies", type=Path, help="Netscape 格式 Cookie 文件")
    parser.add_argument(
        "--cookies-from-browser",
        choices=SUPPORTED_BROWSERS,
        help="从指定浏览器读取 Instagram Cookie；只在显式指定时发生",
    )
    parser.add_argument(
        "--request-delay",
        type=validate_delay,
        default="6-12",
        help="请求间随机等待秒数（默认：6-12；下限不得低于 1）",
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="额外保留 gallery-dl 原始事件 JSON，便于排错",
    )
    parser.add_argument(
        "--replace", action="store_true", help="覆盖 posts.jsonl，不与已有记录合并"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="显示上游调试日志")
    return parser


def _target_slug(target: str) -> str:
    parsed = urlparse(target)
    parts = [part for part in parsed.path.split("/") if part]
    value = "-".join(parts[-2:]) if parts else "instagram"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "instagram"


def _default_output(target: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / "data" / f"{_target_slug(target)}-{timestamp}"


def _default_random_output() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / "data" / f"random-{timestamp}"


def _common_gallery_args(
    args: argparse.Namespace,
    output: Path,
    *,
    include: str | None = None,
    post_shortcodes: Sequence[str] | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--config-ignore",
        "--no-input",
        "--no-colors",
        "--retries",
        "2",
        "--sleep-request",
        args.request_delay,
        "--sleep-429",
        "60-120",
        "--cache-file",
        str((output / "gallery-dl-cache.sqlite3").resolve()),
        "-o",
        f"extractor.instagram.max-posts={args.max_posts}",
        "-o",
        f"extractor.instagram.include={include or args.include}",
        "-o",
        "extractor.instagram.videos=merged",
        "-o",
        "extractor.instagram.cookies-update=false",
    ]
    if args.cookies_from_browser:
        command.extend(
            ["--cookies-from-browser", f"{args.cookies_from_browser}/instagram.com"]
        )
    elif args.cookies:
        command.extend(["--cookies", str(args.cookies.resolve())])
    if args.verbose:
        command.append("--verbose")
    if post_shortcodes is not None:
        command.extend(["--post-filter", _shortcode_filter(post_shortcodes)])
    return command


def _shortcode_filter(shortcodes: Sequence[str]) -> str:
    """Build a trusted gallery-dl expression from normalized Instagram IDs."""
    values = tuple(dict.fromkeys(shortcodes))
    if not values:
        raise CollectorError("没有可用于媒体下载的帖子 shortcode。")
    if len(values) > 500:
        raise CollectorError("筛选命中超过 500 条；请缩小扫描范围后再下载媒体。")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in values):
        raise CollectorError("筛选结果包含无效 shortcode，已停止媒体下载。")
    return f"post_shortcode in {values!r}"


def _run_metadata(
    args: argparse.Namespace,
    target: str,
    output: Path,
    *,
    include: str | None = None,
) -> tuple[list[Any], str]:
    command = _common_gallery_args(args, output, include=include) + ["-J", target]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CollectorError(f"无法启动 gallery-dl：{exc}") from exc
    if args.verbose and completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode:
        detail = completed.stderr.strip() or f"gallery-dl 退出码 {completed.returncode}"
        raise CollectorError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout[:300].strip()
        raise CollectorError(f"无法解析上游输出：{detail or exc}") from exc
    if not isinstance(payload, list):
        raise CollectorError("上游返回了非预期的数据格式。")
    return payload, completed.stderr


def _is_profile_target(target: str) -> bool:
    parts = [part for part in urlparse(target).path.split("/") if part]
    return len(parts) == 1 and bool(USERNAME_PATTERN.fullmatch(parts[0]))


def _contains_upstream_error(events: Sequence[Any]) -> bool:
    return any(isinstance(event, list) and event and event[0] == -1 for event in events)


def _is_terminal_instagram_error(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "429",
            "too many requests",
            "login",
            "challenge",
            "checkpoint",
        )
    )


def _event_error_text(events: Sequence[Any]) -> str:
    values: list[str] = []
    for event in events:
        if not isinstance(event, list) or not event or event[0] != -1:
            continue
        detail = event[1] if len(event) > 1 and isinstance(event[1], dict) else {}
        values.append(f"{detail.get('error', '')}: {detail.get('message', '')}")
    return "; ".join(values)


def _directory_event_count(events: Sequence[Any]) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, list) and event and event[0] == 2
    )


def _request_delay_seconds(value: str) -> float:
    match = DELAY_PATTERN.fullmatch(value)
    if match is None:  # Parser/UI validation normally makes this unreachable.
        raise CollectorError("请求间隔格式无效。")
    low = float(match.group("low"))
    high = float(match.group("high") or low)
    return random.SystemRandom().uniform(low, high)


def _wait_between_sources(request_delay: str) -> None:
    time.sleep(_request_delay_seconds(request_delay))


def _run_random_metadata(
    args: argparse.Namespace,
    plan: DiscoveryPlan,
    output: Path,
) -> tuple[list[Any], str, list[dict[str, Any]]]:
    """Collect several curated hashtag feeds sequentially and stop on blocks."""
    combined: list[Any] = []
    logs: list[str] = []
    sources: list[dict[str, Any]] = []
    total = len(plan.tags)
    for index, (tag, target) in enumerate(zip(plan.tags, plan.targets), 1):
        if index > 1:
            _wait_between_sources(args.request_delay)
        print(f"随机来源 {index}/{total}：#{tag}", flush=True)
        try:
            payload, stderr = _run_metadata(args, target, output, include="posts")
        except CollectorError as exc:
            message = str(exc)
            combined.append(
                [
                    -1,
                    {
                        "error": "DiscoverySourceError",
                        "message": f"#{tag}: {message}",
                    },
                ]
            )
            sources.append(
                {
                    "tag": tag,
                    "target": target,
                    "status": "failed",
                    "candidate_events": 0,
                    "error": message,
                }
            )
            if _is_terminal_instagram_error(message):
                break
            continue

        combined.extend(payload)
        if stderr:
            logs.append(stderr)
        error_text = _event_error_text(payload)
        sources.append(
            {
                "tag": tag,
                "target": target,
                "status": "partial" if error_text else "ok",
                "candidate_events": _directory_event_count(payload),
                "error": error_text or None,
            }
        )
        if error_text and _is_terminal_instagram_error(error_text):
            break
    return combined, "\n".join(logs), sources


def _run_all_metadata(
    args: argparse.Namespace, target: str, output: Path
) -> tuple[list[Any], str, list[str]]:
    """Run profile content types separately so one failure stops the next."""
    includes = (
        args.include.split(",")
        if _is_profile_target(target) and "," in args.include
        else [args.include]
    )
    combined: list[Any] = []
    logs: list[str] = []
    attempted: list[str] = []
    for include in includes:
        attempted.append(include)
        try:
            payload, stderr = _run_metadata(args, target, output, include=include)
        except CollectorError as exc:
            if not combined:
                raise
            combined.append([-1, {"error": "CollectorError", "message": str(exc)}])
            break
        combined.extend(payload)
        if stderr:
            logs.append(stderr)
        if _contains_upstream_error(payload):
            break
    return combined, "\n".join(logs), attempted


def _run_download(
    args: argparse.Namespace,
    target: str,
    output: Path,
    *,
    include: str | None = None,
    post_shortcodes: Sequence[str] | None = None,
) -> tuple[int, str]:
    media_dir = output / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    command = _common_gallery_args(
        args, output, include=include, post_shortcodes=post_shortcodes
    ) + [
        "--windows-filenames",
        "-D",
        str(media_dir.resolve()),
        "-f",
        "{post_shortcode}_{media_id}.{extension}",
        "--download-archive",
        str((output / "media-archive.txt").resolve()),
        target,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=None,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CollectorError(f"无法启动 gallery-dl 媒体下载：{exc}") from exc
    if completed.stderr and (args.verbose or completed.returncode):
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed.returncode, completed.stderr


def _run_all_downloads(
    args: argparse.Namespace,
    target: str,
    output: Path,
    *,
    post_shortcodes: Sequence[str] | None = None,
) -> tuple[int, str, list[str]]:
    """Download profile content types separately and stop on first failure."""
    includes = (
        args.include.split(",")
        if _is_profile_target(target) and "," in args.include
        else [args.include]
    )
    logs: list[str] = []
    attempted: list[str] = []
    for include in includes:
        attempted.append(include)
        try:
            returncode, stderr = _run_download(
                args,
                target,
                output,
                include=include,
                post_shortcodes=post_shortcodes,
            )
        except CollectorError as exc:
            logs.append(str(exc))
            return -1, "\n".join(logs), attempted
        if stderr:
            logs.append(stderr)
        if returncode:
            return returncode, "\n".join(logs), attempted
    return 0, "\n".join(logs), attempted


def _exact_post_target(post: dict[str, Any]) -> str:
    shortcode = post.get("shortcode")
    if not isinstance(shortcode, str) or not SHORTCODE_PATTERN.fullmatch(shortcode):
        raise CollectorError("筛选结果包含无效 shortcode，已停止媒体下载。")
    post_url = post.get("post_url")
    if isinstance(post_url, str):
        try:
            normalized = normalize_target(post_url)
        except CollectorError:
            normalized = ""
        parts = [part for part in urlparse(normalized).path.split("/") if part]
        if (
            len(parts) == 2
            and parts[0] in {"p", "reel", "reels", "tv"}
            and parts[1] == shortcode
        ):
            return normalized
    return f"https://www.instagram.com/p/{shortcode}/"


def _run_exact_downloads(
    args: argparse.Namespace,
    posts: Sequence[dict[str, Any]],
    output: Path,
) -> tuple[int, str, list[str]]:
    """Download only the selected random snapshot, one exact post at a time."""
    if len(posts) > 500:
        raise CollectorError("筛选命中超过 500 条；请缩小范围后再下载媒体。")
    targets = [_exact_post_target(post) for post in posts]
    logs: list[str] = []
    attempted: list[str] = []
    for index, target in enumerate(targets):
        if index:
            _wait_between_sources(args.request_delay)
        attempted.append(target)
        try:
            returncode, stderr = _run_download(
                args,
                target,
                output,
                include="posts",
            )
        except CollectorError as exc:
            logs.append(str(exc))
            return -1, "\n".join(logs), attempted
        if stderr:
            logs.append(stderr)
        if returncode:
            return returncode, "\n".join(logs), attempted
    return 0, "\n".join(logs), attempted


def _atomic_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding=encoding, newline="\n")
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _try_write_json(path: Path, value: Any) -> None:
    """Best-effort failure audit; preserve the original exception if this fails."""
    try:
        _write_json(path, value)
    except OSError:
        pass


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    _atomic_text(path, content)


CSV_COLUMNS = (
    "标题",
    "正文",
    "账号",
    "发布时间",
    "帖子链接",
    "帖子ID",
    "点赞数",
    "评论数",
    "浏览数",
    "播放数",
    "Hashtag",
    "媒体类型",
    "媒体链接",
)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def _post_csv_row(post: dict[str, Any]) -> dict[str, str]:
    media_types: list[str] = []
    media_urls: list[str] = []
    for media in post.get("media") or []:
        if not isinstance(media, dict):
            continue
        media_type = _csv_value(media.get("media_type"))
        if media_type:
            media_types.append(media_type)
        media_url = (
            media.get("video_url")
            if media_type == "video"
            else media.get("display_url")
        ) or media.get("url")
        if media_url:
            media_urls.append(str(media_url))
    return {
        "标题": _csv_value(post.get("title")),
        "正文": _csv_value(post.get("body") or post.get("caption")),
        "账号": _csv_value(post.get("username")),
        "发布时间": _csv_value(post.get("published_at")),
        "帖子链接": _csv_value(post.get("post_url")),
        "帖子ID": _csv_value(post.get("post_id")),
        "点赞数": _csv_value(post.get("like_count")),
        "评论数": _csv_value(post.get("comment_count")),
        "浏览数": _csv_value(post.get("view_count")),
        "播放数": _csv_value(post.get("play_count")),
        "Hashtag": _csv_value(post.get("hashtags")),
        "媒体类型": " | ".join(media_types),
        "媒体链接": "\n".join(media_urls),
    }


def _write_csv(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """Write a Windows-friendly spreadsheet with explicit title/body columns."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_post_csv_row(record) for record in records)
    _atomic_text(path, buffer.getvalue(), encoding="utf-8-sig")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CollectorError(
                f"已有 {path.name} 第 {line_number} 行不是有效 JSON；请修复或使用 --replace。"
            ) from exc
        if not isinstance(value, dict):
            raise CollectorError(f"已有 {path.name} 第 {line_number} 行不是对象。")
        records.append(value)
    return records


def _friendly_errors(errors: Sequence[dict[str, str]]) -> str:
    details = "; ".join(f"{item['error']}: {item['message']}" for item in errors)
    lowered = details.lower()
    if "login" in lowered or "requested user could not be found" in lowered:
        return "Instagram 可能要求有效登录会话，或目标不存在。请先确认用户名；若浏览器可访问，建议加 --cookies-from-browser firefox，或使用 --cookies 文件。"
    if "429" in lowered or "too many requests" in lowered:
        return "Instagram 返回 429，已停止采集。请等待后再试，不要缩短请求间隔。"
    if "challenge" in lowered or "checkpoint" in lowered:
        return (
            "Instagram 要求账号验证，已停止采集。请在官方网页中人工处理，不要自动绕过。"
        )
    return details


def _authentication_mode(args: argparse.Namespace) -> str:
    if args.cookies_from_browser:
        return f"browser:{args.cookies_from_browser}"
    if args.cookies:
        return "cookie-file"
    return "anonymous"


def _stderr_tail(value: str, limit: int = 4000) -> str | None:
    value = value.strip()
    if not value:
        return None
    return value[-limit:]


def _failed_summary(
    args: argparse.Namespace,
    target: str | None,
    fetched_at: str,
    error: str,
    *,
    error_type: str = "CollectorError",
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    random_mode = target is None
    summary: dict[str, Any] = {
        "status": "failed",
        "mode": "random" if random_mode else "target",
        "target": target,
        "fetched_at": fetched_at,
        "authentication": _authentication_mode(args),
        "include": [] if random_mode else args.include.split(","),
        "request_delay": args.request_delay,
        "errors": [{"error": error_type, "message": error}],
    }
    if random_mode:
        summary["max_posts_per_source"] = args.max_posts
        if discovery is not None:
            summary["discovery"] = discovery
    else:
        summary["max_posts_per_type"] = args.max_posts
    return summary


def _filter_spec(args: argparse.Namespace) -> FilterSpec:
    try:
        return FilterSpec.from_values(
            since=args.since,
            until=args.until,
            min_likes=args.min_likes,
            max_likes=args.max_likes,
            keywords=args.keywords,
            keyword_mode=args.keyword_mode,
            hashtags=args.hashtags,
            hashtag_mode=args.hashtag_mode,
            media_type=args.media_type,
        )
    except FilterValidationError as exc:
        raise CollectorError(str(exc)) from exc


def _newest_first(posts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort known publication times first so a cross-source limit is predictable."""
    return sorted(
        posts,
        key=lambda post: (
            bool(post.get("published_at")),
            str(post.get("published_at") or ""),
            str(post.get("post_id") or post.get("shortcode") or ""),
        ),
        reverse=True,
    )


def _discovery_report(
    plan: DiscoveryPlan, sources: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    report = plan.to_dict()
    report.update(
        {
            "selection_order": "filter-then-shuffle-then-limit",
            "attempted_sources": len(sources),
            "successful_sources": sum(
                1 for source in sources if source.get("status") == "ok"
            ),
            "candidate_events": sum(
                int(source.get("candidate_events") or 0) for source in sources
            ),
            "source_results": list(sources),
        }
    )
    return report


def run(args: argparse.Namespace) -> int:
    if args.cookies and args.cookies_from_browser:
        raise CollectorError("--cookies 与 --cookies-from-browser 只能选择一个。")
    if args.cookies and not args.cookies.is_file():
        raise CollectorError(f"Cookie 文件不存在：{args.cookies}")
    if importlib.util.find_spec("gallery_dl") is None:
        raise CollectorError("未安装 gallery-dl；请先运行 python -m pip install -e .")

    random_mode = args.target is None or args.random
    if args.random and args.target is not None:
        raise CollectorError("随机模式不需要 target；请移除账号或 URL。")
    if not random_mode and args.random_seed is not None:
        raise CollectorError("--random-seed 只用于随机模式。")

    filter_spec = _filter_spec(args)
    plan = (
        create_discovery_plan(args.random_sources, args.random_seed)
        if random_mode
        else None
    )
    target = None if random_mode else normalize_target(args.target)
    output = (
        args.output
        or (_default_random_output() if random_mode else _default_output(target))
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)

    if random_mode:
        print("正在随机发现 Instagram 公开主题帖子……", flush=True)
    else:
        print(f"正在采集：{target}", flush=True)
    fetched_at = datetime.now(timezone.utc).isoformat()
    attempted_include: list[str] = []
    discovery_sources: list[dict[str, Any]] = []
    try:
        if random_mode:
            assert plan is not None
            events, _, discovery_sources = _run_random_metadata(args, plan, output)
        else:
            assert target is not None
            events, _, attempted_include = _run_all_metadata(args, target, output)
    except CollectorError as exc:
        discovery = plan.to_dict() if plan is not None else None
        _try_write_json(
            output / "run.json",
            _failed_summary(
                args,
                target,
                fetched_at,
                str(exc),
                discovery=discovery,
            ),
        )
        raise
    discovery = (
        _discovery_report(plan, discovery_sources) if plan is not None else None
    )
    extracted, errors = normalize_events(events, fetched_at=fetched_at)

    if args.keep_raw:
        _write_json(output / "raw-events.json", events)

    try:
        _write_jsonl(output / "extracted.jsonl", extracted)
    except OSError as exc:
        raise CollectorError(f"无法保存本次提取数据：{exc}") from exc

    if errors and not extracted:
        summary = {
            "status": "failed",
            "mode": "random" if random_mode else "target",
            "target": target,
            "fetched_at": fetched_at,
            "authentication": _authentication_mode(args),
            "include": [] if random_mode else args.include.split(","),
            "attempted_include": attempted_include,
            "request_delay": args.request_delay,
            "filter": filter_spec.to_dict(),
            "errors": errors,
        }
        if random_mode:
            summary["max_posts_per_source"] = args.max_posts
            summary["discovery"] = discovery
        else:
            summary["max_posts_per_type"] = args.max_posts
        _try_write_json(output / "run.json", summary)
        raise CollectorError(_friendly_errors(errors))
    if not extracted:
        message = (
            "随机来源没有返回帖子。Instagram 可能要求有效登录 Cookie，"
            "也可能暂时限制了访问。"
            if random_mode
            else "没有获得帖子。目标可能为空、不可访问，或 Cookie 已失效。"
        )
        _try_write_json(
            output / "run.json",
            _failed_summary(
                args,
                target,
                fetched_at,
                message,
                error_type="NoResults",
                discovery=discovery,
            ),
        )
        raise CollectorError(message)

    matched, filter_rejections = apply_filters(extracted, filter_spec)
    matched_before_limit = len(matched)
    result_limit_reached = bool(
        args.max_results is not None and len(matched) > args.max_results
    )
    if random_mode:
        assert plan is not None
        matched = shuffle_discovered_posts(matched, plan.seed)
    elif args.max_results:
        matched = _newest_first(matched)
    if args.max_results:
        current = matched[: args.max_results]
    else:
        current = matched

    posts_path = output / "posts.jsonl"
    try:
        _write_jsonl(output / "current.jsonl", current)
        _write_csv(output / "current.csv", current)
        previous = [] if args.replace else _read_jsonl(posts_path)
        stored, new_count = merge_posts(
            previous, current, preserve_missing=bool(errors)
        )
        _write_jsonl(posts_path, stored)
        _write_csv(output / "posts.csv", stored)
    except (CollectorError, OSError, ValueError) as exc:
        _try_write_json(
            output / "run.json",
            _failed_summary(
                args,
                target,
                fetched_at,
                str(exc),
                discovery=discovery,
            ),
        )
        if isinstance(exc, CollectorError):
            raise
        raise CollectorError(f"无法读取或保存帖子数据：{exc}") from exc

    download_returncode = None
    download_error = None
    download_skipped_reason = None
    attempted_download_include: list[str] = []
    attempted_download_posts: list[str] = []
    selected_shortcodes: list[str] | None = None
    if args.download_media and errors:
        download_skipped_reason = "元数据采集不完整"
    elif args.download_media and not current:
        download_skipped_reason = "没有帖子命中当前筛选条件"
    elif args.download_media:
        # Always bind the second-pass downloader to this metadata snapshot.  A
        # profile feed can change between phases even without a user filter.
        values = [post.get("shortcode") for post in current]
        if any(not isinstance(value, str) or not value for value in values):
            download_skipped_reason = "命中帖子缺少可用的 shortcode"
        elif len(values) > 500:
            download_skipped_reason = "命中超过 500 条；请缩小范围后再下载媒体"
        else:
            selected_shortcodes = values
    if args.download_media and download_skipped_reason is None:
        print("元数据已保存，开始下载媒体……", flush=True)
        if random_mode:
            try:
                download_returncode, download_stderr, attempted_download_posts = (
                    _run_exact_downloads(args, current, output)
                )
            except CollectorError as exc:
                download_returncode, download_stderr = -1, str(exc)
        else:
            assert target is not None
            download_returncode, download_stderr, attempted_download_include = (
                _run_all_downloads(
                    args,
                    target,
                    output,
                    post_shortcodes=selected_shortcodes,
                )
            )
        if download_returncode:
            download_error = _stderr_tail(download_stderr)

    expected_empty_download = download_skipped_reason == "没有帖子命中当前筛选条件"

    summary = {
        "status": (
            "partial"
            if errors
            or download_returncode
            or (download_skipped_reason and not expected_empty_download)
            else "ok"
        ),
        "mode": "random" if random_mode else "target",
        "target": target,
        "fetched_at": fetched_at,
        "authentication": _authentication_mode(args),
        "include": [] if random_mode else args.include.split(","),
        "attempted_include": attempted_include,
        "max_results": args.max_results,
        "request_delay": args.request_delay,
        "filter": filter_spec.to_dict(),
        "scanned_this_run": len(extracted),
        "fetched_this_run": len(extracted),
        "matched_before_limit": matched_before_limit,
        "matched_this_run": len(current),
        "filtered_out": len(extracted) - matched_before_limit,
        "filter_rejections": filter_rejections,
        "result_limit_reached": result_limit_reached,
        "new_posts": new_count,
        "stored_posts": len(stored),
        "media_download_requested": args.download_media,
        "attempted_download_include": attempted_download_include,
        "attempted_download_posts": attempted_download_posts,
        "media_download_returncode": download_returncode,
        "media_download_error": download_error,
        "media_download_skipped_reason": download_skipped_reason,
        "errors": errors,
    }
    if random_mode:
        summary["max_posts_per_source"] = args.max_posts
        summary["discovery"] = discovery
    else:
        summary["max_posts_per_type"] = args.max_posts
    _write_json(output / "run.json", summary)

    print(
        f"完成：扫描 {len(extracted)} 条，命中 {len(current)} 条，"
        f"新增 {new_count} 条，共保存 {len(stored)} 条。"
    )
    print(f"数据：{posts_path}")
    if args.download_media and not download_skipped_reason:
        print(f"媒体：{output / 'media'}")
    if errors:
        print(f"警告：{_friendly_errors(errors)}", file=sys.stderr)
        return 4
    if (
        download_returncode
        or download_error
        or (download_skipped_reason and not expected_empty_download)
    ):
        print("媒体下载未完成，但帖子元数据已保留。", file=sys.stderr)
        return 5
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CollectorError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"文件或进程错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130

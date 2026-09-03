"""Zero-dependency, loopback-only web UI for :mod:`ins_posts`.

The UI deliberately launches the existing CLI in a child process instead of
running collector code in request threads.  This keeps stdout/stderr isolated,
allows a small live log, and makes the web surface a narrow adapter over the
audited command-line interface.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from ..collector.cli import (
    SUPPORTED_BROWSERS,
    CollectorError,
    normalize_target,
    validate_delay,
)
from ..core.filters import FilterSpec, FilterValidationError

LOOPBACK_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOG_LINES = 500
MAX_RESULT_LINES = 2_500
MAX_RESULT_LINE_BYTES = 2 * 1024 * 1024
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed"})
ALLOWED_INCLUDES = frozenset({"posts", "reels"})
ALLOWED_MEDIA_TYPES = frozenset({"all", "image", "video"})
DISCOVERY_BREADTHS = {
    "quick": {"random_sources": 2, "max_posts": 15},
    "standard": {"random_sources": 4, "max_posts": 20},
    "wide": {"random_sources": 8, "max_posts": 25},
}
DEFAULT_DISCOVERY_BREADTH = "standard"
DEFAULT_DISCOVERY_RESULTS = 20
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class UIRequestError(ValueError):
    """A safe validation error returned to the browser."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collector_environment() -> dict[str, str]:
    """Return an environment whose redirected Python output is always UTF-8."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _bounded_text(value: Any, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise UIRequestError(f"{field_name} 必须是文本。")
    value = value.strip()
    if len(value) > maximum:
        raise UIRequestError(f"{field_name} 过长。")
    return value


def _optional_nonnegative(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise UIRequestError(f"{field_name} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise UIRequestError(f"{field_name} 必须是整数。") from exc
    if number < 0 or number > 1_000_000_000_000:
        raise UIRequestError(f"{field_name} 超出允许范围。")
    return number


def _required_int(value: Any, field_name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise UIRequestError(f"{field_name} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise UIRequestError(f"{field_name} 必须是整数。") from exc
    if not low <= number <= high:
        raise UIRequestError(f"{field_name} 必须在 {low} 到 {high} 之间。")
    return number


def _string_list(value: Any, field_name: str, *, maximum_items: int = 20) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values: Iterable[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        values = value
    else:
        raise UIRequestError(f"{field_name} 必须是文本列表。")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise UIRequestError(f"{field_name} 必须是文本列表。")
        item = raw.strip()
        if not item:
            continue
        if len(item) > 100:
            raise UIRequestError(f"{field_name} 中有过长的值。")
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    if len(result) > maximum_items:
        raise UIRequestError(f"{field_name} 最多填写 {maximum_items} 项。")
    return result


def _validate_filters(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UIRequestError("筛选条件格式不正确。")
    allowed = {
        "since",
        "until",
        "min_likes",
        "max_likes",
        "keywords",
        "keyword_mode",
        "hashtags",
        "hashtag_mode",
        "media_type",
        "max_results",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise UIRequestError("包含不支持的筛选字段。")

    raw_since = raw.get("since")
    raw_until = raw.get("until")
    since = (
        "" if raw_since is None else _bounded_text(raw_since, "开始日期", maximum=10)
    )
    until = (
        "" if raw_until is None else _bounded_text(raw_until, "结束日期", maximum=10)
    )
    minimum = _optional_nonnegative(raw.get("min_likes"), "最低点赞数")
    maximum = _optional_nonnegative(raw.get("max_likes"), "最高点赞数")
    keywords = _string_list(raw.get("keywords"), "关键词")
    hashtags = _string_list(raw.get("hashtags"), "Hashtag")
    keyword_mode = raw.get("keyword_mode", "any")
    hashtag_mode = raw.get("hashtag_mode", "any")
    media_type = raw.get("media_type", "all")
    max_results = _optional_nonnegative(raw.get("max_results"), "结果上限")
    if max_results == 0:
        raise UIRequestError("结果上限必须大于 0。")
    if max_results is not None and max_results > 1000:
        raise UIRequestError("结果上限不能超过 1000。")
    if not isinstance(keyword_mode, str) or keyword_mode not in {"any", "all"}:
        raise UIRequestError("关键词模式不正确。")
    if not isinstance(hashtag_mode, str) or hashtag_mode not in {"any", "all"}:
        raise UIRequestError("Hashtag 模式不正确。")
    if not isinstance(media_type, str) or media_type not in ALLOWED_MEDIA_TYPES:
        raise UIRequestError("媒体类型不正确。")

    try:
        FilterSpec.from_values(
            since=since or None,
            until=until or None,
            min_likes=minimum,
            max_likes=maximum,
            keywords=",".join(keywords),
            keyword_mode=keyword_mode,
            hashtags=",".join(hashtags),
            hashtag_mode=hashtag_mode,
            media_type=media_type,
        )
    except FilterValidationError as exc:
        raise UIRequestError(str(exc)) from exc

    return {
        "since": since or None,
        "until": until or None,
        "min_likes": minimum,
        "max_likes": maximum,
        "keywords": keywords,
        "keyword_mode": keyword_mode,
        "hashtags": hashtags,
        "hashtag_mode": hashtag_mode,
        "media_type": media_type,
        "max_results": max_results,
    }


def validate_job_request(payload: Any) -> dict[str, Any]:
    """Validate browser JSON and return a command-safe, normalized config."""
    if not isinstance(payload, dict):
        raise UIRequestError("请求内容必须是 JSON 对象。")
    allowed = {
        "mode",
        "target",
        "include",
        "max_posts",
        "discovery",
        "authentication",
        "download_media",
        "request_delay",
        "filters",
    }
    if set(payload) - allowed:
        raise UIRequestError("请求包含不支持的字段。")

    mode = payload.get("mode", "random")
    if not isinstance(mode, str) or mode not in {"random", "target"}:
        raise UIRequestError("采集模式不正确。")

    target: str | None = None
    include: list[str] = []
    max_posts: int
    random_sources: int | None = None
    discovery: dict[str, Any] | None = None
    if mode == "random":
        forbidden = {"target", "include", "max_posts"}.intersection(payload)
        if forbidden:
            raise UIRequestError("随机发现模式不接受账号、URL 或自定义扫描范围。")
        raw_discovery = payload.get("discovery", {})
        if not isinstance(raw_discovery, dict):
            raise UIRequestError("随机发现设置格式不正确。")
        if set(raw_discovery) - {"breadth", "result_count"}:
            raise UIRequestError("随机发现只支持范围和结果数量设置。")
        breadth = raw_discovery.get("breadth", DEFAULT_DISCOVERY_BREADTH)
        if not isinstance(breadth, str) or breadth not in DISCOVERY_BREADTHS:
            raise UIRequestError("随机发现范围不正确。")
        result_count = _required_int(
            raw_discovery.get("result_count", DEFAULT_DISCOVERY_RESULTS),
            "希望得到的结果数量",
            1,
            50,
        )
        profile = DISCOVERY_BREADTHS[breadth]
        random_sources = profile["random_sources"]
        max_posts = profile["max_posts"]
        discovery = {"breadth": breadth, "result_count": result_count}
    else:
        if "discovery" in payload:
            raise UIRequestError("指定目标模式不接受随机发现设置。")
        missing = {"target", "include", "max_posts"} - set(payload)
        if missing:
            raise UIRequestError("指定目标模式需要目标、内容类型和扫描上限。")
        raw_target = _bounded_text(payload["target"], "目标", maximum=500)
        if not raw_target:
            raise UIRequestError("请填写 Instagram 用户名或帖子链接。")
        try:
            target = normalize_target(raw_target)
        except CollectorError as exc:
            raise UIRequestError(str(exc)) from exc

        raw_include = payload["include"]
        if not isinstance(raw_include, list):
            raise UIRequestError("内容类型格式不正确。")
        for item in raw_include:
            if not isinstance(item, str) or item not in ALLOWED_INCLUDES:
                raise UIRequestError("内容类型只支持 posts 和 reels。")
            if item not in include:
                include.append(item)
        if not include:
            raise UIRequestError("至少选择一种内容类型。")
        max_posts = _required_int(payload["max_posts"], "扫描上限", 1, 1000)

    authentication = payload.get("authentication", "anonymous")
    allowed_authentication = {"anonymous", *SUPPORTED_BROWSERS}
    if (
        not isinstance(authentication, str)
        or authentication not in allowed_authentication
    ):
        raise UIRequestError("认证方式不正确。")
    download_media = payload.get("download_media", False)
    if not isinstance(download_media, bool):
        raise UIRequestError("下载媒体选项格式不正确。")
    request_delay = _bounded_text(
        payload.get("request_delay", "6-12"), "请求间隔", maximum=20
    )
    try:
        request_delay = validate_delay(request_delay)
    except argparse.ArgumentTypeError as exc:
        raise UIRequestError(str(exc)) from exc

    raw_filters = payload.get("filters")
    if (
        mode == "random"
        and isinstance(raw_filters, dict)
        and "max_results" in raw_filters
    ):
        raise UIRequestError("随机发现的结果数量请在随机设置中填写。")
    filters = _validate_filters(raw_filters)
    if mode == "random":
        filters["max_results"] = discovery["result_count"] if discovery else None

    return {
        "mode": mode,
        "target": target,
        "include": include,
        "max_posts": max_posts,
        "random_sources": random_sources,
        "discovery": discovery,
        "authentication": authentication,
        "download_media": download_media,
        "request_delay": request_delay,
        "filters": filters,
    }


@dataclass(slots=True)
class Job:
    id: str
    config: dict[str, Any]
    output_dir: Path
    status: str = "queued"
    phase: str = "waiting"
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    error: str | None = None
    log_sequence: int = 0
    logs: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_LOG_LINES)
    )
    process: subprocess.Popen[str] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def add_log(self, message: str, *, level: str = "info") -> None:
        message = message.strip()
        if not message:
            return
        with self.lock:
            self.log_sequence += 1
            self.logs.append(
                {
                    "sequence": self.log_sequence,
                    "timestamp": _utc_now(),
                    "level": level,
                    "message": message[-2000:],
                }
            )

    def snapshot(self, after_sequence: int = 0) -> dict[str, Any]:
        with self.lock:
            available = list(self.logs)
            earliest = available[0]["sequence"] if available else self.log_sequence + 1
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "returncode": self.returncode,
                "error": self.error,
                "config": self.config,
                "logs": [
                    item for item in available if item["sequence"] > after_sequence
                ],
                "logs_truncated": bool(
                    available and after_sequence and after_sequence < earliest - 1
                ),
            }


PopenFactory = Callable[..., subprocess.Popen[str]]
DirectoryOpener = Callable[[Path], None]


def _default_directory_opener(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    command = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(
        command,
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class UIApp:
    """Thread-safe application state shared by request handlers."""

    def __init__(
        self,
        output_root: Path,
        token: str,
        *,
        popen_factory: PopenFactory = subprocess.Popen,
        directory_opener: DirectoryOpener = _default_directory_opener,
    ) -> None:
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.popen_factory = popen_factory
        self.directory_opener = directory_opener
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ins-ui")

    def close(self) -> None:
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            self._stop_job_process(job)
        self.executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _stop_job_process(job: Job) -> None:
        """Best-effort bounded shutdown of the exact collector child process."""
        with job.lock:
            process = job.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except OSError:
            pass

    def _has_active_job(self) -> bool:
        return any(job.status not in TERMINAL_STATUSES for job in self.jobs.values())

    def create_job(self, payload: Any) -> Job:
        config = validate_job_request(payload)
        with self.lock:
            if self._has_active_job():
                raise UIRequestError("已有任务正在运行，请等待它完成后再开始新任务。")
            job_id = uuid4().hex
            output_dir = (self.output_root / job_id).resolve()
            if output_dir.parent != self.output_root:
                raise UIRequestError("任务输出目录无效。")
            output_dir.mkdir(parents=False, exist_ok=False)
            job = Job(id=job_id, config=config, output_dir=output_dir)
            self.jobs[job_id] = job
            self.executor.submit(self._run_job, job)
            return job

    def get_job(self, job_id: str) -> Job | None:
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            return None
        with self.lock:
            return self.jobs.get(job_id)

    def _build_command(self, job: Job) -> list[str]:
        config = job.config
        filters = config["filters"]
        command = [
            sys.executable,
            "-m",
            "ins_posts",
        ]
        if config["mode"] == "random":
            command.extend(
                ["--random", "--random-sources", str(config["random_sources"])]
            )
        else:
            command.append(config["target"])
        command.extend(
            [
                "--output",
                str(job.output_dir),
                "--max-posts",
                str(config["max_posts"]),
            ]
        )
        if config["mode"] == "target":
            command.extend(["--include", ",".join(config["include"])])
        command.extend(["--request-delay", config["request_delay"]])
        if config["authentication"] != "anonymous":
            command.extend(["--cookies-from-browser", config["authentication"]])
        if config["download_media"]:
            command.append("--download-media")
        option_names = {
            "since": "--since",
            "until": "--until",
            "min_likes": "--min-likes",
            "max_likes": "--max-likes",
            "max_results": "--max-results",
        }
        for name, option in option_names.items():
            value = filters[name]
            if value is not None:
                command.extend([option, str(value)])
        if filters["keywords"]:
            command.extend(["--keywords", ",".join(filters["keywords"])])
        if filters["keyword_mode"] != "any":
            command.extend(["--keyword-mode", filters["keyword_mode"]])
        if filters["hashtags"]:
            command.extend(["--hashtags", ",".join(filters["hashtags"])])
        if filters["hashtag_mode"] != "any":
            command.extend(["--hashtag-mode", filters["hashtag_mode"]])
        if filters["media_type"] != "all":
            command.extend(["--media-type", filters["media_type"]])
        return command

    def _run_job(self, job: Job) -> None:
        with job.lock:
            job.status = "running"
            job.phase = "collecting"
            job.started_at = _utc_now()
        job.add_log("任务已启动。")
        command = self._build_command(job)
        try:
            process = self.popen_factory(
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_collector_environment(),
            )
            with job.lock:
                job.process = process
            stream: TextIO | None = process.stdout
            if stream is not None:
                for line in stream:
                    clean = line.rstrip("\r\n")
                    if "开始下载媒体" in clean:
                        with job.lock:
                            job.phase = "media"
                    job.add_log(clean)
            returncode = process.wait()
            with job.lock:
                job.returncode = returncode
                job.process = None
                job.finished_at = _utc_now()
                job.phase = "finished"
                if returncode == 0:
                    job.status = "succeeded"
                elif returncode in {4, 5}:
                    job.status = "partial"
                else:
                    job.status = "failed"
                    job.error = (
                        f"采集进程退出码：{returncode}；"
                        "运行日志已自动展开，请查看末尾错误。"
                    )
            if returncode == 0:
                job.add_log("任务完成。")
            elif returncode in {4, 5}:
                job.add_log("任务完成，但存在部分错误。", level="warning")
            else:
                job.add_log(f"任务失败（退出码 {returncode}）。", level="error")
        except OSError as exc:
            self._stop_job_process(job)
            with job.lock:
                job.status = "failed"
                job.phase = "finished"
                job.finished_at = _utc_now()
                job.process = None
                job.error = f"采集进程错误：{exc}"
            job.add_log(job.error, level="error")
        except Exception as exc:  # noqa: BLE001 - background worker safety boundary
            self._stop_job_process(job)
            with job.lock:
                job.status = "failed"
                job.phase = "finished"
                job.finished_at = _utc_now()
                job.process = None
                job.error = f"后台任务异常：{type(exc).__name__}"
            job.add_log(job.error, level="error")

    def _safe_job_path(self, job: Job, filename: str) -> Path:
        path = (job.output_dir / filename).resolve()
        if path.parent != job.output_dir.resolve():
            raise UIRequestError("结果文件路径无效。")
        return path

    def read_summary(self, job: Job) -> dict[str, Any] | None:
        path = self._safe_job_path(job, "run.json")
        if not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def read_results(
        self, job: Job, *, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        path = self._safe_job_path(job, "current.jsonl")
        records: list[dict[str, Any]] = []
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if line_number > MAX_RESULT_LINES:
                            break
                        if len(line.encode("utf-8")) > MAX_RESULT_LINE_BYTES:
                            continue
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if isinstance(value, dict):
                            records.append(value)
            except (OSError, UnicodeError, json.JSONDecodeError):
                records = []
        raw_total = len(records)
        total = len(records)
        offset = min(max(offset, 0), total)
        limit = min(max(limit, 1), 100)
        return {
            "items": records[offset : offset + limit],
            "total": total,
            "raw_total": raw_total,
            "offset": offset,
            "limit": limit,
        }

    def open_output(self, job: Job) -> None:
        output = job.output_dir.resolve()
        if output.parent != self.output_root or not output.is_dir():
            raise UIRequestError("任务输出目录不存在。")
        self.directory_opener(output)


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], app: UIApp) -> None:
        self.app = app
        super().__init__(address, UIRequestHandler)

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.server_address[1]}"

    @property
    def expected_host(self) -> str:
        return f"{LOOPBACK_HOST}:{self.server_address[1]}"


class UIRequestHandler(BaseHTTPRequestHandler):
    server: UIServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send_bytes(
        self, status: int, body: bytes, content_type: str, *, include_body: bool = True
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _send_json(self, status: int, value: Any, *, include_body: bool = True) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_bytes(
            status, body, "application/json; charset=utf-8", include_body=include_body
        )

    def _reject(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _valid_network_boundary(self, *, require_origin: bool) -> bool:
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self._reject(HTTPStatus.FORBIDDEN, "只允许本机访问。")
                return False
        except ValueError:
            self._reject(HTTPStatus.FORBIDDEN, "只允许本机访问。")
            return False
        if self.headers.get("Host") != self.server.expected_host:
            self._reject(HTTPStatus.MISDIRECTED_REQUEST, "Host 不匹配。")
            return False
        origin = self.headers.get("Origin")
        if require_origin and origin != self.server.origin:
            self._reject(HTTPStatus.FORBIDDEN, "Origin 不匹配。")
            return False
        if origin is not None and origin != self.server.origin:
            self._reject(HTTPStatus.FORBIDDEN, "Origin 不匹配。")
            return False
        return True

    def _authorized(self) -> bool:
        provided = self.headers.get("X-UI-Token", "")
        if not hmac.compare_digest(provided, self.server.app.token):
            self._reject(HTTPStatus.FORBIDDEN, "启动令牌无效。")
            return False
        return True

    def _read_json_body(self) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise UIRequestError("不支持分块请求。")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise UIRequestError("Content-Type 必须是 application/json。")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise UIRequestError("请求缺少 Content-Length。")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise UIRequestError("Content-Length 无效。") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise UIRequestError("请求内容过大。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UIRequestError("请求 JSON 无效。") from exc

    def _serve_static(self, path: str, *, include_body: bool = True) -> bool:
        spec = STATIC_FILES.get(path)
        if spec is None:
            return False
        filename, content_type = spec
        asset = Path(__file__).resolve().parent / "static" / filename
        try:
            body = asset.read_bytes()
        except OSError:
            self._reject(HTTPStatus.INTERNAL_SERVER_ERROR, "界面资源不可用。")
            return True
        self._send_bytes(HTTPStatus.OK, body, content_type, include_body=include_body)
        return True

    def do_HEAD(self) -> None:
        if not self._valid_network_boundary(require_origin=False):
            return
        path = urlsplit(self.path).path
        if not self._serve_static(path, include_body=False):
            self._reject(HTTPStatus.NOT_FOUND, "未找到。")

    def do_GET(self) -> None:
        if not self._valid_network_boundary(require_origin=False):
            return
        parsed = urlsplit(self.path)
        if self._serve_static(parsed.path):
            return
        if not parsed.path.startswith("/api/"):
            self._reject(HTTPStatus.NOT_FOUND, "未找到。")
            return
        if not self._authorized():
            return
        if parsed.path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "browsers": list(SUPPORTED_BROWSERS),
                    "single_task": True,
                },
            )
            return
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = self.server.app.get_job(parts[2])
            if job is None:
                self._reject(HTTPStatus.NOT_FOUND, "任务不存在。")
                return
            query = parse_qs(parsed.query, keep_blank_values=False)
            try:
                after = _required_int(query.get("after", ["0"])[0], "日志序号", 0, 10**12)
                offset = _required_int(query.get("offset", ["0"])[0], "分页位置", 0, 10**9)
                limit = _required_int(query.get("limit", ["50"])[0], "分页条数", 1, 100)
            except UIRequestError as exc:
                self._reject(HTTPStatus.BAD_REQUEST, str(exc))
                return
            response = job.snapshot(after)
            response["summary"] = self.server.app.read_summary(job)
            response["results"] = self.server.app.read_results(
                job, offset=offset, limit=limit
            )
            self._send_json(HTTPStatus.OK, response)
            return
        self._reject(HTTPStatus.NOT_FOUND, "未找到。")

    def do_POST(self) -> None:
        if not self._valid_network_boundary(require_origin=True):
            return
        if not self._authorized():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/jobs":
            try:
                payload = self._read_json_body()
                job = self.server.app.create_job(payload)
            except UIRequestError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if "已有任务正在运行" in str(exc)
                    else HTTPStatus.BAD_REQUEST
                )
                self._reject(status, str(exc))
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {"id": job.id, "status": job.status, "phase": job.phase},
            )
            return
        parts = [part for part in parsed.path.split("/") if part]
        if (
            len(parts) == 4
            and parts[:2] == ["api", "jobs"]
            and parts[3] == "open-output"
        ):
            job = self.server.app.get_job(parts[2])
            if job is None:
                self._reject(HTTPStatus.NOT_FOUND, "任务不存在。")
                return
            try:
                self.server.app.open_output(job)
            except (UIRequestError, OSError) as exc:
                self._reject(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(HTTPStatus.OK, {"status": "opened"})
            return
        self._reject(HTTPStatus.NOT_FOUND, "未找到。")

    def do_OPTIONS(self) -> None:
        if not self._valid_network_boundary(require_origin=True):
            return
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "不支持 OPTIONS。")


def create_server(
    output_root: Path,
    *,
    token: str | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    directory_opener: DirectoryOpener = _default_directory_opener,
) -> UIServer:
    app = UIApp(
        output_root,
        token or secrets.token_urlsafe(32),
        popen_factory=popen_factory,
        directory_opener=directory_opener,
    )
    return UIServer((LOOPBACK_HOST, 0), app)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 Instagram 采集本地网页界面。")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="任务结果保存目录；默认使用当前目录下的 data/ui-jobs。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_root = args.output_root or Path.cwd() / "data" / "ui-jobs"
    server = create_server(output_root)
    token = quote(server.app.token, safe="")
    url = f"{server.origin}/#token={token}"
    print(f"本地界面已启动：{url}")
    print("仅限本机访问；按 Ctrl+C 停止。")
    if not args.no_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("界面已停止。")
    finally:
        server.server_close()
        server.app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Structured, safe post filters shared by the CLI and local web UI."""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

MatchMode = Literal["any", "all"]
MediaType = Literal["all", "image", "video"]


class FilterValidationError(ValueError):
    """Raised when filter bounds or modes are invalid."""


def _csv_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(value.split())


def _parse_date(value: str | date | None, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FilterValidationError(f"{field} 必须是 YYYY-MM-DD 日期。") from exc


def _nonnegative(value: int | str | None, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise FilterValidationError(f"{field} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FilterValidationError(f"{field} 必须是整数。") from exc
    if number < 0:
        raise FilterValidationError(f"{field} 不能小于 0。")
    return number


def _normalized_hashtag(value: str) -> str:
    value = _normalized_text(value)
    value = value.removeprefix("#")
    return f"#{value}"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    since: date | None = None
    until: date | None = None
    min_likes: int | None = None
    max_likes: int | None = None
    keywords: tuple[str, ...] = ()
    keyword_mode: MatchMode = "any"
    hashtags: tuple[str, ...] = ()
    hashtag_mode: MatchMode = "any"
    media_type: MediaType = "all"

    @classmethod
    def from_values(
        cls,
        *,
        since: str | date | None = None,
        until: str | date | None = None,
        min_likes: int | str | None = None,
        max_likes: int | str | None = None,
        keywords: str | None = None,
        keyword_mode: str = "any",
        hashtags: str | None = None,
        hashtag_mode: str = "any",
        media_type: str = "all",
    ) -> FilterSpec:
        since_date = _parse_date(since, "开始日期")
        until_date = _parse_date(until, "结束日期")
        minimum = _nonnegative(min_likes, "最低点赞数")
        maximum = _nonnegative(max_likes, "最高点赞数")
        if since_date and until_date and since_date > until_date:
            raise FilterValidationError("开始日期不能晚于结束日期。")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise FilterValidationError("最低点赞数不能大于最高点赞数。")
        if keyword_mode not in {"any", "all"}:
            raise FilterValidationError("关键词模式只能是 any 或 all。")
        if hashtag_mode not in {"any", "all"}:
            raise FilterValidationError("Hashtag 模式只能是 any 或 all。")
        if media_type not in {"all", "image", "video"}:
            raise FilterValidationError("媒体类型只能是 all、image 或 video。")

        keyword_values = tuple(_normalized_text(item) for item in _csv_values(keywords))
        hashtag_values = tuple(
            _normalized_hashtag(item) for item in _csv_values(hashtags)
        )
        return cls(
            since=since_date,
            until=until_date,
            min_likes=minimum,
            max_likes=maximum,
            keywords=keyword_values,
            keyword_mode=keyword_mode,
            hashtags=hashtag_values,
            hashtag_mode=hashtag_mode,
            media_type=media_type,
        )

    @property
    def active(self) -> bool:
        return any(
            (
                self.since,
                self.until,
                self.min_likes is not None,
                self.max_likes is not None,
                self.keywords,
                self.hashtags,
                self.media_type != "all",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["since"] = self.since.isoformat() if self.since else None
        value["until"] = self.until.isoformat() if self.until else None
        value["keywords"] = list(self.keywords)
        value["hashtags"] = list(self.hashtags)
        value["active"] = self.active
        return value


def _published_date(post: dict[str, Any]) -> date | None:
    value = post.get("published_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _mismatch_reason(post: dict[str, Any], spec: FilterSpec) -> str | None:
    if spec.since or spec.until:
        published = _published_date(post)
        if published is None:
            return "date_missing"
        if spec.since and published < spec.since:
            return "before_since"
        if spec.until and published > spec.until:
            return "after_until"

    if spec.min_likes is not None or spec.max_likes is not None:
        likes = post.get("like_count")
        if likes is None:
            return "likes_missing"
        try:
            likes = int(likes)
        except (TypeError, ValueError):
            return "likes_missing"
        # gallery-dl's Instagram REST extractor currently maps both a hidden /
        # missing count and a genuine zero count to 0. Treating zero as unknown
        # is conservative: it avoids incorrectly passing hidden counts through
        # a maximum-likes filter, at the cost of excluding genuine zero-like posts.
        if likes == 0:
            return "likes_unknown_or_zero"
        if spec.min_likes is not None and likes < spec.min_likes:
            return "likes_below_min"
        if spec.max_likes is not None and likes > spec.max_likes:
            return "likes_above_max"

    if spec.keywords:
        content = _normalized_text(
            "\n".join(
                str(post.get(name) or "") for name in ("title", "body", "caption")
            )
        )
        checks = (keyword in content for keyword in spec.keywords)
        if not (all(checks) if spec.keyword_mode == "all" else any(checks)):
            return "keywords"

    if spec.hashtags:
        actual = {
            _normalized_hashtag(str(tag)) for tag in (post.get("hashtags") or ())
        }
        checks = (tag in actual for tag in spec.hashtags)
        if not (all(checks) if spec.hashtag_mode == "all" else any(checks)):
            return "hashtags"

    if spec.media_type != "all":
        actual_types = {
            str(item.get("media_type") or "") for item in (post.get("media") or ())
        }
        if spec.media_type not in actual_types:
            return "media_type"
    return None


def apply_filters(
    posts: list[dict[str, Any]], spec: FilterSpec
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter normalized posts and return first-failure reason counts."""
    if not spec.active:
        return list(posts), {}
    matched: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    for post in posts:
        reason = _mismatch_reason(post, spec)
        if reason is None:
            matched.append(post)
        else:
            rejected[reason] += 1
    return matched, dict(sorted(rejected.items()))

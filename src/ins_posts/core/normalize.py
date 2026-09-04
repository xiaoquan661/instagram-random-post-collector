"""Normalize gallery-dl's message stream into stable post records."""

from __future__ import annotations

import re
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

DIRECTORY_MESSAGE = 2
URL_MESSAGE = 3
ERROR_MESSAGE = -1
SCHEMA_VERSION = 2
TITLE_MAX_LENGTH = 120


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_value(metadata: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = metadata.get(name)
        if value is not None:
            return value
    return None


def _content_title(metadata: dict[str, Any], body: str) -> str:
    explicit = _text(_first_value(metadata, "title", "headline"))
    source = explicit or next(
        (line.strip() for line in body.splitlines() if line.strip()), ""
    )
    source = re.sub(r"\s+", " ", source).strip()
    if not explicit:
        source = re.split(r"(?<=[.!?。！？])", source, maxsplit=1)[0]
    if len(source) <= TITLE_MAX_LENGTH:
        return source
    return source[: TITLE_MAX_LENGTH - 1].rstrip() + "…"


def _utc_string(value: Any) -> str | None:
    """Turn gallery-dl's naive UTC datetime string into ISO-8601."""
    value = _text(value)
    if not value or value == "[Invalid DateTime]":
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        return value.replace(" ", "T") + "Z"
    return value


def _post_key(metadata: dict[str, Any]) -> str | None:
    for name in ("post_id", "sidecar_media_id"):
        if value := _text(metadata.get(name)):
            return f"id:{value}"
    for name in ("post_shortcode", "sidecar_shortcode", "shortcode"):
        if value := _text(metadata.get(name)):
            return f"shortcode:{value}"
    if value := _text(metadata.get("post_url")):
        return f"url:{value}"
    return None


def _hashtags(metadata: dict[str, Any], caption: str) -> list[str]:
    tags = metadata.get("tags")
    if isinstance(tags, (list, tuple, set)):
        values = [str(tag) for tag in tags if str(tag).strip()]
    else:
        values = re.findall(r"#\w+", caption, flags=re.UNICODE)
    return sorted(set(values), key=str.casefold)


def _source_tags(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for name in ("tag", "source_tag", "source_tags"):
        raw = metadata.get(name)
        if isinstance(raw, (list, tuple, set)):
            values.extend(str(item) for item in raw)
        elif raw is not None:
            values.append(str(raw))
    normalized = {
        value.strip().removeprefix("#")
        for value in values
        if value.strip().removeprefix("#")
    }
    return sorted(normalized, key=str.casefold)


def _merge_string_lists(previous: Iterable[Any], current: Iterable[Any]) -> list[str]:
    values = {
        str(value).strip()
        for value in (*previous, *current)
        if str(value).strip()
    }
    return sorted(values, key=str.casefold)


def _new_post(metadata: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    body = _text(_first_value(metadata, "description", "caption", "text")) or ""
    location = {
        "id": _text(metadata.get("location_id")),
        "slug": _text(metadata.get("location_slug")),
        "url": _text(metadata.get("location_url")),
    }
    if not any(location.values()):
        location = None

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "instagram",
        "post_id": _text(metadata.get("post_id") or metadata.get("sidecar_media_id")),
        "shortcode": _text(
            metadata.get("post_shortcode")
            or metadata.get("sidecar_shortcode")
            or metadata.get("shortcode")
        ),
        "post_url": _text(metadata.get("post_url")),
        "type": _text(
            metadata.get("type")
            or metadata.get("subcategory")
            or metadata.get("typename")
        ),
        "username": _text(metadata.get("username")),
        "owner_id": _text(metadata.get("owner_id")),
        "full_name": _text(metadata.get("fullname")),
        "published_at": _utc_string(metadata.get("post_date") or metadata.get("date")),
        "title": _content_title(metadata, body),
        "body": body,
        "caption": body,
        "accessibility_text": _text(
            _first_value(
                metadata,
                "accessibility_caption",
                "accessibility_text",
                "alt_text",
            )
        ),
        "hashtags": _hashtags(metadata, body),
        "source_tags": _source_tags(metadata),
        "like_count": _integer(metadata.get("likes")),
        "comment_count": _integer(
            _first_value(metadata, "comments", "comments_count", "comment_count")
        ),
        "view_count": _integer(
            _first_value(metadata, "video_view_count", "views", "view_count")
        ),
        "play_count": _integer(_first_value(metadata, "play_count", "plays")),
        "pinned": metadata.get("pinned"),
        "coauthors": metadata.get("coauthors") or [],
        "location": location,
        "expected_media_count": _integer(metadata.get("count")),
        "media": [],
        "fetched_at": fetched_at,
    }


def _merge_post_fields(post: dict[str, Any], metadata: dict[str, Any]) -> None:
    candidate = _new_post(metadata, post["fetched_at"])
    for name, value in candidate.items():
        if name in {"media", "fetched_at", "schema_version", "platform"}:
            continue
        if name == "source_tags":
            post[name] = _merge_string_lists(post.get(name) or [], value)
            continue
        if post.get(name) in (None, "", [], {}) and value not in (None, "", [], {}):
            post[name] = value


def _media_key(metadata: dict[str, Any], event_url: str) -> str:
    if value := _text(metadata.get("media_id")):
        return f"id:{value}"
    if value := _text(metadata.get("shortcode")):
        return f"shortcode:{value}"
    return f"url:{event_url}"


def _media_record(metadata: dict[str, Any], event_url: str) -> dict[str, Any]:
    display_url = _text(metadata.get("display_url"))
    video_url = _text(metadata.get("video_url"))
    if not display_url and not event_url.startswith("ytdl:") and not video_url:
        display_url = event_url
    return {
        "media_id": _text(metadata.get("media_id")),
        "shortcode": _text(metadata.get("shortcode")),
        "index": _integer(metadata.get("num")) or 1,
        "media_type": "video" if video_url else "image",
        "display_url": display_url,
        "video_url": video_url,
        "width": _integer(metadata.get("width")),
        "height": _integer(metadata.get("height")),
        "tagged_users": metadata.get("tagged_users") or [],
    }


def _merge_media(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    for name, value in incoming.items():
        if current.get(name) in (None, "", [], {}) and value not in (None, "", [], {}):
            current[name] = value
    if incoming.get("media_type") == "video":
        current["media_type"] = "video"


def normalize_events(
    events: Iterable[Any], fetched_at: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return normalized posts and upstream errors.

    gallery-dl emits one directory message per post and one or more URL
    messages per media item. Video posts may emit both a video and a preview
    image URL; those messages are merged by media ID.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    posts: dict[str, dict[str, Any]] = {}
    media_indexes: dict[str, dict[str, dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []

    for event in events:
        if not isinstance(event, list) or not event:
            continue
        message_type = event[0]

        if message_type == ERROR_MESSAGE:
            detail = event[1] if len(event) > 1 and isinstance(event[1], dict) else {}
            errors.append(
                {
                    "error": _text(detail.get("error")) or "UpstreamError",
                    "message": _text(detail.get("message")) or "Unknown upstream error",
                }
            )
            continue

        if message_type == DIRECTORY_MESSAGE:
            metadata = event[1] if len(event) > 1 and isinstance(event[1], dict) else {}
            key = _post_key(metadata)
            if key is None:
                errors.append(
                    {
                        "error": "SchemaError",
                        "message": "Skipped a post event without post_id, shortcode, or post_url",
                    }
                )
                continue
            if key not in posts:
                posts[key] = _new_post(metadata, fetched_at)
                media_indexes[key] = {}
            else:
                _merge_post_fields(posts[key], metadata)
            continue

        if message_type != URL_MESSAGE or len(event) < 3:
            continue
        event_url = _text(event[1]) or ""
        metadata = event[2] if isinstance(event[2], dict) else {}
        key = _post_key(metadata)
        if key is None:
            errors.append(
                {
                    "error": "SchemaError",
                    "message": "Skipped a media event without post_id, shortcode, or post_url",
                }
            )
            continue
        if key not in posts:
            posts[key] = _new_post(metadata, fetched_at)
            media_indexes[key] = {}
        else:
            _merge_post_fields(posts[key], metadata)

        media_key = _media_key(metadata, event_url)
        incoming = _media_record(metadata, event_url)
        current = media_indexes[key].get(media_key)
        if current is None:
            media_indexes[key][media_key] = incoming
            posts[key]["media"].append(incoming)
        else:
            _merge_media(current, incoming)

    result = list(posts.values())
    for post in result:
        post["media"].sort(
            key=lambda item: (item.get("index") or 0, item.get("media_id") or "")
        )
        post["media_count"] = len(post["media"])
    return result, errors


def record_key(post: dict[str, Any]) -> str:
    """Stable key used for incremental local storage."""
    if value := _text(post.get("post_id")):
        return f"id:{value}"
    if value := _text(post.get("shortcode")):
        return f"shortcode:{value}"
    if value := _text(post.get("post_url")):
        return f"url:{value}"
    raise ValueError("Post record has no post_id, shortcode, or post_url")


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _upgrade_post_schema(post: dict[str, Any]) -> dict[str, Any]:
    """Backfill content fields when merging records created by schema v1."""
    result = deepcopy(post)
    body = _text(result.get("body")) or _text(result.get("caption")) or ""
    result["schema_version"] = SCHEMA_VERSION
    result["body"] = body
    result["caption"] = _text(result.get("caption")) or body
    result["title"] = _text(result.get("title")) or _content_title({}, body)
    return result


def _media_identity(media: dict[str, Any]) -> str:
    if value := _text(media.get("media_id")):
        return f"id:{value}"
    if value := _text(media.get("shortcode")):
        return f"shortcode:{value}"
    return f"index:{media.get('index', 1)}"


def _merge_media_history(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    old = {_media_identity(item): item for item in previous}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in current:
        key = _media_identity(item)
        if key in old:
            result.append(_merge_missing_fields(old[key], item))
        else:
            result.append(deepcopy(item))
        seen.add(key)
    for key, item in old.items():
        if key not in seen:
            result.append(deepcopy(item))
    result.sort(key=lambda item: (item.get("index") or 0, item.get("media_id") or ""))
    return result


def _merge_missing_fields(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Overlay meaningful current values while retaining missing old fields."""
    result = deepcopy(previous)
    for name, value in current.items():
        old_value = result.get(name)
        if name == "media" and isinstance(old_value, list) and isinstance(value, list):
            result[name] = _merge_media_history(old_value, value)
        elif name == "source_tags" and isinstance(value, list):
            result[name] = _merge_string_lists(old_value or [], value)
        elif isinstance(old_value, dict) and isinstance(value, dict):
            result[name] = _merge_missing_fields(old_value, value)
        elif _has_value(value):
            result[name] = deepcopy(value)
    if isinstance(result.get("media"), list):
        result["media_count"] = len(result["media"])
    return result


def merge_posts(
    previous: Iterable[dict[str, Any]],
    current: Iterable[dict[str, Any]],
    *,
    preserve_missing: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Put current records first and retain older records not seen this run.

    With ``preserve_missing``, incomplete current records are overlaid on the
    previous version instead of erasing fields and media that were not returned.
    """
    old: dict[str, dict[str, Any]] = {}
    for post in previous:
        upgraded = _upgrade_post_schema(post)
        old[record_key(upgraded)] = upgraded
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    new_count = 0

    for raw_post in current:
        post = _upgrade_post_schema(raw_post)
        key = record_key(post)
        if key not in old:
            new_count += 1
            merged.append(post)
        elif preserve_missing:
            merged.append(_merge_missing_fields(old[key], post))
        else:
            merged.append(post)
        seen.add(key)
    for key, post in old.items():
        if key not in seen:
            merged.append(post)
    return merged, new_count

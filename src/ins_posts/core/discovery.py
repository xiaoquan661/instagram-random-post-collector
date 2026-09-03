"""Curated, transparent random-discovery sources for Instagram posts."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import quote

# Instagram does not expose a global uniform-random post feed.  Random mode
# samples several deliberately broad, non-sensitive hashtag feeds and then
# shuffles the deduplicated posts locally.  Keep the pool visible and auditable.
DISCOVERY_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "photography",
        "streetphotography",
        "fotografia",
        "photographie",
        "写真",
        "사진",
        "摄影",
    ),
    ("travel", "nature", "hiking", "viaje", "voyage", "여행", "旅行", "户外"),
    ("food", "cooking", "coffee", "comida", "料理", "음식", "美食"),
    ("art", "music", "books", "arte", "kunst", "音楽", "音乐", "读书"),
    ("technology", "science", "space", "coding", "ciencia", "科学", "科技"),
    ("fitness", "running", "cycling", "sports", "deporte", "운동", "健身"),
    (
        "design",
        "architecture",
        "fashion",
        "diseño",
        "architektur",
        "建築",
        "设计",
    ),
    ("animals", "pets", "dogs", "cats", "mascotas", "حيوانات", "宠物"),
)
DISCOVERY_POOL_VERSION = 1
MIN_DISCOVERY_SOURCES = 1
MAX_DISCOVERY_SOURCES = 12
DEFAULT_DISCOVERY_SOURCES = 4
_SOURCE_RANDOM_SALT = 0x51A7D15C0


@dataclass(frozen=True, slots=True)
class DiscoveryPlan:
    seed: int
    tags: tuple[str, ...]

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(
            f"https://www.instagram.com/explore/tags/{quote(tag, safe='')}/"
            for tag in self.tags
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": "curated-hashtag-sample",
            "pool_version": DISCOVERY_POOL_VERSION,
            "pool_size": sum(len(group) for group in DISCOVERY_GROUPS),
            "uniform_global_sample": False,
            "seed": self.seed,
            "tags": list(self.tags),
            "sources": list(self.targets),
        }


def create_discovery_plan(
    source_count: int = DEFAULT_DISCOVERY_SOURCES, seed: int | None = None
) -> DiscoveryPlan:
    if not MIN_DISCOVERY_SOURCES <= source_count <= MAX_DISCOVERY_SOURCES:
        raise ValueError(
            f"随机来源数必须在 {MIN_DISCOVERY_SOURCES} 到 "
            f"{MAX_DISCOVERY_SOURCES} 之间。"
        )
    actual_seed = secrets.randbits(64) if seed is None else seed
    rng = random.Random(actual_seed ^ _SOURCE_RANDOM_SALT)
    groups = list(DISCOVERY_GROUPS)
    rng.shuffle(groups)

    selected = [rng.choice(group) for group in groups[: min(source_count, len(groups))]]
    if source_count > len(selected):
        remaining = [
            tag
            for group in DISCOVERY_GROUPS
            for tag in group
            if tag not in selected
        ]
        selected.extend(rng.sample(remaining, source_count - len(selected)))
    return DiscoveryPlan(seed=actual_seed, tags=tuple(selected))


T = TypeVar("T")


def shuffle_discovered_posts(posts: list[T], seed: int) -> list[T]:
    """Return a deterministic local shuffle without mutating the caller's list."""
    result = list(posts)
    random.Random(seed ^ 0x5A9F11E).shuffle(result)
    return result

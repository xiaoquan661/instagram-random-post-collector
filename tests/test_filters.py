import pytest

from ins_posts.core.filters import (
    FilterSpec,
    FilterValidationError,
    apply_filters,
)


def _post(post_id: str, **overrides):
    post = {
        "post_id": post_id,
        "published_at": "2026-08-15T12:00:00Z",
        "like_count": 50,
        "caption": "",
        "hashtags": [],
        "media": [],
    }
    post.update(overrides)
    return post


def _ids(posts):
    return [post["post_id"] for post in posts]


def test_date_filter_is_inclusive_at_both_calendar_boundaries_and_rejects_missing():
    posts = [
        _post("lower", published_at="2026-08-01T00:00:00Z"),
        _post("upper", published_at="2026-08-31T23:59:59Z"),
        _post("before", published_at="2026-07-31T23:59:59Z"),
        _post("after", published_at="2026-09-01T00:00:00Z"),
        _post("missing", published_at=None),
        _post("invalid", published_at="not-a-date"),
    ]

    matched, rejected = apply_filters(
        posts,
        FilterSpec.from_values(since="2026-08-01", until="2026-08-31"),
    )

    assert _ids(matched) == ["lower", "upper"]
    assert rejected == {
        "after_until": 1,
        "before_since": 1,
        "date_missing": 2,
    }


def test_like_filter_includes_numeric_bounds_and_reports_missing_values():
    posts = [
        _post("minimum", like_count=10),
        _post("maximum", like_count=20),
        _post("below", like_count=9),
        _post("above", like_count=21),
        _post("missing", like_count=None),
        _post("invalid", like_count="hidden"),
    ]

    matched, rejected = apply_filters(
        posts,
        FilterSpec.from_values(min_likes=10, max_likes=20),
    )

    assert _ids(matched) == ["minimum", "maximum"]
    assert rejected == {
        "likes_above_max": 1,
        "likes_below_min": 1,
        "likes_missing": 2,
    }


def test_zero_likes_is_conservatively_unknown_even_when_zero_is_in_range():
    posts = [_post("ambiguous-zero", like_count=0), _post("known-one", like_count=1)]

    matched, rejected = apply_filters(
        posts,
        FilterSpec.from_values(min_likes=0, max_likes=100),
    )

    assert _ids(matched) == ["known-one"]
    assert rejected == {"likes_unknown_or_zero": 1}


def test_keywords_use_nfkc_casefold_with_any_and_all_modes():
    post = _post(
        "unicode",
        # Full-width ABC and a decomposed acute accent exercise both NFKC and
        # case-insensitive matching.
        caption="新品 ＡＢＣ Cafe\u0301 夏日指南",
    )

    any_matches, _ = apply_filters(
        [post],
        FilterSpec.from_values(
            keywords="不存在, CAFÉ",
            keyword_mode="any",
        ),
    )
    all_matches, _ = apply_filters(
        [post],
        FilterSpec.from_values(
            keywords="abc, café, 夏日",
            keyword_mode="all",
        ),
    )
    failed_all, rejected = apply_filters(
        [post],
        FilterSpec.from_values(
            keywords="abc, 冬日",
            keyword_mode="all",
        ),
    )

    assert _ids(any_matches) == ["unicode"]
    assert _ids(all_matches) == ["unicode"]
    assert failed_all == []
    assert rejected == {"keywords": 1}


def test_hashtags_match_exact_normalized_tags_in_any_and_all_modes():
    post = _post("tags", hashtags=["#Travel", "#上海", "#AI"])

    any_matches, _ = apply_filters(
        [post],
        FilterSpec.from_values(
            hashtags="travelgram, #TRAVEL",
            hashtag_mode="any",
        ),
    )
    all_matches, _ = apply_filters(
        [post],
        FilterSpec.from_values(
            hashtags="travel, 上海",
            hashtag_mode="all",
        ),
    )
    exact_miss, exact_rejected = apply_filters(
        [post],
        FilterSpec.from_values(hashtags="travelgram", hashtag_mode="any"),
    )
    missing_all, all_rejected = apply_filters(
        [post],
        FilterSpec.from_values(hashtags="travel, food", hashtag_mode="all"),
    )

    assert _ids(any_matches) == ["tags"]
    assert _ids(all_matches) == ["tags"]
    assert exact_miss == []
    assert exact_rejected == {"hashtags": 1}
    assert missing_all == []
    assert all_rejected == {"hashtags": 1}


def test_media_filter_means_contains_type_including_mixed_carousels():
    posts = [
        _post("image", media=[{"media_type": "image"}]),
        _post("video", media=[{"media_type": "video"}]),
        _post(
            "mixed",
            media=[{"media_type": "image"}, {"media_type": "video"}],
        ),
        _post("missing", media=[]),
    ]

    images, image_rejections = apply_filters(
        posts, FilterSpec.from_values(media_type="image")
    )
    videos, video_rejections = apply_filters(
        posts, FilterSpec.from_values(media_type="video")
    )

    assert _ids(images) == ["image", "mixed"]
    assert image_rejections == {"media_type": 2}
    assert _ids(videos) == ["video", "mixed"]
    assert video_rejections == {"media_type": 2}


@pytest.mark.parametrize(
    "values",
    [
        {"since": "2026-09-01", "until": "2026-08-31"},
        {"min_likes": 101, "max_likes": 100},
    ],
)
def test_rejects_reversed_ranges(values):
    with pytest.raises(FilterValidationError):
        FilterSpec.from_values(**values)

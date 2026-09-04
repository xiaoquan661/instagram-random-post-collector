from ins_posts.core.normalize import merge_posts, normalize_events


def test_normalizes_carousel_and_merges_video_preview():
    common = {
        "post_id": "100",
        "post_shortcode": "ABC123",
        "post_url": "https://www.instagram.com/p/ABC123/",
        "username": "example",
        "owner_id": "42",
        "post_date": "2026-08-30 12:34:56",
        "description": "A caption #Demo",
        "likes": 7,
        "count": 2,
    }
    events = [
        [2, common],
        [
            3,
            "https://cdn.example/one.jpg",
            {
                **common,
                "media_id": "101",
                "num": 1,
                "display_url": "https://cdn.example/one.jpg",
                "width": 1080,
                "height": 1080,
            },
        ],
        [
            3,
            "ytdl:https://example/video",
            {
                **common,
                "media_id": "102",
                "num": 2,
                "display_url": "https://cdn.example/two.jpg",
                "video_url": "https://cdn.example/two.mp4",
                "width": 1080,
                "height": 1920,
            },
        ],
        [
            3,
            "https://cdn.example/two.jpg",
            {
                **common,
                "media_id": "102",
                "num": 2,
                "display_url": "https://cdn.example/two.jpg",
                "video_url": "https://cdn.example/two.mp4",
                "width": 1080,
                "height": 1920,
            },
        ],
    ]

    posts, errors = normalize_events(events, fetched_at="2026-08-31T00:00:00+00:00")

    assert errors == []
    assert len(posts) == 1
    post = posts[0]
    assert post["post_id"] == "100"
    assert post["schema_version"] == 2
    assert post["title"] == "A caption #Demo"
    assert post["body"] == "A caption #Demo"
    assert post["caption"] == post["body"]
    assert post["published_at"] == "2026-08-30T12:34:56Z"
    assert post["hashtags"] == ["#Demo"]
    assert post["media_count"] == 2
    assert post["media"][1]["media_type"] == "video"
    assert post["media"][1]["video_url"] == "https://cdn.example/two.mp4"


def test_extracts_explicit_title_body_accessibility_and_engagement():
    events = [
        [
            2,
            {
                "post_id": "200",
                "title": "官方标题",
                "description": "第一行正文。\n第二行保留。",
                "accessibility_caption": "图片中是一片海滩",
                "comments_count": 12,
                "video_view_count": 345,
                "play_count": 456,
            },
        ]
    ]

    posts, errors = normalize_events(events)

    assert errors == []
    assert posts[0]["title"] == "官方标题"
    assert posts[0]["body"] == "第一行正文。\n第二行保留。"
    assert posts[0]["accessibility_text"] == "图片中是一片海滩"
    assert posts[0]["comment_count"] == 12
    assert posts[0]["view_count"] == 345
    assert posts[0]["play_count"] == 456


def test_derives_and_limits_title_from_first_body_line():
    long_first_line = "这是一段很长的标题" * 20
    posts, _ = normalize_events(
        [[2, {"post_id": "300", "description": f"{long_first_line}\n正文第二行"}]]
    )

    assert len(posts[0]["title"]) == 120
    assert posts[0]["title"].endswith("…")
    assert posts[0]["body"].endswith("正文第二行")


def test_derived_title_stops_after_first_sentence():
    posts, _ = normalize_events(
        [[2, {"post_id": "301", "description": "第一句。第二句继续说明"}]]
    )

    assert posts[0]["title"] == "第一句。"
    assert posts[0]["body"] == "第一句。第二句继续说明"


def test_collects_upstream_error():
    posts, errors = normalize_events(
        [
            [
                -1,
                {"error": "AbortExtraction", "message": "HTTP redirect to login page"},
            ],
        ]
    )

    assert posts == []
    assert errors == [
        {"error": "AbortExtraction", "message": "HTTP redirect to login page"}
    ]


def test_merges_discovery_source_tags_without_polluting_caption_hashtags():
    base = {
        "post_id": "100",
        "post_shortcode": "ABC123",
        "description": "A caption #RealTag",
    }

    posts, errors = normalize_events(
        [[2, {**base, "tag": "photography"}], [2, {**base, "tag": "旅行"}]]
    )

    assert errors == []
    assert posts[0]["hashtags"] == ["#RealTag"]
    assert posts[0]["source_tags"] == ["photography", "旅行"]


def test_skips_event_without_stable_post_identity():
    posts, errors = normalize_events(
        [[2, {"description": "missing identity"}], [3, "https://cdn/x.jpg", {}]]
    )

    assert posts == []
    assert [item["error"] for item in errors] == ["SchemaError", "SchemaError"]


def test_incremental_merge_replaces_seen_and_keeps_history():
    previous = [
        {"post_id": "1", "shortcode": "OLD", "caption": "old value"},
        {"post_id": "2", "shortcode": "HISTORY", "caption": "history"},
    ]
    current = [
        {"post_id": "1", "shortcode": "OLD", "caption": "updated"},
        {"post_id": "3", "shortcode": "NEW", "caption": "new"},
    ]

    merged, new_count = merge_posts(previous, current)

    assert new_count == 1
    assert [post["post_id"] for post in merged] == ["1", "3", "2"]
    assert merged[0]["caption"] == "updated"


def test_partial_merge_preserves_missing_fields_and_media():
    previous = [
        {
            "post_id": "1",
            "caption": "complete caption",
            "location": {"id": "99", "slug": "somewhere"},
            "media": [
                {"media_id": "11", "index": 1, "display_url": "old-one"},
                {"media_id": "12", "index": 2, "display_url": "old-two"},
            ],
            "media_count": 2,
        }
    ]
    current = [
        {
            "post_id": "1",
            "caption": "",
            "location": None,
            "media": [
                {"media_id": "11", "index": 1, "display_url": "new-one"},
            ],
            "media_count": 1,
        }
    ]

    merged, new_count = merge_posts(previous, current, preserve_missing=True)

    assert new_count == 0
    assert merged[0]["caption"] == "complete caption"
    assert merged[0]["body"] == "complete caption"
    assert merged[0]["title"] == "complete caption"
    assert merged[0]["schema_version"] == 2
    assert merged[0]["location"]["id"] == "99"
    assert [item["media_id"] for item in merged[0]["media"]] == ["11", "12"]
    assert merged[0]["media"][0]["display_url"] == "new-one"
    assert merged[0]["media_count"] == 2


def test_partial_merge_unions_discovery_provenance():
    previous = [{"post_id": "1", "source_tags": ["travel"]}]
    current = [{"post_id": "1", "source_tags": ["nature"]}]

    merged, _ = merge_posts(previous, current, preserve_missing=True)

    assert merged[0]["source_tags"] == ["nature", "travel"]

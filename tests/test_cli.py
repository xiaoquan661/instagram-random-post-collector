import argparse
import json

import pytest

from ins_posts.collector import cli
from ins_posts.collector.cli import (
    CollectorError,
    normalize_target,
    validate_delay,
    validate_include,
)
from ins_posts.core.discovery import create_discovery_plan, shuffle_discovered_posts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("instagram", "https://www.instagram.com/instagram/"),
        ("@instagram", "https://www.instagram.com/instagram/"),
        ("https://www.instagram.com/p/ABC123/", "https://www.instagram.com/p/ABC123/"),
        (
            "https://instagram.com/nasa/reels/?hl=en",
            "https://www.instagram.com/nasa/reels/",
        ),
        (
            "http://m.instagram.com/explore/tags/%E4%B8%8A%E6%B5%B7/?hl=zh-cn",
            "https://www.instagram.com/explore/tags/%E4%B8%8A%E6%B5%B7/",
        ),
    ],
)
def test_normalize_target(value, expected):
    assert normalize_target(value) == expected


def test_rejects_non_instagram_url():
    with pytest.raises(CollectorError):
        normalize_target("https://example.com/post/1")


@pytest.mark.parametrize(
    "value",
    [
        "https://www.instagram.com/nasa/followers/",
        "https://www.instagram.com/nasa/following/",
        "https://www.instagram.com/nasa/saved/",
        "https://www.instagram.com/stories/me/",
        "https://www.instagram.com/nasa/info/",
        "https://www.instagram.com/nasa/tagged/",
        "https://user@www.instagram.com/p/ABC/",
    ],
)
def test_rejects_instagram_routes_outside_post_collection_scope(value):
    with pytest.raises(CollectorError):
        normalize_target(value)


def test_validates_delay_and_include():
    assert validate_delay("6-12") == "6-12"
    assert validate_include("posts,reels") == "posts,reels"
    with pytest.raises(argparse.ArgumentTypeError):
        validate_delay("0.2")
    with pytest.raises(argparse.ArgumentTypeError):
        validate_include("stories")


def test_partial_metadata_does_not_start_media_download(tmp_path, monkeypatch):
    events = [
        [
            2,
            {
                "post_id": "1",
                "post_shortcode": "ABC",
                "post_url": "https://www.instagram.com/p/ABC/",
            },
        ],
        [-1, {"error": "HttpError", "message": "429 Too Many Requests"}],
    ]
    args = cli.build_parser().parse_args(
        [
            "instagram",
            "--output",
            str(tmp_path),
            "--include",
            "posts,reels",
            "--download-media",
        ]
    )
    attempted = []

    def partial_metadata(*_, include=None):
        attempted.append(include)
        return events, ""

    monkeypatch.setattr(cli, "_run_metadata", partial_metadata)

    def unexpected_download(*_):
        pytest.fail("media download must not run after partial upstream failure")

    monkeypatch.setattr(cli, "_run_download", unexpected_download)

    assert cli.run(args) == 4
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial"
    assert summary["media_download_skipped_reason"]
    assert summary["attempted_include"] == ["posts"]
    assert attempted == ["posts"]


def test_metadata_launch_failure_writes_audit(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(["instagram", "--output", str(tmp_path)])

    def fail_metadata(*_, **__):
        raise CollectorError("upstream failed")

    monkeypatch.setattr(cli, "_run_metadata", fail_metadata)

    with pytest.raises(CollectorError, match="upstream failed"):
        cli.run(args)
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["errors"][0]["message"] == "upstream failed"


def test_media_download_stops_before_next_content_type(tmp_path, monkeypatch):
    post_events = [
        [
            2,
            {
                "post_id": "1",
                "post_shortcode": "ABC",
                "post_url": "https://www.instagram.com/p/ABC/",
            },
        ]
    ]
    args = cli.build_parser().parse_args(
        [
            "instagram",
            "--output",
            str(tmp_path),
            "--include",
            "posts,reels",
            "--download-media",
        ]
    )

    def metadata(*_, include=None):
        return (post_events if include == "posts" else []), ""

    attempted = []

    def download(*_, include=None, post_shortcodes=None):
        attempted.append(include)
        assert post_shortcodes == ["ABC"]
        if include == "posts":
            return 8, "429 Too Many Requests"
        pytest.fail("reels must not run after posts download fails")

    monkeypatch.setattr(cli, "_run_metadata", metadata)
    monkeypatch.setattr(cli, "_run_download", download)

    assert cli.run(args) == 5
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial"
    assert summary["attempted_download_include"] == ["posts"]
    assert attempted == ["posts"]


def test_filters_write_current_view_and_limit_media_to_matches(tmp_path, monkeypatch):
    events = [
        [
            2,
            {
                "post_id": "1",
                "post_shortcode": "KEEP1",
                "post_url": "https://www.instagram.com/p/KEEP1/",
                "description": "Please keep this",
                "likes": 20,
            },
        ],
        [
            2,
            {
                "post_id": "2",
                "post_shortcode": "DROP2",
                "post_url": "https://www.instagram.com/p/DROP2/",
                "description": "Not selected",
                "likes": 30,
            },
        ],
    ]
    args = cli.build_parser().parse_args(
        [
            "instagram",
            "--output",
            str(tmp_path),
            "--keywords",
            "KEEP",
            "--download-media",
        ]
    )
    monkeypatch.setattr(cli, "_run_metadata", lambda *_, **__: (events, ""))
    selected = []

    def download(*_, include=None, post_shortcodes=None):
        selected.extend(post_shortcodes or [])
        return 0, ""

    monkeypatch.setattr(cli, "_run_download", download)

    assert cli.run(args) == 0
    extracted = (tmp_path / "extracted.jsonl").read_text(encoding="utf-8")
    current = (tmp_path / "current.jsonl").read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert extracted.count("\n") == 2
    assert current.count("\n") == 1
    assert "KEEP1" in current and "DROP2" not in current
    assert selected == ["KEEP1"]
    assert summary["scanned_this_run"] == 2
    assert summary["matched_this_run"] == 1


def test_zero_filter_matches_is_success_and_skips_media(tmp_path, monkeypatch):
    events = [
        [
            2,
            {
                "post_id": "1",
                "post_shortcode": "ABC",
                "post_url": "https://www.instagram.com/p/ABC/",
                "description": "ordinary caption",
            },
        ]
    ]
    args = cli.build_parser().parse_args(
        [
            "instagram",
            "--output",
            str(tmp_path),
            "--keywords",
            "not present",
            "--download-media",
        ]
    )
    monkeypatch.setattr(cli, "_run_metadata", lambda *_, **__: (events, ""))
    monkeypatch.setattr(
        cli,
        "_run_download",
        lambda *_args, **_kwargs: pytest.fail("no media should be requested"),
    )

    assert cli.run(args) == 0
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "ok"
    assert summary["matched_this_run"] == 0
    assert summary["media_download_skipped_reason"]
    assert (tmp_path / "current.jsonl").read_text(encoding="utf-8") == ""


def test_matched_post_without_shortcode_returns_partial_media_status(
    tmp_path, monkeypatch
):
    events = [
        [
            2,
            {
                "post_id": "1",
                "post_url": "https://www.instagram.com/p/unknown/",
                "description": "selected",
            },
        ]
    ]
    args = cli.build_parser().parse_args(
        [
            "instagram",
            "--output",
            str(tmp_path),
            "--keywords",
            "selected",
            "--download-media",
        ]
    )
    monkeypatch.setattr(cli, "_run_metadata", lambda *_, **__: (events, ""))
    monkeypatch.setattr(
        cli,
        "_run_download",
        lambda *_args, **_kwargs: pytest.fail("unsafe media download attempted"),
    )

    assert cli.run(args) == 5
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial"
    assert "shortcode" in summary["media_download_skipped_reason"]


def test_result_limit_keeps_newest_across_combined_sources(tmp_path, monkeypatch):
    events = [
        [
            2,
            {
                "post_id": "old",
                "post_shortcode": "OLD",
                "post_url": "https://www.instagram.com/p/OLD/",
                "post_date": "2026-01-01 00:00:00",
            },
        ],
        [
            2,
            {
                "post_id": "new",
                "post_shortcode": "NEW",
                "post_url": "https://www.instagram.com/p/NEW/",
                "post_date": "2026-08-31 00:00:00",
            },
        ],
    ]
    args = cli.build_parser().parse_args(
        ["instagram", "--output", str(tmp_path), "--max-results", "1"]
    )
    monkeypatch.setattr(cli, "_run_metadata", lambda *_, **__: (events, ""))

    assert cli.run(args) == 0
    current = json.loads(
        (tmp_path / "current.jsonl").read_text(encoding="utf-8").strip()
    )
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert current["post_id"] == "new"
    assert summary["matched_before_limit"] == 2
    assert summary["result_limit_reached"] is True


def test_no_target_defaults_to_random_filter_then_shuffle(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(
        [
            "--output",
            str(tmp_path),
            "--random-sources",
            "2",
            "--random-seed",
            "17",
            "--keywords",
            "keep",
            "--max-results",
            "2",
        ]
    )
    calls = []

    def metadata(_, target, __, *, include=None):
        calls.append((target, include))
        tag = "source-a" if len(calls) == 1 else "source-b"
        values = (
            [("1", "ONE", "keep one"), ("drop", "DROP", "discard")]
            if len(calls) == 1
            else [("1", "ONE", "keep one"), ("2", "TWO", "keep two"), ("3", "THREE", "keep three")]
        )
        return [
            [
                2,
                {
                    "post_id": post_id,
                    "post_shortcode": shortcode,
                    "post_url": f"https://www.instagram.com/p/{shortcode}/",
                    "description": caption,
                    "tag": tag,
                },
            ]
            for post_id, shortcode, caption in values
        ], ""

    monkeypatch.setattr(cli, "_run_metadata", metadata)
    monkeypatch.setattr(cli, "_wait_between_sources", lambda _: None)

    assert cli.run(args) == 0
    current = [
        json.loads(line)
        for line in (tmp_path / "current.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    expected = shuffle_discovered_posts(
        [{"post_id": "1"}, {"post_id": "2"}, {"post_id": "3"}], 17
    )[:2]
    summary = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))

    assert args.target is None
    assert [item["post_id"] for item in current] == [
        item["post_id"] for item in expected
    ]
    assert len(calls) == 2
    assert all(include == "posts" for _, include in calls)
    assert summary["mode"] == "random"
    assert summary["target"] is None
    assert summary["matched_before_limit"] == 3
    assert summary["discovery"]["uniform_global_sample"] is False
    extracted = [
        json.loads(line)
        for line in (tmp_path / "extracted.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    duplicate = next(item for item in extracted if item["post_id"] == "1")
    assert duplicate["source_tags"] == ["source-a", "source-b"]


def test_random_media_download_uses_only_exact_selected_post(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(
        [
            "--output",
            str(tmp_path),
            "--random-sources",
            "1",
            "--random-seed",
            "3",
            "--max-results",
            "1",
            "--download-media",
        ]
    )
    events = [
        [
            2,
            {
                "post_id": str(number),
                "post_shortcode": shortcode,
                "post_url": f"https://www.instagram.com/p/{shortcode}/",
            },
        ]
        for number, shortcode in [(1, "ONE"), (2, "TWO")]
    ]
    attempted = []
    monkeypatch.setattr(cli, "_run_metadata", lambda *_, **__: (events, ""))

    def download(_, target, __, *, include=None, post_shortcodes=None):
        attempted.append((target, include, post_shortcodes))
        return 0, ""

    monkeypatch.setattr(cli, "_run_download", download)

    assert cli.run(args) == 0
    selected = json.loads(
        (tmp_path / "current.jsonl").read_text(encoding="utf-8").strip()
    )
    assert attempted == [(selected["post_url"], "posts", None)]


def test_random_flag_rejects_target_before_collection(monkeypatch):
    args = cli.build_parser().parse_args(["instagram", "--random"])
    monkeypatch.setattr(
        cli,
        "_run_metadata",
        lambda *_args, **_kwargs: pytest.fail("collection must not start"),
    )

    with pytest.raises(CollectorError, match="不需要 target"):
        cli.run(args)


def test_random_sources_stop_immediately_on_rate_limit(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(
        ["--random-sources", "3", "--random-seed", "9"]
    )
    plan = create_discovery_plan(3, 9)
    attempted = []

    def blocked(_, target, __, *, include=None):
        attempted.append(target)
        raise CollectorError("429 Too Many Requests")

    monkeypatch.setattr(cli, "_run_metadata", blocked)
    events, _, sources = cli._run_random_metadata(args, plan, tmp_path)

    assert len(attempted) == 1
    assert events[0][0] == -1
    assert sources[0]["status"] == "failed"

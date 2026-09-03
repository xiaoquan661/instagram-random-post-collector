from ins_posts.core.discovery import (
    DISCOVERY_GROUPS,
    MAX_DISCOVERY_SOURCES,
    DiscoveryPlan,
    create_discovery_plan,
    shuffle_discovered_posts,
)


def test_discovery_plan_is_deterministic_for_a_fixed_seed():
    first = create_discovery_plan(source_count=6, seed=20260831)
    second = create_discovery_plan(source_count=6, seed=20260831)

    assert first == second
    assert first.seed == 20260831
    assert first.to_dict() == second.to_dict()


def test_discovery_plan_never_selects_duplicate_or_unknown_tags():
    pool = {tag for group in DISCOVERY_GROUPS for tag in group}

    # Exercise both the one-tag-per-group branch and the extra-source branch.
    for seed in range(20):
        plan = create_discovery_plan(
            source_count=MAX_DISCOVERY_SOURCES,
            seed=seed,
        )

        assert len(plan.tags) == MAX_DISCOVERY_SOURCES
        assert len(set(plan.tags)) == len(plan.tags)
        assert set(plan.tags) <= pool


def test_discovery_targets_percent_encode_unicode_and_reserved_characters():
    plan = DiscoveryPlan(seed=7, tags=("摄影", "street photo/#?"))

    assert plan.targets == (
        "https://www.instagram.com/explore/tags/%E6%91%84%E5%BD%B1/",
        "https://www.instagram.com/explore/tags/street%20photo%2F%23%3F/",
    )
    assert plan.to_dict()["sources"] == list(plan.targets)


def test_shuffle_is_deterministic_and_does_not_mutate_input():
    posts = [{"post_id": str(index)} for index in range(10)]
    original = [dict(post) for post in posts]

    first = shuffle_discovered_posts(posts, seed=12345)
    second = shuffle_discovered_posts(posts, seed=12345)

    assert first == second
    assert first != original
    assert posts == original
    assert first is not posts
    assert {post["post_id"] for post in first} == {
        post["post_id"] for post in original
    }

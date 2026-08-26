"""Unit tests for Stagenator's deterministic core — the parts where
correctness is non-negotiable and no LLM is involved."""

import datetime as dt

from agent import config
from agent.pipelines.replenish import (
    APPLE_CODE_LIFETIME_DAYS,
    _judge,
)
from agent.pipelines.subliminal import (
    build_layout,
    build_solution_svg,
)

NOW_MS = int(dt.datetime(2026, 8, 25, tzinfo=dt.UTC).timestamp() * 1000)
TODAY = "2026-08-25"


def ms_ago(days: float) -> int:
    return NOW_MS - int(days * 86_400_000)


# ---------------------------------------------------------- expiry rules ----


class TestExpiryJudgement:
    def test_promotion_end_future_is_valid(self):
        assert (
            _judge({"promotionEnd": "2027-01-20"}, "google", NOW_MS, TODAY) == "valid"
        )

    def test_promotion_end_past_is_expired(self):
        assert (
            _judge({"promotionEnd": "2026-02-10"}, "google", NOW_MS, TODAY) == "expired"
        )

    def test_promotion_end_today_is_valid(self):
        assert _judge({"promotionEnd": TODAY}, "google", NOW_MS, TODAY) == "valid"

    def test_apple_within_28_days_valid(self):
        assert _judge({"createdAt": ms_ago(27)}, "apple", NOW_MS, TODAY) == "valid"

    def test_apple_over_28_days_expired(self):
        assert (
            _judge(
                {"createdAt": ms_ago(APPLE_CODE_LIFETIME_DAYS + 1)},
                "apple",
                NOW_MS,
                TODAY,
            )
            == "expired"
        )

    def test_apple_untraceable_expired(self):
        assert _judge({}, "apple", NOW_MS, TODAY) == "expired"

    def test_google_old_legacy_expired(self):
        assert _judge({"createdAt": ms_ago(400)}, "google", NOW_MS, TODAY) == "expired"

    def test_google_midage_legacy_suspect(self):
        assert _judge({"createdAt": ms_ago(120)}, "google", NOW_MS, TODAY) == "suspect"

    def test_google_fresh_legacy_valid(self):
        assert _judge({"createdAt": ms_ago(10)}, "google", NOW_MS, TODAY) == "valid"

    def test_promotion_end_beats_age(self):
        # metadata is authoritative even for ancient codes
        assert (
            _judge(
                {"promotionEnd": "2099-01-01", "createdAt": ms_ago(400)},
                "google",
                NOW_MS,
                TODAY,
            )
            == "valid"
        )


class TestSubliminalContract:
    def test_layout_within_word_level_svg_ranges(self):
        for _ in range(50):
            for letter in build_layout("STORM"):
                assert 0.0 <= letter["x"] <= 1.0
                assert 0.0 <= letter["y"] <= 1.0
                assert 24 <= letter["fontSize"] <= 512
                assert 0.2 <= letter["scaleX"] <= 12.0
                assert 0.2 <= letter["scaleY"] <= 12.0
                assert abs(letter["rotationDegrees"]) <= 360
                assert abs(letter["skewXDegrees"]) <= 80
                assert 0.0 <= letter["opacity"] <= 1.0
                assert letter["fontWeightValue"] in (200, 400, 700)

    def test_svg_carries_answer_and_all_letters(self):
        # canonical <path> format (matches the admin dashboard word_level_svg.js)
        layout = build_layout("FROST")
        svg = build_solution_svg(layout, "FROST")
        assert 'data-font="Roboto"' in svg and "data-x=" in svg
        assert "<text" not in svg and "letter_" not in svg
        assert svg.count("<path ") == len("FROST")
        for lid in ("F", "R", "O", "S", "T"):
            assert f'id="{lid}"' in svg


# ---------------------------------------------------------------- config ----


class TestCaps:
    def test_caps_are_sane(self):
        assert 0 < config.CAPS["codes_per_game_per_day"] <= 50
        assert 0 < config.CAPS["levels_per_game_per_day"] <= 10
        assert 0 < config.CAPS["push_actions_per_game_per_4h"] <= 4

    def test_all_games_configured(self):
        for cfg in config.GAMES.values():
            assert cfg["project"] and cfg["ga_property"]
            assert "level_backend" in cfg


# --------------------------------------------------- memory / context bounds ----


class TestMemoryDiscipline:
    def test_reflector_context_aggregates_not_raw(self):
        """gather_day must aggregate, never raw-dump verbose ledger entries."""
        import json
        from unittest.mock import patch

        from agent import agent as A

        # a noisy ledger with verbose action results (media/prompts)
        fake = [
            {
                "kind": "action",
                "status": "done",
                "game": "ai-movie-quiz",
                "action": "level_pipeline",
                "result": {
                    "media": {"clip": "http://x/" + "a" * 500},
                    "design": {"veo_prompt": "p" * 500},
                },
            }
            for _ in range(50)
        ] + [{"kind": "brief", "brief": "b" * 900} for _ in range(5)]
        with (
            patch.object(A.state, "recent_ledger", return_value=fake),
            patch.object(A.state, "get_playbook", return_value={"version": 1}),
            patch.object(A.state, "pending_directives", return_value=[]),
            patch.object(A.rules, "campaign_inventory", return_value={"campaigns": {}}),
        ):
            ctx = json.loads(A.gather_day("nightly"))
        # 50 verbose actions collapse to a single count; no media/prompt leakage
        assert ctx["actions_taken"] == {"ai-movie-quiz:level_pipeline": 50}
        assert (
            "veo_prompt" not in A.gather_day.__doc__
            and "http://x/" not in json.dumps(ctx)
        )
        assert len(json.dumps(ctx)) < 2000  # bounded regardless of 50 noisy entries


# ── AI Movie Quiz: only ship titles the in-game keyboard can actually spell ──


def test_amq_title_playable_accepts_typeable_titles():
    from agent.pipelines.moviequiz import _title_playable

    for good in (
        "Mission: Impossible",
        "Spider-Man",
        "Se7en",
        "2001",
        "The Good, the Bad and the Ugly",
        "The Lord of the Rings: The Fellowship of the Ring",
    ):
        assert _title_playable(good), good


def test_amq_title_playable_rejects_unsolvable_and_oversized():
    from agent.pipelines.moviequiz import _title_playable

    # chars the on-screen keyboard cannot produce -> unwinnable level
    for bad in (
        "Schindler's List",
        "Fast & Furious",
        "Amélie",
        "Léon",
        "WALL·E",
        "",
        "   ",
    ):
        assert not _title_playable(bad), bad
    # absurdly long titles would overflow the box grid
    assert not _title_playable("A" * 45)


# ── Subliminal Words: solution SVG must be the dashboard's canonical <path> format ──


def test_subliminal_solution_svg_is_canonical_path_format():
    from agent.pipelines.subliminal import build_layout, build_solution_svg

    word = "REEL"  # duplicate E exercises the A/A2 id scheme
    svg = build_solution_svg(build_layout(word), word)
    assert "<text" not in svg and "letter_" not in svg  # NOT the old wrong format
    assert svg.count("<path ") == len(word)  # one outline path per letter
    assert 'viewBox="0 0 1024 1024"' in svg
    assert 'data-font="Roboto"' in svg and 'style="fill:#000000"' in svg
    for lid in ('id="R"', 'id="E"', 'id="E2"', 'id="L"'):
        assert lid in svg, lid

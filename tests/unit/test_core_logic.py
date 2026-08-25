"""Unit tests for Stagenator's deterministic core — the parts where
correctness is non-negotiable and no LLM is involved."""

import datetime as dt

import pytest

from agent import config
from agent.pipelines import levels
from agent.pipelines.replenish import (
    APPLE_CODE_LIFETIME_DAYS,
    _judge,
)
from agent.pipelines.subliminal import (
    BASE_FONT_SIZE,
    build_layout,
    build_solution_svg,
)


NOW_MS = int(dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc).timestamp() * 1000)
TODAY = "2026-08-25"


def ms_ago(days: float) -> int:
    return NOW_MS - int(days * 86_400_000)


# ---------------------------------------------------------- expiry rules ----

class TestExpiryJudgement:
    def test_promotion_end_future_is_valid(self):
        assert _judge({"promotionEnd": "2027-01-20"}, "google", NOW_MS, TODAY) == "valid"

    def test_promotion_end_past_is_expired(self):
        assert _judge({"promotionEnd": "2026-02-10"}, "google", NOW_MS, TODAY) == "expired"

    def test_promotion_end_today_is_valid(self):
        assert _judge({"promotionEnd": TODAY}, "google", NOW_MS, TODAY) == "valid"

    def test_apple_within_28_days_valid(self):
        assert _judge({"createdAt": ms_ago(27)}, "apple", NOW_MS, TODAY) == "valid"

    def test_apple_over_28_days_expired(self):
        assert _judge({"createdAt": ms_ago(APPLE_CODE_LIFETIME_DAYS + 1)}, "apple", NOW_MS, TODAY) == "expired"

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
        assert _judge({"promotionEnd": "2099-01-01", "createdAt": ms_ago(400)},
                      "google", NOW_MS, TODAY) == "valid"


# ------------------------------------------------------------- palindrome ----

class TestPalindromeValidation:
    @pytest.mark.parametrize("text", ["TAAT", "たまのまた", "Elu par cette crapule.",
                                      "GATEMAN SEES NAME, GARAGEMAN SEES NAME TAG."])
    def test_valid_palindromes(self, text):
        assert levels._is_palindrome(text)

    @pytest.mark.parametrize("text", ["HELLO", "AB", "", "NOT A PALINDROME"])
    def test_invalid_palindromes(self, text):
        assert not levels._is_palindrome(text)

    def test_normalization_ignores_case_punct(self):
        assert levels._norm("Elu par CETTE crapule.") == levels._norm("eluparcettecrapule")


# ------------------------------------------------- subliminal svg contract ----

class TestSubliminalContract:
    def test_layout_within_word_level_svg_ranges(self):
        for _ in range(50):
            for letter in build_layout("STORM"):
                assert 0.0 <= letter["x"] <= 1.0
                assert 0.0 <= letter["y"] <= 1.0
                assert 0.2 <= letter["scale"] <= 12.0
                assert abs(letter["rotation"]) <= 360
                assert abs(letter["skewX"]) <= 80
                assert 0.0 <= letter["opacity"] <= 1.0
                assert letter["weight"] in (200, 400, 700)
                assert 24 <= BASE_FONT_SIZE * letter["scale"] <= 512

    def test_svg_carries_answer_and_all_letters(self):
        layout = build_layout("FROST")
        svg = build_solution_svg(layout, "FROST")
        assert 'data-answer="FROST"' in svg
        for ch in "FROST":
            assert f">{ch}</text>" in svg


# ---------------------------------------------------------------- config ----

class TestCaps:
    def test_caps_are_sane(self):
        assert 0 < config.CAPS["codes_per_game_per_day"] <= 50
        assert 0 < config.CAPS["levels_per_game_per_day"] <= 10
        assert config.CAPS["push_actions_per_game_per_4h"] == 1

    def test_all_games_configured(self):
        for game, cfg in config.GAMES.items():
            assert cfg["project"] and cfg["ga_property"]
            assert "level_backend" in cfg

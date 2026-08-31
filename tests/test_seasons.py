from datetime import date

from footy_scraper.seasons import default_seasons, parse_seasons, season_label


def test_season_label():
    assert season_label(2024) == "2024-25"
    assert season_label(2026) == "2026-27"


def test_current_season_after_july():
    seasons = default_seasons(count=10, today=date(2026, 8, 30))
    assert len(seasons) == 10
    assert seasons[0] == "2026-27"
    assert seasons[-1] == "2017-18"


def test_current_season_before_july():
    seasons = default_seasons(count=3, today=date(2026, 3, 1))
    assert seasons == ["2025-26", "2024-25", "2023-24"]


def test_parse_explicit_list():
    assert parse_seasons("2024-25,2023-24") == ["2024-25", "2023-24"]


def test_parse_last_n():
    assert parse_seasons("last:3", today=date(2026, 8, 30)) == ["2026-27", "2025-26", "2024-25"]


def test_parse_none_defaults():
    assert parse_seasons(None, count=2, today=date(2026, 8, 30)) == ["2026-27", "2025-26"]

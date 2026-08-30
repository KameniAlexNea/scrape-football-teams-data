from footy_scraper.models import LeagueData, Player, Standing


def test_player_position_is_free_text_as_seen_on_site():
    # The agent writes positions exactly as the site shows them — no mapping.
    assert Player(name="X", position="Centre-Back").position == "Centre-Back"
    assert Player(name="X", position="RB, CB, LB").position == "RB, CB, LB"
    assert Player(name="X").position is None


def test_player_extra_fields():
    p = Player(name="X", position="RW, AM", height_cm=178, market_value="€94.3M", injury="Ankle injury")
    assert p.height_cm == 178
    assert p.market_value == "€94.3M"
    assert p.injury == "Ankle injury"
    assert p.position == "RW, AM"


def test_player_age_coerced_from_string():
    p = Player(name="X", age="25")
    assert p.age == 25


def test_player_extra_preserved():
    p = Player(name="X", extra={"captain": True, "height_cm": 182})
    assert p.extra["captain"] is True


def test_standing_parses():
    s = Standing.model_validate(
        {"club": "Arsenal", "position": 2, "played": 38, "points": 78, "goal_difference": 43}
    )
    assert s.goal_difference == 43


def test_league_roundtrip_dump():
    data = LeagueData(league="Premier League", url="https://x", meta={"note": "hi"})
    data.ensure_season("2024-25").standings.append(
        Standing(club="Arsenal", position=2)
    )
    dumped = data.model_dump(mode="json")
    reloaded = LeagueData.model_validate(dumped)
    assert reloaded.seasons["2024-25"].standings[0].club == "Arsenal"
    assert reloaded.meta["note"] == "hi"

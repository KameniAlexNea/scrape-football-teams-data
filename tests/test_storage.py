from footy_scraper.storage import LeagueStore


def _store(tmp_path):
    return LeagueStore(tmp_path / "pl.json", "Premier League")


def test_upsert_standings_squad_manager_matches_and_reload(tmp_path):
    store = _store(tmp_path)
    store.apply_payload(
        {"season": "2024-25", "standings": [{"club": "Arsenal", "position": 2, "points": 78}]}
    )
    store.apply_payload(
        {
            "season": "2024-25",
            "club": "Arsenal",
            "source": "https://premierleague.com/en/clubs",
            "squad": [{"name": "Saka", "position": "Right Winger", "shirt_number": 7, "age": 23}],
            "manager": {"name": "Arteta", "nationality": "Spain"},
            "final_position": 2,
        }
    )
    store.apply_payload(
        {"season": "2024-25", "matches": [{"home_team": "Arsenal", "away_team": "Wolves", "home_score": 2, "away_score": 0}]}
    )
    # Duplicate the same match — must dedupe, not append.
    store.apply_payload(
        {"season": "2024-25", "matches": [{"home_team": "Arsenal", "away_team": "Wolves", "home_score": 2, "away_score": 0}]}
    )

    # Reload from disk to prove persistence.
    store2 = LeagueStore(tmp_path / "pl.json", "Premier League")
    sd = store2.season("2024-25")
    assert len(sd.matches) == 1
    assert sd.matches[0].home_score == 2
    assert sd.clubs["Arsenal"].squad[0].name == "Saka"
    assert sd.clubs["Arsenal"].squad[0].position == "Right Winger"
    assert sd.clubs["Arsenal"].manager.name == "Arteta"
    assert sd.clubs["Arsenal"].final_position == 2
    assert sd.standings[0].club == "Arsenal"


def test_payload_without_club_only_updates_standings(tmp_path):
    store = _store(tmp_path)
    rep = store.apply_payload({"season": "2024-25", "standings": [{"club": "Chelsea", "position": 6}]})
    assert rep["applied"] is True
    assert not rep["errors"]
    assert "Chelsea" in store.season("2024-25").standings[0].club


def test_malformed_fragment_is_recorded_not_raised(tmp_path):
    store = _store(tmp_path)
    rep = store.apply_payload(
        {"season": "2024-25", "standings": [{"club": 123, "position": "not-an-int"}]}
    )
    assert rep["errors"], "expected validation errors to be recorded"
    assert store.season("2024-25").standings == []


def test_missing_season_rejected(tmp_path):
    store = _store(tmp_path)
    rep = store.apply_payload({"club": "Arsenal"})
    assert rep["applied"] is False


def test_squad_merge_by_player_name(tmp_path):
    store = _store(tmp_path)
    store.apply_payload({"season": "2024-25", "club": "Arsenal", "squad": [{"name": "Saka", "shirt_number": 7}]})
    store.apply_payload({"season": "2024-25", "club": "Arsenal", "squad": [{"name": "Saka", "age": 23}, {"name": "Odegaard"}]})
    squad = store.season("2024-25").clubs["Arsenal"].squad
    assert len(squad) == 2
    by_name = {p.name: p for p in squad}
    assert by_name["Saka"].age == 23  # updated, not duplicated
    assert by_name["Saka"].shirt_number == 7  # previous fields preserved

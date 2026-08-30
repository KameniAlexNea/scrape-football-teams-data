from footy_scraper.models import Manager, Match, Player, Standing
from footy_scraper.storage import LeagueStore


def _store(tmp_path):
    return LeagueStore(tmp_path / "pl.json", "Premier League")


def test_save_standings_squad_matches_and_reload(tmp_path):
    store = _store(tmp_path)
    store.save_standings("2024-25", [Standing(club="Arsenal", position=2, points=78)])
    store.save_squad(
        "2024-25",
        "Arsenal",
        squad=[Player(name="Saka", position="Right Winger", shirt_number=7, age=23)],
        manager=Manager(name="Arteta", nationality="Spain"),
        final_position=2,
        sources=["https://premierleague.com/en/clubs"],
    )
    store.save_matches(
        "2024-25",
        [Match(home_team="Arsenal", away_team="Wolves", home_score=2, away_score=0)],
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


def test_standings_already_saved(tmp_path):
    store = _store(tmp_path)
    rows = [Standing(club="Arsenal", position=2, points=78)]
    first = store.save_standings("2024-25", rows)
    second = store.save_standings("2024-25", rows)
    assert first["already_saved"] is False
    assert first["added"] == 1
    assert second["already_saved"] is True
    assert second["added"] == 0 and second["updated"] == 0


def test_standings_updated_when_row_changes(tmp_path):
    store = _store(tmp_path)
    store.save_standings("2024-25", [Standing(club="Arsenal", position=2, points=78)])
    changed = store.save_standings("2024-25", [Standing(club="Arsenal", position=2, points=80)])
    assert changed["updated"] == 1
    assert changed["already_saved"] is False
    assert store.season("2024-25").standings[0].points == 80


def test_matches_dedupe_and_already_saved(tmp_path):
    store = _store(tmp_path)
    matches = [Match(home_team="Arsenal", away_team="Wolves", home_score=2, away_score=0)]
    store.save_matches("2024-25", matches)
    second = store.save_matches("2024-25", matches)
    assert second["already_saved"] is True
    assert len(store.season("2024-25").matches) == 1


def test_squad_merge_by_player_name(tmp_path):
    store = _store(tmp_path)
    store.save_squad("2024-25", "Arsenal", squad=[Player(name="Saka", shirt_number=7)])
    store.save_squad(
        "2024-25",
        "Arsenal",
        squad=[Player(name="Saka", age=23), Player(name="Odegaard")],
    )
    squad = store.season("2024-25").clubs["Arsenal"].squad
    assert len(squad) == 2
    by_name = {p.name: p for p in squad}
    assert by_name["Saka"].age == 23  # updated, not duplicated
    assert by_name["Saka"].shirt_number == 7  # previous fields preserved


def test_squad_already_saved(tmp_path):
    store = _store(tmp_path)
    squad = [Player(name="Saka", shirt_number=7, age=23)]
    store.save_squad("2024-25", "Arsenal", squad=squad, manager=Manager(name="Arteta"))
    second = store.save_squad("2024-25", "Arsenal", squad=squad, manager=Manager(name="Arteta"))
    assert second["already_saved"] is True


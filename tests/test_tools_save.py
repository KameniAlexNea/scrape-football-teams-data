import json

from footy_scraper.storage import LeagueStore
from footy_scraper.tools import ToolExecutor


def _executor(tmp_path) -> ToolExecutor:
    store = LeagueStore(tmp_path / "pl.json", "Premier League")
    return ToolExecutor(browser=None, store=store)  # type: ignore[arg-type]


async def test_save_standing_saves_and_reports_already_saved(tmp_path):
    ex = _executor(tmp_path)
    payload = {"season": "2024-25", "standings": [{"club": "Arsenal", "position": 2, "points": 78}]}
    first = json.loads(await ex.run("save_standing", payload))
    second = json.loads(await ex.run("save_standing", payload))
    assert first["status"] == "saved"
    assert "saved" in first["message"]
    assert second["status"] == "already_saved"
    assert "already saved" in second["message"]


async def test_save_squad_saves_and_reports_already_saved(tmp_path):
    ex = _executor(tmp_path)
    payload = {
        "season": "2024-25",
        "club": "Arsenal",
        "squad": [{"name": "Saka", "position": "RW"}],
        "manager": {"name": "Arteta"},
    }
    first = json.loads(await ex.run("save_squad", payload))
    second = json.loads(await ex.run("save_squad", payload))
    assert first["status"] == "saved"
    assert second["status"] == "already_saved"


async def test_save_match_saves_and_reports_already_saved(tmp_path):
    ex = _executor(tmp_path)
    payload = {
        "season": "2024-25",
        "matches": [{"home_team": "Arsenal", "away_team": "Wolves", "home_score": 2}],
    }
    first = json.loads(await ex.run("save_match", payload))
    second = json.loads(await ex.run("save_match", payload))
    assert first["status"] == "saved"
    assert second["status"] == "already_saved"


async def test_save_standing_validation_error(tmp_path):
    ex = _executor(tmp_path)
    out = json.loads(await ex.run("save_standing", {"season": "2024-25", "standings": [{"club": 123}]}))
    assert "error" in out


async def test_missing_required_field_returns_error(tmp_path):
    ex = _executor(tmp_path)
    out = json.loads(await ex.run("save_squad", {"season": "2024-25"}))
    assert "error" in out

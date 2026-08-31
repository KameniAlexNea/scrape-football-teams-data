"""Validating, incremental JSON storage for one league.

Design goals:
  * *Iterative* — every ``save_*`` call persists to disk, so interrupted runs
    keep their progress.
  * *Idempotent* — re-saving identical data is detected and reported as
    ``already_saved``; changed data is merged instead of duplicated.
  * *Atomic* — writes go to a temp file then ``os.replace``, so the JSON file
    is never left half-written.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from footy_scraper.models import (
    ClubSeason,
    LeagueData,
    Manager,
    Match,
    Player,
    SeasonData,
    Standing,
)
from loguru import logger


class LeagueStore:
    def __init__(self, path: Path, league: str, url: str | None = None):
        self.path = Path(path)
        self._data = LeagueData(league=league, url=url, scraped_at=_utcnow())
        if self.path.exists():
            self._load()

    # ------------------------------------------------------------------ I/O
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = LeagueData.model_validate(raw)
            logger.info(
                "Loaded existing store from {} ({} seasons)", self.path, len(self._data.seasons)
            )
        except Exception:
            logger.exception("Failed to parse existing store {}; starting fresh", self.path)

    def save(self) -> Path:
        """Atomically persist the current state to disk."""
        self._data.scraped_at = _utcnow()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, prefix=".store-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data.model_dump(mode="json"), fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        logger.debug("Saved store to {}", self.path)
        return self.path

    # ------------------------------------------------------------- accessors
    def season(self, season: str) -> "SeasonData":
        return self._data.ensure_season(season)

    def export(self) -> dict[str, Any]:
        return self._data.model_dump(mode="json")

    # --------------------------------------------------------------- upserts
    def save_standings(self, season: str, standings: list[Standing]) -> dict[str, Any]:
        """Upsert a season's final table; returns an idempotency-aware status."""
        sd = self.season(season)
        by_club = {s.club: s for s in sd.standings}
        added = updated = unchanged = 0
        for row in standings:
            existing = by_club.get(row.club)
            if existing is None:
                by_club[row.club] = row
                added += 1
            elif existing.model_dump() == row.model_dump():
                unchanged += 1
            else:
                by_club[row.club] = row
                updated += 1
        sd.standings = [by_club[k] for k in by_club]
        self.save()
        return {
            "rows": len(standings),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "already_saved": added == 0 and updated == 0 and len(standings) > 0,
        }

    def save_matches(self, season: str, matches: list[Match]) -> dict[str, Any]:
        """Upsert match results; returns an idempotency-aware status.

        Matches are keyed by (date, home_team, away_team).
        """
        sd = self.season(season)
        keyed: dict[tuple[Any, ...], Match] = {}
        for m in sd.matches:
            keyed[_match_key(m)] = m
        added = updated = unchanged = 0
        for m in matches:
            key = _match_key(m)
            existing = keyed.get(key)
            if existing is None:
                keyed[key] = m
                added += 1
            elif existing.model_dump() == m.model_dump():
                unchanged += 1
            else:
                keyed[key] = m
                updated += 1
        sd.matches = list(keyed.values())
        self.save()
        return {
            "rows": len(matches),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "already_saved": added == 0 and updated == 0 and len(matches) > 0,
        }

    def save_squad(
        self,
        season: str,
        club: str,
        *,
        squad: list[Player] | None = None,
        manager: Manager | None = None,
        final_position: int | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Upsert one club's squad/manager for a season; idempotency-aware.

        Players merge field-by-field by name (iterative accumulation).
        """
        sd = self.season(season)
        cs = sd.clubs.get(club) or ClubSeason(club=club, season=season)
        cs.club = club
        cs.season = season
        added = updated = unchanged = 0
        if squad is not None:
            by_name = {p.name: p for p in cs.squad}
            for p in squad:
                existing = by_name.get(p.name)
                if existing is None:
                    by_name[p.name] = p
                    added += 1
                elif existing.model_dump() == p.model_dump():
                    unchanged += 1
                else:
                    by_name[p.name] = _merge_player(existing, p)
                    updated += 1
            cs.squad = [by_name[k] for k in by_name]
        manager_changed = manager is not None and cs.manager != manager
        if manager is not None:
            cs.manager = manager
        final_pos_changed = final_position is not None and cs.final_position != final_position
        if final_position is not None:
            cs.final_position = final_position
        if sources:
            for src in sources:
                if src and src not in cs.sources:
                    cs.sources.append(src)
        sd.clubs[club] = cs
        self.save()
        provided = squad is not None or manager is not None or final_position is not None
        return {
            "players": len(cs.squad),
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "manager_saved": manager_changed,
            "final_position": cs.final_position,
            "already_saved": provided
            and added == 0
            and updated == 0
            and not manager_changed
            and not final_pos_changed,
        }


def _match_key(m: Match) -> tuple[Any, ...]:
    return (m.date, m.home_team, m.away_team)


def _merge_player(existing: Player, new: Player) -> Player:
    """Field-level merge: new non-None values win, everything else is kept.

    Enables iterative accumulation — e.g. a first save with ``shirt_number``
    followed by a second save with ``age`` keeps both.
    """
    merged = existing.model_copy()
    for field_name in Player.model_fields:
        value = getattr(new, field_name)
        if value is not None:
            setattr(merged, field_name, value)
    merged.extra = {**existing.extra, **new.extra}
    return merged


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

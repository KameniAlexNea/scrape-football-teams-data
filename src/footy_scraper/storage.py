"""Validating, incremental JSON storage for one league.

Design goals:
  * *Iterative* — the agent calls ``apply_payload`` after every extraction and
    each call persists to disk, so interrupted runs keep their progress.
  * *Merging* — data is upserted by (season, club); re-saving the same entity
    updates it instead of duplicating it.
  * *Best effort* — a malformed fragment is recorded as an error and skipped,
    never crashing the run.
  * *Atomic* — writes go to a temp file then ``os.replace``, so the JSON file
    is never left half-written.
"""



import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
            logger.info("Loaded existing store from {} ({} seasons)", self.path, len(self._data.seasons))
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
    def upsert_standings(self, season: str, standings: list[Standing]) -> int:
        sd = self.season(season)
        by_club = {s.club: s for s in sd.standings}
        by_club.update({s.club: s for s in standings})
        sd.standings = [by_club[k] for k in by_club]
        return len(standings)

    def upsert_matches(self, season: str, matches: list[Match]) -> tuple[int, int]:
        """Return (added, updated) counts. Matches are keyed by (date, home, away)."""
        sd = self.season(season)
        keyed: dict[tuple[Any, ...], Match] = {}
        for m in sd.matches:
            keyed[_match_key(m)] = m
        added = updated = 0
        for m in matches:
            key = _match_key(m)
            if key in keyed:
                keyed[key] = m
                updated += 1
            else:
                keyed[key] = m
                added += 1
        sd.matches = list(keyed.values())
        return added, updated

    def upsert_club_season(
        self,
        season: str,
        club: str,
        *,
        squad: list[Player] | None = None,
        manager: Manager | None = None,
        final_position: int | None = None,
        sources: list[str] | None = None,
    ) -> int:
        sd = self.season(season)
        cs = sd.clubs.get(club) or ClubSeason(club=club, season=season)
        cs.club = club
        cs.season = season
        if squad is not None:
            by_name = {p.name: p for p in cs.squad}
            for p in squad:
                existing = by_name.get(p.name)
                by_name[p.name] = _merge_player(existing, p) if existing is not None else p
            cs.squad = [by_name[k] for k in by_name]
        if manager is not None:
            cs.manager = manager
        if final_position is not None:
            cs.final_position = final_position
        if sources:
            for src in sources:
                if src and src not in cs.sources:
                    cs.sources.append(src)
        sd.clubs[club] = cs
        return 1

    # -------------------------------------------------------------- payloads
    def apply_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one agent-supplied fragment (see prompts.py for the shape).

        Returns a report dict; errors are recorded but never raised.
        """
        season = payload.get("season")
        if not season or not str(season).strip():
            return {"applied": False, "error": "payload missing a non-empty 'season'"}

        report: dict[str, Any] = {"season": str(season), "applied": True, "warnings": [], "errors": []}

        if "standings" in payload:
            try:
                standings = [Standing.model_validate(s) for s in payload["standings"]]
            except ValidationError as exc:
                report["errors"].append(f"standings invalid: {_short_exc(exc)}")
            else:
                report["standings_updated"] = self.upsert_standings(str(season), standings)

        if "matches" in payload:
            try:
                matches = [Match.model_validate(m) for m in payload["matches"]]
            except ValidationError as exc:
                report["errors"].append(f"matches invalid: {_short_exc(exc)}")
            else:
                added, updated = self.upsert_matches(str(season), matches)
                report["matches_added"], report["matches_updated"] = added, updated

        club = payload.get("club")
        if club is not None:
            if not isinstance(club, str) or not club.strip():
                report["errors"].append("'club' must be a non-empty string")
            else:
                club = club.strip()
                squad: list[Player] | None = None
                manager: Manager | None = None
                final_position: int | None = None
                sources = _as_list(payload.get("source"))

                if "squad" in payload:
                    try:
                        squad = [Player.model_validate(p) for p in payload["squad"]]
                    except ValidationError as exc:
                        report["errors"].append(f"squad for {club} invalid: {_short_exc(exc)}")

                if "manager" in payload and payload["manager"] is not None:
                    try:
                        manager = Manager.model_validate(payload["manager"])
                    except ValidationError as exc:
                        report["errors"].append(f"manager for {club} invalid: {_short_exc(exc)}")

                if payload.get("final_position") is not None:
                    try:
                        final_position = int(payload["final_position"])
                    except (TypeError, ValueError):
                        report["errors"].append(f"final_position for {club} not an int")

                try:
                    self.upsert_club_season(
                        str(season), club, squad=squad, manager=manager,
                        final_position=final_position, sources=sources,
                    )
                except ValidationError as exc:
                    report["errors"].append(f"club {club} rejected: {_short_exc(exc)}")

        if payload.get("source") and club is None:
            # Nothing to attach a source to — just note it.
            report["warnings"].append("source given without a club; ignored")

        self.save()
        return report


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


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _short_exc(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

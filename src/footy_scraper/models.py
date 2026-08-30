"""Pydantic data models for the scraped football data.

Hierarchy::

    LeagueData
      └── seasons: dict[str, SeasonData]            # keyed by "2024-25"
            ├── standings: list[Standing]           # final league table
            ├── matches: list[Match]                # results with scores
            └── clubs: dict[str, ClubSeason]        # keyed by club name
                  ├── squad: list[Player]
                  ├── manager: Manager | None
                  └── final_position: int | None

Fields such as ``position`` are kept as free text exactly as the agent saw them
on the site — no normalisation or hardcoded mapping is applied. Every model
carries an ``extra`` dict so site-specific fields are never lost.
"""


from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Player(BaseModel):
    name: str
    shirt_number: int | None = None
    position: str | None = None
    age: int | None = None
    nationality: str | None = None
    height_cm: int | None = None
    joined: str | None = None
    contract_until: str | None = None
    market_value: str | None = None
    injury: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Manager(BaseModel):
    name: str
    nationality: str | None = None
    appointed: str | None = None
    age: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Standing(BaseModel):
    """One row of the end-of-season league table."""

    club: str
    position: int
    played: int | None = None
    won: int | None = None
    drawn: int | None = None
    lost: int | None = None
    goals_for: int | None = None
    goals_against: int | None = None
    goal_difference: int | None = None
    points: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Match(BaseModel):
    """A single fixture/result."""

    date: str | None = None
    home_team: str
    away_team: str
    home_score: int | None = None
    away_score: int | None = None
    round: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ClubSeason(BaseModel):
    """Everything known about one club in one season."""

    club: str
    season: str
    manager: Manager | None = None
    squad: list[Player] = Field(default_factory=list)
    final_position: int | None = None
    sources: list[str] = Field(default_factory=list)


class SeasonData(BaseModel):
    """All data captured for a single season of a league."""

    season: str
    standings: list[Standing] = Field(default_factory=list)
    matches: list[Match] = Field(default_factory=list)
    clubs: dict[str, ClubSeason] = Field(default_factory=dict)


class LeagueData(BaseModel):
    """Top-level document persisted to one JSON file per league."""

    league: str
    url: str | None = None
    scraped_at: datetime | None = None
    seasons: dict[str, SeasonData] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    def ensure_season(self, season: str) -> SeasonData:
        return self.seasons.setdefault(season, SeasonData(season=season))

"""Season label helpers.

Seasons are labelled like "2024-25" (the football convention for the
2024/2025 season). The "current" season is the one that started in the most
recent autumn, i.e. a run started in July or later belongs to the season that
began that same year.
"""



from datetime import date


def season_label(start_year: int) -> str:
    """Return the short label for a season starting in ``start_year``.

    >>> season_label(2024)
    '2024-25'
    """
    return f"{start_year}-{str(start_year + 1)[2:]}"


def current_season_start(today: date | None = None) -> int:
    """Year in which the season current at ``today`` began."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def default_seasons(count: int = 10, today: date | None = None) -> list[str]:
    """Last ``count`` seasons (most recent first), including the current one."""
    start = current_season_start(today)
    return [season_label(start - i) for i in range(count)]


def parse_seasons(value: str | None, count: int = 10, today: date | None = None) -> list[str]:
    """Parse the ``--seasons`` CLI value.

    Accepted forms:
      - ``None``          -> last ``count`` seasons
      - ``"last:N"``      -> last N seasons
      - ``"2024-25,2023-24"`` -> that explicit list (order preserved)
    """
    if not value:
        return default_seasons(count, today)
    value = value.strip()
    if value.lower().startswith("last:"):
        n = int(value.split(":", 1)[1].strip())
        return default_seasons(n, today)
    return [part.strip() for part in value.split(",") if part.strip()]

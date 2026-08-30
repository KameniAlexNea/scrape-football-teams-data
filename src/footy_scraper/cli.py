"""Command-line interface: ``footy-scrape <league-url>``.

A single default command keeps the primary usage natural::

    footy-scrape <league-url> [options]

and ``--install-browsers`` is a setup flag on the same command.
"""


import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import typer
from loguru import logger

from footy_scraper.agent import AgentResult, ScrapeAgent
from footy_scraper.browser import BrowserSession
from footy_scraper.config import Settings
from footy_scraper.prompts import build_mission, build_system_prompt
from footy_scraper.seasons import parse_seasons
from footy_scraper.storage import LeagueStore
from footy_scraper.tools import ToolExecutor

app = typer.Typer(
    name="footy-scrape",
    help="Agentic football data scraper (squads, managers, standings, results) via claude-agent-sdk "
    "(Claude Code) + Playwright. You give a league name and a link; the agent browses the site "
    "like a human and saves the data to JSON.",
    no_args_is_help=True,
)


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")


def _derive_league(url: str) -> str:
    """Best-effort league name from the URL when ``--league`` is not given."""
    host = urlparse(url).netloc or url
    host = host.removeprefix("www.").split(".")[0]
    if host and host != "fotmob":
        return host.title() or "unknown"
    path = [s for s in urlparse(url).path.split("/") if s]
    return (path[-1].replace("-", " ").title() if path else host) or "unknown"


@app.command()
def run(
    url: str = typer.Argument(
        ...,
        help="Link to the league page the agent will explore, e.g. "
        "https://www.fotmob.com/leagues/47/overview/premier-league",
    ),
    install_browsers: bool = typer.Option(
        False,
        "--install-browsers",
        help="Install the Playwright Chromium browser and exit.",
    ),
    league: str = typer.Option(None, "--league", help="League name used in the JSON and mission prompt (e.g. \"Premier League\"). Default: derived from the URL."),
    seasons: str = typer.Option(None, "--seasons", help='Comma-separated seasons, e.g. "2024-25,2023-24", or last:N. Default: last:10.'),
    output: Path = typer.Option(None, "--output", "-o", help="Output JSON file path. Default: <FOOTY_OUTPUT_DIR>/<league>.json."),
    headful: bool = typer.Option(False, "--headful", help="Show the browser window (default is headless)."),
    model: str = typer.Option(None, "--model", help="Claude model name. Default: FOOTY_MODEL env."),
    max_steps: int = typer.Option(100, "--max-steps", help="Cap on agent tool-call steps."),
    timeout_ms: int = typer.Option(None, "--timeout-ms", help="Browser navigation timeout in ms."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Scrape a league's squads, managers, standings and results into a JSON file."""
    if install_browsers:
        _install_browsers()
        raise typer.Exit(code=0)

    _setup_logging(verbose)

    league_name = league or _derive_league(url)

    settings = Settings()
    if not settings.has_api_key:
        typer.echo(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "Copy .env.example to .env and add your key, or export ANTHROPIC_API_KEY.",
            err=True,
        )
        raise typer.Exit(code=1)

    season_list = parse_seasons(seasons)
    out_path = output or (settings.output_dir / f"{_slug(league_name)}.json")
    eff_model = settings.resolve_model(model)
    eff_timeout = timeout_ms or settings.timeout_ms

    typer.echo(f"League      : {league_name}")
    typer.echo(f"URL         : {url}")
    typer.echo(f"Seasons     : {', '.join(season_list)}")
    typer.echo(f"Model       : {eff_model}")
    typer.echo(f"Output      : {out_path}")

    result = asyncio.run(
        _scrape(
            url=url,
            league=league_name,
            seasons=season_list,
            out_path=out_path,
            headless=not headful,
            model=eff_model,
            max_steps=max_steps,
            timeout_ms=eff_timeout,
            snapshot_max_chars=settings.snapshot_max_chars,
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
        )
    )

    typer.echo(f"\nFinished: {result.steps} steps, {result.tool_calls} tool calls (stop: {result.stop_reason})")
    if result.total_cost_usd is not None:
        typer.echo(f"Cost      : ${result.total_cost_usd:.4f}")
    typer.echo(f"Data saved to {out_path}")


def _install_browsers() -> None:
    """Install the Playwright Chromium browser (required before the first run)."""
    typer.echo("Installing Playwright Chromium...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    typer.echo("Done.")


async def _scrape(
    *,
    url: str,
    league: str,
    seasons: list[str],
    out_path: Path,
    headless: bool,
    model: str,
    max_steps: int,
    timeout_ms: int,
    snapshot_max_chars: int,
    api_key: str,
    base_url: str | None,
) -> AgentResult:
    store = LeagueStore(out_path, league=league, url=url)

    async with BrowserSession(
        headless=headless,
        timeout_ms=timeout_ms,
        screenshot_dir=Path(out_path).parent / "screenshots",
    ) as browser:
        nav = await browser.goto(url)
        logger.info("Opened {} -> {}", url, nav["title"])

        executor = ToolExecutor(browser, store, snapshot_max_chars=snapshot_max_chars)
        system_prompt = build_system_prompt(output_path=out_path)
        mission = build_mission(league=league, url=url, seasons=seasons)

        agent = ScrapeAgent(
            model=model,
            system_prompt=system_prompt,
            executor=executor,
            max_steps=max_steps,
            api_key=api_key,
            base_url=base_url,
        )
        return await agent.run(mission=mission)


def _setup_logging(verbose: bool) -> None:
    """Configure loguru for clear, end-user-friendly progress output."""
    logger.remove()  # drop the default stderr handler; add our own
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        colorize=None,  # auto-detect TTY so piped output stays clean
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}:{line}</cyan> - <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )
    # Keep noisy third-party stdlib loggers quiet unless verbose.
    for noisy in ("playwright", "claude_agent_sdk", "anthropic", "httpx", "httpcore", "openai", "uvicorn", "mcp"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


if __name__ == "__main__":
    app()

"""Agent tool definitions and the executor that maps tool calls to actions.

Each tool is declared as a ``ToolSpec`` (name / description / JSON schema) and
implemented as a ``_do_*`` method on :class:`ToolExecutor`. The executor exposes
the tools to the agent as an **in-process MCP server** (``build_mcp_tools``),
so ``claude-agent-sdk``'s Claude Code subprocess can call them without any IPC.
Every implementation returns a JSON string fed back to the model as the tool
result.
"""



import json
from dataclasses import dataclass
from typing import Any, Callable

from claude_agent_sdk import SdkMcpTool
from claude_agent_sdk import tool as sdk_tool
from loguru import logger
from pydantic import ValidationError

from footy_scraper.browser import BrowserSession
from footy_scraper.models import Manager, Match, Player, Standing
from footy_scraper.storage import LeagueStore


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _short_validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
    )


def _sources(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


BROWSE_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "navigate",
        "Open a URL in the current browser tab. Use this for any navigation, including club links found in page_snapshot.",
        _obj({"url": {"type": "string", "description": "Absolute URL to open"}}, ["url"]),
    ),
    ToolSpec(
        "page_snapshot",
        "Return the current page's visible text, its title, and a list of clickable links (text + href). This is your primary way to understand a page.",
        _obj({"focus": {"type": "string", "description": "Optional hint for what you are looking for (not used by the browser)"}}),
    ),
    ToolSpec(
        "click",
        "Click an element by CSS selector or Playwright locator (e.g. 'button:has-text(\"Squad\")', '.nav a'). Prefer click_text when you know the visible label.",
        _obj({"selector": {"type": "string", "description": "CSS selector / locator"}}, ["selector"]),
    ),
    ToolSpec(
        "click_text",
        "Click the first visible element containing the given text. Great for tabs like 'Squad', 'Table', 'Results', 'Accept'.",
        _obj({"text": {"type": "string", "description": "Text to find and click"}}, ["text"]),
    ),
    ToolSpec(
        "select_option",
        "Select an option in a dropdown/select (e.g. a season selector).",
        _obj({"selector": {"type": "string", "description": "CSS selector of the select element"}, "value": {"type": "string", "description": "Option value or label to select"}}, ["selector", "value"]),
    ),
    ToolSpec(
        "fill",
        "Type text into an input field (e.g. search box).",
        _obj({"selector": {"type": "string", "description": "CSS selector of the input"}, "value": {"type": "string", "description": "Text to type"}}, ["selector", "value"]),
    ),
    ToolSpec(
        "scroll",
        "Scroll the page: 'top', 'bottom', 'up', or 'down'.",
        _obj({"direction": {"type": "string", "enum": ["top", "bottom", "up", "down"]}}, ["direction"]),
    ),
    ToolSpec(
        "wait",
        "Pause for the given number of milliseconds, letting lazy-loaded content appear. Use when a page looks empty or links are missing, then call page_snapshot again.",
        _obj({"ms": {"type": "integer", "description": "Milliseconds to wait (max 10000)"}}, ["ms"]),
    ),
    ToolSpec(
        "screenshot",
        "Save a screenshot of the current page to disk (useful for debugging). Returns the file path.",
        _obj({"name": {"type": "string", "description": "Short filename stem, e.g. 'arsenal-squad-2024'"}}, ["name"]),
    ),
    ToolSpec(
        "save_standing",
        "Save a season's final league table (standings): one row per club with its final position. "
        "Call once per season. Re-saving identical data returns 'already saved'.",
        _obj(
            {
                "season": {"type": "string", "description": "Season label like '2024-25'"},
                "standings": {"type": "array", "description": "End-of-season table rows: club (required), position (required), played, won, drawn, lost, goals_for, goals_against, goal_difference, points"},
                "source": {"type": "string", "description": "URL you extracted this from (recommended)"},
            },
            ["season", "standings"],
        ),
    ),
    ToolSpec(
        "save_squad",
        "Save a club's squad (and optionally its manager and final position) for a season. "
        "Call once per club per season. Re-saving identical data returns 'already saved'.",
        _obj(
            {
                "season": {"type": "string", "description": "Season label like '2024-25'"},
                "club": {"type": "string", "description": "Club name exactly as the site shows it"},
                "squad": {"type": "array", "description": "Players: name (required), shirt_number, position, age, nationality, height_cm, joined, contract_until, market_value, injury"},
                "manager": {"type": "object", "description": "Manager/head coach: name (required), nationality, appointed, age"},
                "final_position": {"type": "integer", "description": "Club's final league position for this season"},
                "source": {"type": "string", "description": "URL you extracted this from (recommended)"},
            },
            ["season", "club"],
        ),
    ),
    ToolSpec(
        "save_match",
        "Save match results with scores for a season. Call per page/section of results. "
        "Re-saving identical matches returns 'already saved'.",
        _obj(
            {
                "season": {"type": "string", "description": "Season label like '2024-25'"},
                "matches": {"type": "array", "description": "Matches: home_team (required), away_team (required), home_score, away_score, date, round"},
                "source": {"type": "string", "description": "URL you extracted this from (recommended)"},
            },
            ["season", "matches"],
        ),
    ),
]

_TOOL_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in BROWSE_TOOLS}


class ToolExecutor:
    """Runs tool calls against the live browser and store."""

    def __init__(self, browser: BrowserSession, store: LeagueStore, snapshot_max_chars: int = 30_000):
        self._browser = browser
        self._store = store
        self._snapshot_max_chars = snapshot_max_chars

    def build_mcp_tools(self) -> list[SdkMcpTool]:
        """Expose ``BROWSE_TOOLS`` as ``claude-agent-sdk`` in-process MCP tools.

        Each tool's ``input_schema`` (already a JSON Schema dict) is passed
        straight to ``claude_agent_sdk.tool``; the async handler funnels every
        call into :meth:`run`, which dispatches to the matching ``_do_*``
        implementation and returns the JSON string as MCP text content.
        """
        return [
            sdk_tool(spec.name, spec.description, spec.input_schema)(self._mcp_handler(spec.name))
            for spec in BROWSE_TOOLS
        ]

    def _mcp_handler(self, name: str) -> Callable[[dict[str, Any]], Any]:
        async def handle(args: dict[str, Any] | None) -> dict[str, Any]:
            result = await self.run(name, args or {})
            return {"content": [{"type": "text", "text": result}]}

        return handle

    async def run(self, name: str, args: dict[str, Any] | None) -> str:
        handler: Callable[[dict[str, Any]], Any] | None = getattr(self, f"_do_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name!r}"})
        try:
            result = await handler(args or {})
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the model
            logger.warning("Tool {} failed: {}", name, exc)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------ handlers
    async def _do_navigate(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url", "")
        if not url.startswith(("http://", "https://")):
            return {"error": f"invalid URL: {url!r}"}
        return await self._browser.goto(url)

    async def _do_page_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.snapshot(max_chars=self._snapshot_max_chars)

    async def _do_click(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.click(args.get("selector", ""))

    async def _do_click_text(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.click_text(args.get("text", ""))

    async def _do_select_option(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.select_option(args.get("selector", ""), args.get("value", ""))

    async def _do_fill(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.fill(args.get("selector", ""), args.get("value", ""))

    async def _do_scroll(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.scroll(args.get("direction", "down"))

    async def _do_wait(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.wait(args.get("ms", 1000))

    async def _do_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._browser.screenshot(args.get("name", "page"))

    async def _do_save_standing(self, args: dict[str, Any]) -> dict[str, Any]:
        season = str(args.get("season", "")).strip()
        raw = args.get("standings")
        if not season:
            return {"error": "missing 'season'"}
        if not isinstance(raw, list) or not raw:
            return {"error": "missing 'standings' array (list of table rows)"}
        try:
            standings = [Standing.model_validate(row) for row in raw]
        except ValidationError as exc:
            return {"error": f"standings invalid: {_short_validation(exc)}"}
        res = self._store.save_standings(season, standings)
        if res["already_saved"]:
            msg = f"already saved: {season} standings ({res['rows']} rows)"
        else:
            msg = (
                f"saved: {season} standings ({res['rows']} rows; "
                f"+{res['added']} added, {res['updated']} updated)"
            )
        logger.info("🗒 {}", msg)
        logger.log("TRACE", "save_standing payload: {}", json.dumps(args, ensure_ascii=False, default=str))
        return {"status": "already_saved" if res["already_saved"] else "saved", "message": msg, **res}

    async def _do_save_squad(self, args: dict[str, Any]) -> dict[str, Any]:
        season = str(args.get("season", "")).strip()
        club = args.get("club")
        if not season:
            return {"error": "missing 'season'"}
        if not club or not str(club).strip():
            return {"error": "missing 'club'"}
        club = str(club).strip()
        squad: list[Player] | None = None
        if "squad" in args:
            try:
                squad = [Player.model_validate(p) for p in args["squad"]]
            except ValidationError as exc:
                return {"error": f"squad invalid: {_short_validation(exc)}"}
        manager: Manager | None = None
        if args.get("manager") is not None:
            try:
                manager = Manager.model_validate(args["manager"])
            except ValidationError as exc:
                return {"error": f"manager invalid: {_short_validation(exc)}"}
        final_position: int | None = None
        if args.get("final_position") is not None:
            try:
                final_position = int(args["final_position"])
            except (TypeError, ValueError):
                return {"error": "final_position must be an integer"}
        res = self._store.save_squad(
            season,
            club,
            squad=squad,
            manager=manager,
            final_position=final_position,
            sources=_sources(args.get("source")),
        )
        if res["already_saved"]:
            msg = f"already saved: {season} squad for {club} ({res['players']} players)"
        else:
            bits = [f"saved: {season} squad for {club} ({res['players']} players total)"]
            if res["added"]:
                bits.append(f"+{res['added']} new")
            if res["updated"]:
                bits.append(f"{res['updated']} updated")
            if res["manager_saved"]:
                bits.append("manager saved")
            if res["final_position"] is not None:
                bits.append(f"final position {res['final_position']}")
            msg = ", ".join(bits)
        logger.info("👥 {}", msg)
        logger.log("TRACE", "save_squad payload: {}", json.dumps(args, ensure_ascii=False, default=str))
        return {"status": "already_saved" if res["already_saved"] else "saved", "message": msg, **res}

    async def _do_save_match(self, args: dict[str, Any]) -> dict[str, Any]:
        season = str(args.get("season", "")).strip()
        raw = args.get("matches")
        if not season:
            return {"error": "missing 'season'"}
        if not isinstance(raw, list) or not raw:
            return {"error": "missing 'matches' array"}
        try:
            matches = [Match.model_validate(m) for m in raw]
        except ValidationError as exc:
            return {"error": f"matches invalid: {_short_validation(exc)}"}
        res = self._store.save_matches(season, matches)
        if res["already_saved"]:
            msg = f"already saved: {season} matches ({res['rows']} rows)"
        else:
            msg = f"saved: {season} matches (+{res['added']} added, {res['updated']} updated)"
        logger.info("📅 {}", msg)
        logger.log("TRACE", "save_match payload: {}", json.dumps(args, ensure_ascii=False, default=str))
        return {"status": "already_saved" if res["already_saved"] else "saved", "message": msg, **res}

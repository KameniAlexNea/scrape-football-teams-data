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

from footy_scraper.browser import BrowserSession
from footy_scraper.storage import LeagueStore


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


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
        "save_data",
        "Persist an extracted fragment into the league JSON file. Call this early and often; it merges by season + club.",
        _obj(
            {
                "season": {"type": "string", "description": "Season label like '2024-25'"},
                "club": {"type": "string", "description": "Club name as shown on the site (omit for standings/matches-only payloads)"},
                "source": {"type": "string", "description": "URL you extracted this from (recommended)"},
                "final_position": {"type": "integer", "description": "Club's final league position for this season"},
                "squad": {"type": "array", "description": "List of player objects: name (required), shirt_number, position, age, nationality, height_cm, joined, contract_until, market_value, injury"},
                "manager": {"type": "object", "description": "Manager object: name (required), nationality, appointed"},
                "standings": {"type": "array", "description": "Full end-of-season table rows: club (required), position (required), played, won, drawn, lost, goals_for, goals_against, goal_difference, points"},
                "matches": {"type": "array", "description": "Match results: home_team (required), away_team (required), home_score, away_score, date, round"},
            },
            ["season"],
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

    async def _do_save_data(self, args: dict[str, Any]) -> dict[str, Any]:
        # save_data's schema fields are top-level (season, club, squad, ...),
        # so the whole argument dict IS the payload.
        report = self._store.apply_payload(args)
        bits: list[str] = [f"season={report.get('season')}"]
        if report.get("club"):
            bits.append(f"club={report['club']}")
        if report.get("players_saved") is not None:
            bits.append(f"players={report['players_saved']}")
        if report.get("standings_updated"):
            bits.append(f"standings={report['standings_updated']}")
        if report.get("matches_added") or report.get("matches_updated"):
            bits.append(
                f"matches=+{report.get('matches_added', 0)}/~{report.get('matches_updated', 0)}"
            )
        if report.get("manager_saved"):
            bits.append("manager=✓")
        if report.get("final_position") is not None:
            bits.append(f"final_position={report['final_position']}")
        if report.get("errors"):
            bits.append(f"errors={len(report['errors'])}")
        logger.info("💾 saved {}", ", ".join(bits))
        logger.log("TRACE", "save_data payload: {}", json.dumps(args, ensure_ascii=False, default=str))
        return report

"""The extraction agent — powered by ``claude-agent-sdk``.

The agent's tools are exposed as an in-process MCP server (``ToolExecutor``).
Claude Code (spawned by the SDK as a subprocess) decides which tools to call;
the SDK routes the calls to our Python implementations in-process and streams
every step back as typed messages.

The endpoint is fully configurable — a "specific link": set ``ANTHROPIC_BASE_URL``
to any Anthropic-Messages-compatible server (official API, a gateway, or a
self-hosted model server), plus ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_MODEL``.
"""



import logging
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ServerToolUseBlock,
    create_sdk_mcp_server,
    query,
)

from footy_scraper.tools import ToolExecutor

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "footy_tools"


@dataclass
class AgentResult:
    steps: int
    tool_calls: int
    stop_reason: str
    final_text: str
    session_id: str | None = None
    total_cost_usd: float | None = None


class ScrapeAgent:
    """Runs the extraction mission with Claude Code via ``claude-agent-sdk``."""

    def __init__(
        self,
        *,
        model: str,
        system_prompt: str,
        executor: ToolExecutor,
        max_steps: int = 100,
        api_key: str = "",
        base_url: str | None = None,
        cwd: Path | None = None,
    ):
        self._model = model
        self._system_prompt = system_prompt
        self._executor = executor
        self._max_steps = max_steps
        self._api_key = api_key
        self._base_url = base_url
        self._cwd = Path(cwd) if cwd else Path.cwd()

    async def run(self, mission: str) -> AgentResult:
        """Run one extraction mission; returns the agent's final summary text."""
        sdk_tools = self._executor.build_mcp_tools()
        server = create_sdk_mcp_server(
            name=_MCP_SERVER_NAME, version="1.0.0", tools=sdk_tools
        )
        allowed_tools = [f"mcp__{_MCP_SERVER_NAME}__{t.name}" for t in sdk_tools]

        # The "specific link": forward the Anthropic-compatible endpoint
        # config to the Claude Code subprocess spawned by the SDK.
        env: dict[str, str] = {}
        if self._base_url:
            env["ANTHROPIC_BASE_URL"] = self._base_url
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        if self._model:
            env["ANTHROPIC_MODEL"] = self._model

        options = ClaudeAgentOptions(
            tools=[],  # disable default CLI tools (Read/Write/Bash…): MCP tools only
            mcp_servers={_MCP_SERVER_NAME: server},
            allowed_tools=allowed_tools,
            system_prompt=self._system_prompt,
            # The user's step budget becomes Claude Code's turn budget.
            max_turns=self._max_steps,
            permission_mode="acceptEdits",
            env=env,
            model=self._model,
            cwd=str(self._cwd),
        )

        final_text = ""
        tool_calls = 0
        result_msg: ResultMessage | None = None
        result_seen = False

        try:
            # The SDK already executes in-process MCP tools for us; the stream
            # lets us count steps and keep the last text as the final summary.
            async for message in query(prompt=mission, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ServerToolUseBlock):
                            tool_calls += 1
                            logger.info("[agent] tool call #%d: %s", tool_calls, block.name)
                elif isinstance(message, ResultMessage) and not result_seen:
                    result_msg = message
                    if message.result:
                        final_text = message.result
                    result_seen = True
        except Exception as exc:  # noqa: BLE001 - surface any failure, don't crash the CLI
            logger.exception("Agent run failed")
            return AgentResult(
                steps=0,
                tool_calls=tool_calls,
                stop_reason=f"error: {type(exc).__name__}",
                final_text=f"Agent run failed: {exc}",
            )

        if result_msg is not None and result_msg.is_error:
            logger.warning("Agent returned an error result: %s", result_msg.result)
            final_text = f"Claude Code returned an error result: {result_msg.result or ''}"

        steps = (result_msg.num_turns if result_msg is not None else 0) or tool_calls
        stop_reason = result_msg.stop_reason if result_msg is not None else "end_turn"

        return AgentResult(
            steps=steps,
            tool_calls=tool_calls,
            stop_reason=stop_reason or "end_turn",
            final_text=final_text.strip(),
            session_id=result_msg.session_id if result_msg is not None else None,
            total_cost_usd=result_msg.total_cost_usd if result_msg is not None else None,
        )


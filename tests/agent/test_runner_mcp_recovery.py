from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.mark.asyncio
async def test_runner_classifies_mcp_timeout_result() -> None:
    provider = MagicMock(spec=LLMProvider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.get_metadata.return_value = {
        "read_only": True,
        "concurrency_safe": True,
        "exclusive": False,
        "config_key": "mcp",
        "scopes": ("mcp",),
    }
    tools.execute = AsyncMock(return_value="(MCP tool call timed out after 5s)")

    runner = AgentRunner(provider)
    result, event, error = await runner._run_tool(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        ),
        ToolCallRequest(id="call_mcp", name="mcp_docs_search", arguments={"query": "x"}),
        {},
        {},
    )

    assert "timed out" in result
    assert error is None
    assert event["status"] == "error"
    assert event["failure_category"] == "mcp_timeout"
    assert event["recovery_action"] == "retry"
    assert event["retryable"] is True
    assert event["needs_user_input"] is False
    assert event["diagnostic_label"] == "MCP timeout"
    assert "reachable" in event["recommended_action"]
    assert event["read_only"] is True
    assert event["concurrency_safe"] is True


def test_runner_classifies_mcp_protocol_and_permission_results() -> None:
    assert (
        AgentRunner._mcp_failure_category(
            "mcp_bad_server_tool",
            "(MCP tool call failed: JSONRPC protocol error)",
        )
        == "mcp_protocol_error"
    )
    assert (
        AgentRunner._mcp_failure_category(
            "mcp_secure_tool",
            "(MCP tool call failed: permission denied)",
        )
        == "mcp_permission_denied"
    )
    assert (
        AgentRunner._mcp_failure_category(
            "read_file",
            "(MCP tool call failed: permission denied)",
        )
        is None
    )


def test_runner_recovery_metadata_includes_mcp_diagnostics() -> None:
    protocol = AgentRunner._tool_recovery_metadata("mcp_protocol_error")
    permission = AgentRunner._tool_recovery_metadata("mcp_permission_denied")

    assert protocol["diagnostic_label"] == "Protocol error"
    assert "stdout" in protocol["diagnostic_hint"]
    assert permission["diagnostic_label"] == "Permission denied"
    assert "auth" in permission["recommended_action"].lower()

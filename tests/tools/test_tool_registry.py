from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


class _FakeTool(Tool):
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs


class _ReadOnlyTool(_FakeTool):
    config_key = "fake"
    _scopes = {"core", "subagent"}

    @property
    def read_only(self) -> bool:
        return True


class _InvalidSchemaTool(_FakeTool):
    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "array", "items": {"type": "string"}}


class _InvalidNameTool(_FakeTool):
    @property
    def name(self) -> str:
        return "bad tool name"


class _ConflictingCapabilityTool(_FakeTool):
    @property
    def read_only(self) -> bool:
        return True

    @property
    def exclusive(self) -> bool:
        return True


def _tool_names(definitions: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for definition in definitions:
        fn = definition.get("function", {})
        names.append(fn.get("name", ""))
    return names


def test_get_definitions_orders_builtins_then_mcp_tools() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("mcp_git_status"))
    registry.register(_FakeTool("write_file"))
    registry.register(_FakeTool("mcp_fs_list"))
    registry.register(_FakeTool("read_file"))

    assert _tool_names(registry.get_definitions()) == [
        "read_file",
        "write_file",
        "mcp_fs_list",
        "mcp_git_status",
    ]


def test_prepare_call_read_file_rejects_non_object_params_with_actionable_hint() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    tool, params, error = registry.prepare_call("read_file", ["foo.txt"])

    assert tool is None
    assert params == ["foo.txt"]
    assert error is not None
    assert "must be a JSON object" in error
    assert "Use named parameters" in error


def test_prepare_call_other_tools_keep_generic_object_validation() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("grep"))

    tool, params, error = registry.prepare_call("grep", ["TODO"])

    assert tool is not None
    assert params == ["TODO"]
    assert error == "Error: Invalid parameters for tool 'grep': parameters must be an object, got list"


def test_get_definitions_returns_cached_result() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    first = registry.get_definitions()
    assert registry._cached_definitions is not None
    second = registry.get_definitions()
    assert first == second


def test_register_invalidates_cache() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    first = registry.get_definitions()
    registry.register(_FakeTool("write_file"))
    second = registry.get_definitions()
    assert first is not second
    assert len(second) == 2


def test_unregister_invalidates_cache() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    registry.register(_FakeTool("write_file"))
    first = registry.get_definitions()
    registry.unregister("write_file")
    second = registry.get_definitions()
    assert first is not second
    assert len(second) == 1


def test_register_stores_standard_metadata() -> None:
    registry = ToolRegistry()
    registry.register(_ReadOnlyTool("read_file"))

    metadata = registry.get_metadata("read_file")

    assert metadata is not None
    assert metadata["name"] == "read_file"
    assert metadata["config_key"] == "fake"
    assert metadata["scopes"] == ("core", "subagent")
    assert metadata["read_only"] is True
    assert metadata["concurrency_safe"] is True
    assert metadata["exclusive"] is False


def test_get_metadata_returns_copy() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    metadata = registry.get_metadata("read_file")
    assert metadata is not None
    metadata["parameters"]["properties"]["mutated"] = {"type": "string"}

    fresh = registry.get_metadata("read_file")
    assert fresh is not None
    assert "mutated" not in fresh["parameters"]["properties"]


def test_unregister_clears_metadata() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    registry.unregister("read_file")

    assert registry.get_metadata("read_file") is None
    assert registry.get_metadata_map() == {}


def test_register_rejects_invalid_tool_name() -> None:
    registry = ToolRegistry()

    try:
        registry.register(_InvalidNameTool("ignored"))
    except ValueError as exc:
        assert "name may only contain" in str(exc)
    else:
        raise AssertionError("invalid tool name should fail registration")


def test_register_rejects_non_object_parameter_schema() -> None:
    registry = ToolRegistry()

    try:
        registry.register(_InvalidSchemaTool("read_file"))
    except ValueError as exc:
        assert "parameters schema must be object type" in str(exc)
    else:
        raise AssertionError("invalid parameter schema should fail registration")


def test_register_rejects_conflicting_tool_capabilities() -> None:
    registry = ToolRegistry()

    try:
        registry.register(_ConflictingCapabilityTool("bad_capabilities"))
    except ValueError as exc:
        assert "read_only and exclusive cannot both be true" in str(exc)
    else:
        raise AssertionError("conflicting capabilities should fail registration")

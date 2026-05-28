"""Tool discovery and registration via package scanning."""
from __future__ import annotations

import importlib
import pkgutil
from copy import deepcopy
from importlib.metadata import entry_points
from typing import Any, TypedDict

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolRegistrationError
from nanobot.agent.tools.registry import ToolRegistry

_SKIP_MODULES = frozenset({
    "base", "schema", "registry", "context", "loader", "config",
    "file_state", "sandbox", "mcp", "__init__", "runtime_state",
})


class PluginDiagnostic(TypedDict):
    source: str
    plugin_name: str
    tool_class: str
    stage: str
    code: str
    message: str
    config_key: str


class ToolLoader:
    def __init__(self, package: Any = None, *, test_classes: list[type[Tool]] | None = None):
        if package is None:
            import nanobot.agent.tools as _pkg
            package = _pkg
        self._package = package
        self._test_classes = test_classes
        self._discovered: list[type[Tool]] | None = None
        self._plugins: dict[str, type[Tool]] | None = None
        self._plugin_diagnostics: list[PluginDiagnostic] = []

    def discover(self) -> list[type[Tool]]:
        if self._test_classes is not None:
            return list(self._test_classes)
        if self._discovered is not None:
            return self._discovered
        seen: set[int] = set()
        results: list[type[Tool]] = []
        for _importer, module_name, _ispkg in pkgutil.iter_modules(self._package.__path__):
            if module_name.startswith("_") or module_name in _SKIP_MODULES:
                continue
            try:
                module = importlib.import_module(f".{module_name}", self._package.__name__)
            except Exception:
                logger.exception("Failed to import tool module: %s", module_name)
                continue
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Tool)
                    and attr is not Tool
                    and not attr_name.startswith("_")
                    and not getattr(attr, "__abstractmethods__", None)
                    and getattr(attr, "_plugin_discoverable", True)
                    and id(attr) not in seen
                ):
                    seen.add(id(attr))
                    results.append(attr)
        results.sort(key=lambda cls: cls.__name__)
        self._discovered = results
        return results

    def _discover_plugins(self) -> dict[str, type[Tool]]:
        """Discover external tool plugins registered via entry_points."""
        if self._plugins is not None:
            return self._plugins
        plugins: dict[str, type[Tool]] = {}
        self._plugin_diagnostics = []
        try:
            eps = entry_points(group="nanobot.tools")
        except Exception:
            return plugins
        for ep in eps:
            try:
                cls = ep.load()
                if (
                    isinstance(cls, type)
                    and issubclass(cls, Tool)
                    and not getattr(cls, "__abstractmethods__", None)
                    and getattr(cls, "_plugin_discoverable", True)
                ):
                    plugins[ep.name] = cls
            except Exception as exc:
                tool_class = getattr(ep, "value", "") or ep.name
                diagnostic = self._build_plugin_diagnostic(
                    source=ep.name,
                    tool_class=str(tool_class),
                    stage="load",
                    code="load_error",
                    message=f"{type(exc).__name__}: {exc}",
                    config_key="",
                )
                self._plugin_diagnostics.append(diagnostic)
                logger.exception(
                    "Failed to load tool plugin '{}' [{}]: {}",
                    ep.name,
                    diagnostic["code"],
                    diagnostic["message"],
                )
        self._plugins = plugins
        return plugins

    @property
    def plugin_diagnostics(self) -> list[PluginDiagnostic]:
        return deepcopy(self._plugin_diagnostics)

    @staticmethod
    def _build_plugin_diagnostic(
        *,
        source: str,
        tool_class: str,
        stage: str,
        code: str,
        message: str,
        config_key: str,
    ) -> PluginDiagnostic:
        return {
            "source": source,
            "plugin_name": source,
            "tool_class": tool_class,
            "stage": stage,
            "code": code,
            "message": message,
            "config_key": config_key,
        }

    @staticmethod
    def _config_issue_code(exc: BaseException, tool_cls: type[Tool]) -> str:
        config_key = getattr(tool_cls, "config_key", "")
        if not config_key:
            return "register_error"
        if isinstance(exc, (AttributeError, KeyError)):
            return "missing_config"
        message = str(exc).lower()
        if "config" in message and ("missing" in message or "required" in message):
            return "missing_config"
        return "register_error"

    def load(self, ctx: Any, registry: ToolRegistry, *, scope: str = "core") -> list[str]:
        registered: list[str] = []
        builtin_names: set[str] = set()
        sources = [
            ([(cls.__name__, cls) for cls in self.discover()], False),
            (list(self._discover_plugins().items()), True),
        ]
        for source, is_plugin_source in sources:
            for source_name, tool_cls in source:
                cls_label = tool_cls.__name__
                try:
                    if scope not in getattr(tool_cls, "_scopes", {"core"}):
                        continue
                    if not tool_cls.enabled(ctx):
                        continue
                    tool = tool_cls.create(ctx)
                    if registry.has(tool.name):
                        if is_plugin_source and tool.name in builtin_names:
                            self._plugin_diagnostics.append(self._build_plugin_diagnostic(
                                source=source_name,
                                tool_class=cls_label,
                                stage="register",
                                code="name_conflict",
                                message=f"conflicts with built-in tool {tool.name}",
                                config_key=getattr(tool_cls, "config_key", ""),
                            ))
                            logger.warning(
                                "Plugin %s skipped: conflicts with built-in tool %s",
                                cls_label, tool.name,
                            )
                            continue
                        logger.warning(
                            "Tool name collision: %s from %s overwrites existing",
                            tool.name, cls_label,
                        )
                    registry.register(tool)
                    metadata = registry.get_metadata(tool.name)
                    registered.append(metadata["name"] if metadata else tool.name)
                    if metadata:
                        logger.debug(
                            "Registered tool '{}' scopes={} read_only={} concurrency_safe={} exclusive={}",
                            metadata["name"],
                            ",".join(metadata["scopes"]),
                            metadata["read_only"],
                            metadata["concurrency_safe"],
                            metadata["exclusive"],
                        )
                    if not is_plugin_source:
                        builtin_names.add(tool.name)
                except ToolRegistrationError as exc:
                    if is_plugin_source:
                        for issue in exc.issues:
                            self._plugin_diagnostics.append(self._build_plugin_diagnostic(
                                source=source_name,
                                tool_class=cls_label,
                                stage="register",
                                code=issue["code"],
                                message=issue["message"],
                                config_key=getattr(tool_cls, "config_key", ""),
                            ))
                    logger.exception("Failed to register tool '{}' [{}]", cls_label, exc)
                except Exception as exc:
                    if is_plugin_source:
                        code = self._config_issue_code(exc, tool_cls)
                        self._plugin_diagnostics.append(self._build_plugin_diagnostic(
                            source=source_name,
                            tool_class=cls_label,
                            stage="register",
                            code=code,
                            message=f"{type(exc).__name__}: {exc}",
                            config_key=getattr(tool_cls, "config_key", ""),
                        ))
                    logger.exception("Failed to register tool: %s", cls_label)
        return registered

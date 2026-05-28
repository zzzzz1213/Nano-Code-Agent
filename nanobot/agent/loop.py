"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from contextlib import AsyncExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, CompositeHook
from nanobot.agent.memory import Consolidator, Dream
from nanobot.agent.memory_candidates import build_memory_candidate
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults, ModelPresetConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot
from nanobot.session.goal_state import (
    runner_wall_llm_timeout_s,
)
from nanobot.session.manager import Session, SessionManager
from nanobot.session.webui_turns import (
    WebuiTurnCoordinator,
    build_bus_progress_callback,
    build_turn_checkpoint,
    mark_webui_session,
)
from nanobot.utils.document import extract_documents
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.image_generation_intent import image_generation_prompt
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None
    pending_summary: str | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _RUNTIME_CHECKPOINT_MATERIALIZED_KEY = "runtime_checkpoint_materialized"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _CHECKPOINT_SHELL_TOOL_NAMES = frozenset({"exec", "shell"})
    _CHECKPOINT_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file", "notebook_edit"})
    _CHECKPOINT_CONFIDENT_MCP_CAPABILITY_SOURCES = frozenset((
        "annotation",
        "prompt",
        "resource",
        "server_transport",
    ))
    _CHECKPOINT_MCP_REVIEW_MARKERS = (
        "_write",
        "_edit",
        "_delete",
        "_remove",
        "_create",
        "_update",
        "_patch",
        "_exec",
        "_shell",
        "_command",
        "_run",
    )

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        max_concurrent_tools: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
    ):
        from nanobot.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.max_concurrent_tools = (
            max_concurrent_tools
            if max_concurrent_tools is not None
            else defaults.max_concurrent_tools
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._pending_turn_latency_ms: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self._webui_turns = WebuiTurnCoordinator(
            bus=self.bus,
            sessions=self.sessions,
            schedule_background=lambda coro: self._schedule_background(coro),
        )
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._restored_runtime_checkpoints: dict[str, dict[str, Any]] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from nanobot.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            max_concurrent_tools=defaults.max_concurrent_tools,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self.dream.set_provider(provider, model)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from nanobot.agent.tools.context import ToolContext
        from nanobot.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            timezone=self.context.timezone or "UTC",
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from nanobot.agent.tools.context import ContextAware, RequestContext

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        return self.context.build_messages(
            history=history,
            current_message=image_generation_prompt(msg.content, msg.metadata),
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
        )

    async def _publish_active_skills(
        self,
        msg: InboundMessage,
        *,
        session: Session,
        session_summary: str | None,
    ) -> None:
        if msg.channel != "websocket":
            return
        snapshot = self.context.build_active_skills_snapshot(
            session_summary=session_summary,
            session_metadata=session.metadata,
            current_message=image_generation_prompt(msg.content, msg.metadata),
        )
        await self._webui_turns.publish_active_skills(msg, skills=snapshot)

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        turn_id: str | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks) if self._extra_hooks else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)
            checkpoint = build_turn_checkpoint(payload, turn_id=turn_id or session_key or session.key)
            if checkpoint.get("phase") != "final_response":
                await self._webui_turns.publish_turn_checkpoint(
                    InboundMessage(
                        channel=channel,
                        sender_id="agent",
                        chat_id=str(chat_id),
                        content="",
                        metadata=dict(metadata or {}),
                    ),
                    checkpoint=checkpoint,
                )

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                max_concurrent_tools=self.max_concurrent_tools,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=self.workspace,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed NANOBOT_LLM_TIMEOUT_S; idle stall
                # is still capped by NANOBOT_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=(session.metadata if session is not None else None),
                ),
            ))
        finally:
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, msg.session_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        # Register a pending queue so follow-up messages for this session are
        # routed here (mid-turn injection) instead of spawning a new task.
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[session_key] = pending

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    if msg.channel == "websocket":
                        turn_lat = self._pending_turn_latency_ms.pop(session_key, None)
                        await self._webui_turns.handle_turn_end(
                            msg,
                            session_key=session_key,
                            latency_ms=turn_lat,
                        )
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            self._schedule_background(
                                self._publish_recovered_runtime_checkpoint(
                                    msg,
                                    session,
                                    turn_id=f"{key}:cancelled",
                                )
                            )
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
        finally:
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )
            await self._webui_turns.publish_run_status(msg, "idle")
            self._pending_turn_latency_ms.pop(session_key, None)
            self._webui_turns.discard(session_key)

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            await self._publish_recovered_runtime_checkpoint(msg, session, turn_id=key)
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)
            compaction = self._consume_session_compaction_event(session)
            if compaction:
                await self._webui_turns.publish_context_compaction(
                    msg,
                    compaction=compaction,
                )
                self.sessions.save(session)

        compactions = await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        await self._publish_context_compactions(msg, compactions)
        await self._publish_memory_snapshot(msg, session_summary=pending)
        await self._publish_active_skills(
            msg,
            session=session,
            session_summary=pending,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        if channel == "websocket":
            self._pending_turn_latency_ms[key] = latency_ms
        session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
            outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)

        if self._restore_runtime_checkpoint(ctx.session):
            await self._publish_recovered_runtime_checkpoint(
                ctx.msg,
                ctx.session,
                turn_id=ctx.turn_id,
            )
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        if pending:
            compaction = self._consume_session_compaction_event(ctx.session)
            if compaction:
                await self._webui_turns.publish_context_compaction(
                    ctx.msg,
                    compaction=compaction,
                )
                self.sessions.save(ctx.session)
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        compactions = await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )
        await self._publish_context_compactions(ctx.msg, compactions)
        await self._publish_memory_snapshot(ctx.msg, session_summary=ctx.pending_summary)
        await self._publish_active_skills(
            ctx.msg,
            session=ctx.session,
            session_summary=ctx.pending_summary,
        )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._webui_turns.capture_title_context(
            ctx.session_key,
            ctx.msg,
            self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        await self._webui_turns.publish_run_status(ctx.msg, "running")
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            turn_id=ctx.turn_id,
            pending_queue=ctx.pending_queue,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.msg.channel == "websocket":
            self._pending_turn_latency_ms[ctx.session_key] = ctx.turn_latency_ms
        ctx.session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        candidate = build_memory_candidate(
            memory=self.context.memory,
            user_text=ctx.msg.content,
            assistant_text=ctx.final_content,
            turn_id=ctx.turn_id,
        )
        if candidate:
            await self._webui_turns.publish_memory_candidate(
                ctx.msg,
                candidate=candidate,
            )
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        )
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        session.metadata.pop(self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY, None)
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)
        session.metadata.pop(self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY, None)

    @staticmethod
    def _restored_runtime_checkpoint_key(session: Session) -> str:
        key = getattr(session, "key", None)
        return key if isinstance(key, str) and key else str(id(session))

    def _remember_restored_runtime_checkpoint(
        self,
        session: Session,
        checkpoint: dict[str, Any],
    ) -> None:
        if not hasattr(self, "_restored_runtime_checkpoints"):
            self._restored_runtime_checkpoints = {}
        self._restored_runtime_checkpoints[
            self._restored_runtime_checkpoint_key(session)
        ] = dict(checkpoint)

    def _consume_restored_runtime_checkpoint(self, session: Session) -> dict[str, Any] | None:
        if not hasattr(self, "_restored_runtime_checkpoints"):
            self._restored_runtime_checkpoints = {}
        return self._restored_runtime_checkpoints.pop(
            self._restored_runtime_checkpoint_key(session),
            None,
        )

    async def _publish_recovered_runtime_checkpoint(
        self,
        msg: InboundMessage,
        session: Session,
        *,
        turn_id: str,
    ) -> None:
        checkpoint_payload = self._consume_restored_runtime_checkpoint(session)
        if checkpoint_payload is None:
            return
        checkpoint = build_turn_checkpoint(checkpoint_payload, turn_id=turn_id)
        pending = checkpoint_payload.get("pending_tool_calls")
        checkpoint["source"] = "recovered"
        checkpoint["recovered"] = True
        checkpoint["recovered_pending_tool_count"] = (
            len(pending) if isinstance(pending, list) else 0
        )
        await self._webui_turns.publish_turn_checkpoint(msg, checkpoint=checkpoint)

    async def _publish_context_compactions(
        self,
        msg: InboundMessage,
        events: list[dict[str, Any]],
    ) -> None:
        if not isinstance(events, list):
            return
        for event in events:
            if isinstance(event, dict):
                await self._webui_turns.publish_context_compaction(
                    msg,
                    compaction=event,
                )

    async def _publish_memory_snapshot(
        self,
        msg: InboundMessage,
        *,
        session_summary: str | None,
    ) -> None:
        if msg.channel != "websocket":
            return
        snapshot = self.context.build_memory_snapshot(session_summary=session_summary)
        await self._webui_turns.publish_memory_snapshot(msg, snapshot=snapshot)

    def _consume_session_compaction_event(self, session: Session) -> dict[str, Any] | None:
        event = session.metadata.get("_last_compaction")
        if not isinstance(event, dict):
            return None
        if event.get("webui_published") is True:
            return None
        session.metadata["_last_compaction"] = {**event, "webui_published": True}
        return event

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    @staticmethod
    def _tool_result_id(message: dict[str, Any]) -> str:
        if message.get("role") != "tool":
            return ""
        raw = message.get("tool_call_id") or message.get("call_id")
        return raw if isinstance(raw, str) and raw else ""

    @staticmethod
    def _checkpoint_tool_call_id(tool_call: dict[str, Any]) -> str:
        raw = tool_call.get("id") or tool_call.get("tool_call_id") or tool_call.get("call_id")
        return raw if isinstance(raw, str) and raw else ""

    @staticmethod
    def _checkpoint_tool_name(tool_call: dict[str, Any]) -> str:
        function = tool_call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        raw = tool_call.get("name")
        return raw if isinstance(raw, str) else ""

    @staticmethod
    def _checkpoint_tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call.get("function")
        raw_arguments = function.get("arguments") if isinstance(function, dict) else tool_call.get("arguments")
        if isinstance(raw_arguments, dict):
            return dict(raw_arguments)
        if not isinstance(raw_arguments, str) or not raw_arguments.strip():
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _checkpoint_tool_result_text(result: Any) -> str:
        if isinstance(result, str):
            text = result
        else:
            try:
                text = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                text = str(result)
        text = text.strip()
        return text or "(empty)"

    @staticmethod
    def _checkpoint_tool_recovery_strategy(tool_call: dict[str, Any]) -> str:
        """Return how an interrupted pending tool should be represented."""
        recovery_action = tool_call.get("recovery_action")
        failure_category = tool_call.get("failure_category")
        if tool_call.get("needs_user_input") is True:
            return "requires_user"
        if recovery_action in {"ask_user", "revise_request"}:
            return "requires_user"
        if failure_category == "safety_block":
            return "requires_user"
        if tool_call.get("retryable") is True:
            return "retryable"
        if recovery_action in {"retry", "retry_alternative", "revise_arguments"}:
            return "retryable"
        return "compensated"

    @classmethod
    def _checkpoint_tool_review_kind(cls, name: str) -> str | None:
        normalized = name.lower()
        if normalized in cls._CHECKPOINT_SHELL_TOOL_NAMES:
            return "shell"
        if normalized in cls._CHECKPOINT_WRITE_TOOL_NAMES:
            return "write"
        if normalized.startswith("mcp_") and any(
            marker in normalized for marker in cls._CHECKPOINT_MCP_REVIEW_MARKERS
        ):
            return "mcp_mutating"
        return None

    @classmethod
    def _checkpoint_tool_metadata_scopes(cls, metadata: dict[str, Any]) -> set[str]:
        scopes = metadata.get("scopes")
        if not isinstance(scopes, (list, tuple, set)):
            return set()
        return {
            str(scope).strip().lower()
            for scope in scopes
            if isinstance(scope, str) and scope.strip()
        }

    @classmethod
    def _checkpoint_tool_review_kind_from_metadata(
        cls,
        name: str,
        metadata: dict[str, Any],
    ) -> str | None:
        explicit = cls._checkpoint_tool_review_kind(name)
        if explicit is not None:
            return explicit

        config_key = metadata.get("config_key")
        normalized_config = config_key.strip().lower() if isinstance(config_key, str) else ""
        scopes = cls._checkpoint_tool_metadata_scopes(metadata)
        if normalized_config == "exec" or {"exec", "shell"} & scopes:
            return "shell"

        if normalized_config == "mcp":
            if metadata.get("exclusive") is True or metadata.get("read_only") is not True:
                if str(metadata.get("mcp_origin") or "").strip().lower() == "local":
                    return "mcp_local"
                return "mcp_mutating"
            if cls._checkpoint_mcp_requires_review(metadata):
                return "mcp_local"

        if metadata.get("exclusive") is True:
            return "exclusive"
        if metadata.get("read_only") is not True:
            return "mutating"
        if metadata.get("concurrency_safe") is not True:
            return "concurrency_review"
        return None

    @classmethod
    def _checkpoint_mcp_requires_review(cls, metadata: dict[str, Any]) -> bool:
        config_key = metadata.get("config_key")
        if not isinstance(config_key, str) or config_key.strip().lower() != "mcp":
            return False
        if metadata.get("read_only") is not True:
            return True
        if metadata.get("exclusive") is True or metadata.get("concurrency_safe") is not True:
            return True
        origin = str(metadata.get("mcp_origin") or "").strip().lower()
        if origin != "local":
            return False
        source = str(metadata.get("mcp_capability_source") or "").strip().lower()
        return bool(source) and source not in cls._CHECKPOINT_CONFIDENT_MCP_CAPABILITY_SOURCES

    @classmethod
    def _checkpoint_tool_is_shell_or_write(cls, name: str, metadata: dict[str, Any] | None = None) -> bool:
        return cls._checkpoint_tool_review_kind_from_metadata(name, metadata or {}) in {
            "shell",
            "write",
        }

    def _checkpoint_tool_metadata(self, name: str) -> dict[str, Any]:
        tools = getattr(self, "tools", None)
        get_metadata = getattr(tools, "get_metadata", None)
        if not callable(get_metadata):
            return {}
        try:
            metadata = get_metadata(name)
        except Exception:
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _checkpoint_tool_is_resumable(
        self,
        tool_call: dict[str, Any],
        recovery_strategy: str,
    ) -> bool:
        """Return whether a pending tool is safe to offer for future resume."""
        if recovery_strategy == "requires_user":
            return False
        name = self._checkpoint_tool_name(tool_call)
        if not name:
            return False
        metadata = self._checkpoint_tool_metadata(name)
        if self._checkpoint_tool_is_shell_or_write(name, metadata):
            return False
        if self._checkpoint_mcp_requires_review(metadata):
            return False
        return (
            metadata.get("read_only") is True
            and metadata.get("concurrency_safe") is True
            and metadata.get("exclusive") is not True
        )

    @staticmethod
    def _checkpoint_interrupted_tool_content(
        recovery_strategy: str,
        *,
        resumable: bool = False,
    ) -> str:
        base = "Error: Task interrupted before this tool finished."
        if resumable:
            return f"{base} Safe resume candidate; do not repeat automatically."
        if recovery_strategy == "requires_user":
            return f"{base} Recovery requires user input before retrying."
        if recovery_strategy == "retryable":
            return f"{base} Recovery strategy: retryable; do not repeat automatically."
        return base

    def _checkpoint_recovery_review(
        self,
        pending_tool_calls: list[dict[str, Any]],
        *,
        include_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build UI-safe review groups for pending checkpoint tools."""
        groups: dict[str, list[str]] = {
            "safe_resume": [],
            "review_required": [],
            "needs_input": [],
            "blocked": [],
        }
        items: list[dict[str, Any]] = []

        for tool_call in pending_tool_calls:
            tool_id = self._checkpoint_tool_call_id(tool_call)
            if not tool_id or (include_ids is not None and tool_id not in include_ids):
                continue
            name = self._checkpoint_tool_name(tool_call) or "tool"
            recovery_strategy = self._checkpoint_tool_recovery_strategy(tool_call)
            metadata = self._checkpoint_tool_metadata(name)
            resumable = self._checkpoint_tool_is_resumable(tool_call, recovery_strategy)
            review_kind = self._checkpoint_tool_review_kind_from_metadata(name, metadata)
            group = self._checkpoint_recovery_review_group(
                name,
                tool_call,
                recovery_strategy,
                metadata=metadata,
                resumable=resumable,
            )
            review_state = self._checkpoint_recovery_review_state(tool_call, group)
            groups[group].append(tool_id)
            items.append({
                "tool_call_id": tool_id,
                "name": name,
                "group": group,
                "reason": self._checkpoint_recovery_review_reason(
                    name,
                    tool_call,
                    recovery_strategy,
                    group,
                    metadata=metadata,
                ),
                "recovery_action": self._checkpoint_recovery_review_action(group),
                "action_label": self._checkpoint_recovery_review_action_label(group),
                "review_kind": review_kind
                or self._checkpoint_recovery_review_kind_for_group(group, metadata=metadata),
                "summary": self._checkpoint_recovery_review_summary(
                    name,
                    tool_call,
                    group,
                ),
                "config_key": metadata.get("config_key"),
                "scope": self._checkpoint_recovery_review_scope(metadata),
                "can_resume_now": group == "safe_resume",
                "can_retry_now": self._checkpoint_recovery_review_can_retry_now(
                    tool_call,
                    group,
                ),
                "review_state": review_state,
                "status_label": self._checkpoint_recovery_review_status_label(
                    tool_call,
                    group,
                    review_state=review_state,
                ),
                "input_required": self._checkpoint_recovery_review_input_required(
                    tool_call,
                    group,
                ),
                "input_placeholder": self._checkpoint_recovery_review_input_placeholder(
                    name,
                    tool_call,
                    group,
                ),
                "review_confirmed": tool_call.get("review_confirmed") is True,
            })

        return {
            "safe_resume_tool_call_ids": groups["safe_resume"],
            "safe_resume_tool_count": len(groups["safe_resume"]),
            "review_required_tool_call_ids": groups["review_required"],
            "review_required_tool_count": len(groups["review_required"]),
            "needs_input_tool_call_ids": groups["needs_input"],
            "needs_input_tool_count": len(groups["needs_input"]),
            "blocked_tool_call_ids": groups["blocked"],
            "blocked_tool_count": len(groups["blocked"]),
            "recovery_review_items": items,
            "recovery_review_count": len(items),
        }

    def _checkpoint_recovery_review_group(
        self,
        name: str,
        tool_call: dict[str, Any],
        recovery_strategy: str,
        *,
        metadata: dict[str, Any],
        resumable: bool,
    ) -> str:
        if resumable:
            return "safe_resume"
        failure_category = tool_call.get("failure_category")
        if failure_category == "safety_block":
            return "blocked"
        if recovery_strategy == "requires_user":
            return "needs_input"
        if self._checkpoint_tool_review_kind_from_metadata(name, metadata) is not None:
            return "review_required"
        if metadata.get("exclusive") is True or metadata.get("read_only") is not True:
            return "review_required"
        if recovery_strategy == "retryable":
            return "review_required"
        return "review_required"

    @classmethod
    def _checkpoint_recovery_review_reason(
        cls,
        name: str,
        tool_call: dict[str, Any],
        recovery_strategy: str,
        group: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        metadata = metadata or {}
        failure_category = tool_call.get("failure_category")
        if isinstance(failure_category, str) and failure_category:
            return failure_category
        if group == "safe_resume":
            return "read_only_safe_candidate"
        if group == "blocked":
            return "blocked_by_safety_policy"
        if group == "needs_input":
            return "requires_user_input"
        review_kind = cls._checkpoint_tool_review_kind_from_metadata(name, metadata)
        if review_kind == "shell":
            return "shell_command_requires_review"
        if review_kind == "write":
            return "write_tool_requires_review"
        if review_kind == "mcp_local":
            return "mcp_local_tool_requires_review"
        if review_kind == "mcp_mutating":
            return "mcp_mutating_tool_requires_review"
        if review_kind == "exclusive":
            return "exclusive_tool_requires_review"
        if review_kind == "mutating":
            return "non_read_only_tool_requires_review"
        if review_kind == "concurrency_review":
            return "non_concurrency_safe_tool_requires_review"
        if recovery_strategy == "retryable":
            return "retryable_requires_review"
        return "pending_tool_requires_review"

    @staticmethod
    def _checkpoint_recovery_review_action(group: str) -> str:
        if group == "safe_resume":
            return "resume_safe"
        if group == "needs_input":
            return "provide_input"
        if group == "blocked":
            return "revise_request"
        return "review_before_retry"

    @staticmethod
    def _checkpoint_recovery_review_action_label(group: str) -> str:
        if group == "safe_resume":
            return "Resume safe tools"
        if group == "needs_input":
            return "Collect input"
        if group == "blocked":
            return "Revise request"
        return "Review before retry"

    @staticmethod
    def _checkpoint_recovery_review_scope(metadata: dict[str, Any]) -> str | None:
        config_key = metadata.get("config_key")
        if isinstance(config_key, str):
            return config_key
        scopes = metadata.get("scopes")
        if isinstance(scopes, (list, tuple)):
            for scope in scopes:
                if isinstance(scope, str) and scope:
                    return scope
        return None

    @staticmethod
    def _checkpoint_recovery_review_kind_for_group(
        group: str,
        *,
        metadata: dict[str, Any],
    ) -> str:
        if group == "safe_resume":
            return "read_only"
        if group == "needs_input":
            return "needs_input"
        if group == "blocked":
            return "blocked"
        if metadata.get("config_key") == "mcp" and AgentLoop._checkpoint_mcp_requires_review(metadata):
            return "mcp_local"
        if metadata.get("exclusive") is True:
            return "exclusive"
        if metadata.get("read_only") is True:
            return "retryable"
        return "mutating"

    @classmethod
    def _checkpoint_recovery_review_state(
        cls,
        tool_call: dict[str, Any],
        group: str,
    ) -> str:
        raw_state = tool_call.get("review_state")
        if isinstance(raw_state, str) and raw_state:
            return raw_state
        if group == "safe_resume":
            return "ready_to_resume"
        if group == "blocked":
            return "blocked"
        if group == "needs_input":
            return "awaiting_input"
        return "awaiting_review"

    @classmethod
    def _checkpoint_recovery_review_status_label(
        cls,
        tool_call: dict[str, Any],
        group: str,
        *,
        review_state: str,
    ) -> str:
        if review_state == "confirmed":
            return "Retry confirmed"
        if review_state == "input_provided":
            return "Input collected"
        if review_state == "ready_to_resume":
            return "Ready to resume"
        if review_state == "blocked":
            return "Blocked by safety policy"
        if group == "needs_input":
            return "Waiting for input"
        if group == "review_required":
            return "Waiting for confirmation"
        return "Pending action"

    @staticmethod
    def _checkpoint_recovery_review_can_retry_now(
        tool_call: dict[str, Any],
        group: str,
    ) -> bool:
        if group == "safe_resume":
            return True
        review_state = tool_call.get("review_state")
        return isinstance(review_state, str) and review_state in {
            "confirmed",
            "input_provided",
        }

    @staticmethod
    def _checkpoint_recovery_review_input_required(
        tool_call: dict[str, Any],
        group: str,
    ) -> bool:
        if group == "needs_input":
            return True
        return tool_call.get("needs_user_input") is True

    @classmethod
    def _checkpoint_recovery_review_input_placeholder(
        cls,
        name: str,
        tool_call: dict[str, Any],
        group: str,
    ) -> str | None:
        if group != "needs_input":
            return None
        arguments = cls._checkpoint_tool_arguments(tool_call)
        if "query" in arguments:
            return "Provide the missing query details"
        if "prompt" in arguments:
            return "Provide the missing prompt details"
        if "command" in arguments:
            return "Provide the missing command details"
        return f"Provide missing input for {name}"

    @classmethod
    def _checkpoint_recovery_review_summary(
        cls,
        name: str,
        tool_call: dict[str, Any],
        group: str,
    ) -> str:
        arguments = cls._checkpoint_tool_arguments(tool_call)
        for key in ("path", "file_path", "target_path", "cwd", "url", "query", "pattern"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return f"{key}: {truncate_text_fn(value.strip(), 80)}"
        if "command" in arguments:
            return "command available during review"
        if "prompt" in arguments:
            return "prompt provided"
        if arguments:
            keys = ", ".join(sorted(str(key) for key in arguments.keys())[:4])
            return f"args: {truncate_text_fn(keys, 80)}"
        if group == "safe_resume":
            return "read-only candidate ready to resume"
        return f"tool: {name}"

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False
        if session.metadata.get(self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY) is True:
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []
        existing_tool_result_ids = {
            tool_id for message in session.messages if (tool_id := self._tool_result_id(message))
        }

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        restored_tool_result_ids: set[str] = set()
        reused_tool_call_ids: list[str] = []
        skipped_duplicate_tool_call_ids: list[str] = []
        for message in completed_tool_results:
            if isinstance(message, dict):
                tool_id = self._tool_result_id(message)
                if tool_id in restored_tool_result_ids:
                    skipped_duplicate_tool_call_ids.append(tool_id)
                    continue
                if tool_id:
                    reused_tool_call_ids.append(tool_id)
                    restored_tool_result_ids.add(tool_id)
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        planned_compensation_tool_call_ids: list[str] = []
        retryable_tool_call_ids: list[str] = []
        requires_user_tool_call_ids: list[str] = []
        resumable_tool_call_ids: list[str] = []
        seen_pending_tool_call_ids: set[str] = set()
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = self._checkpoint_tool_call_id(tool_call)
            if not tool_id:
                continue
            if (
                tool_id in seen_pending_tool_call_ids
                or tool_id in restored_tool_result_ids
                or tool_id in existing_tool_result_ids
            ):
                skipped_duplicate_tool_call_ids.append(tool_id)
                continue
            seen_pending_tool_call_ids.add(tool_id)
            name = self._checkpoint_tool_name(tool_call) or "tool"
            recovery_strategy = self._checkpoint_tool_recovery_strategy(tool_call)
            resumable = self._checkpoint_tool_is_resumable(tool_call, recovery_strategy)
            if recovery_strategy == "retryable":
                retryable_tool_call_ids.append(tool_id)
            elif recovery_strategy == "requires_user":
                requires_user_tool_call_ids.append(tool_id)
            if resumable:
                resumable_tool_call_ids.append(tool_id)
            planned_compensation_tool_call_ids.append(tool_id)
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": self._checkpoint_interrupted_tool_content(
                        recovery_strategy,
                        resumable=resumable,
                    ),
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        existing_or_appended_tool_result_ids = set(existing_tool_result_ids)
        messages_to_append: list[dict[str, Any]] = []
        compensation_tool_call_ids: list[str] = []
        for message in restored_messages[overlap:]:
            tool_id = self._tool_result_id(message)
            if tool_id:
                if tool_id in existing_or_appended_tool_result_ids:
                    skipped_duplicate_tool_call_ids.append(tool_id)
                    continue
                existing_or_appended_tool_result_ids.add(tool_id)
                if tool_id in planned_compensation_tool_call_ids:
                    compensation_tool_call_ids.append(tool_id)
            messages_to_append.append(message)
        session.messages.extend(messages_to_append)

        restored_checkpoint = {
            **checkpoint,
            "reused_tool_call_ids": reused_tool_call_ids,
            "reused_tool_count": len(reused_tool_call_ids),
            "compensation_tool_call_ids": compensation_tool_call_ids,
            "compensation_tool_count": len(compensation_tool_call_ids),
            "retryable_tool_call_ids": [
                tool_id for tool_id in retryable_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ],
            "retryable_tool_count": len([
                tool_id for tool_id in retryable_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ]),
            "requires_user_tool_call_ids": [
                tool_id for tool_id in requires_user_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ],
            "requires_user_tool_count": len([
                tool_id for tool_id in requires_user_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ]),
            "resumable_tool_call_ids": [
                tool_id for tool_id in resumable_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ],
            "resumable_tool_count": len([
                tool_id for tool_id in resumable_tool_call_ids
                if tool_id in compensation_tool_call_ids
            ]),
            "skipped_duplicate_tool_call_ids": skipped_duplicate_tool_call_ids,
            "skipped_duplicate_tool_count": len(skipped_duplicate_tool_call_ids),
            **self._checkpoint_recovery_review(
                pending_tool_calls,
                include_ids=set(compensation_tool_call_ids),
            ),
        }
        self._remember_restored_runtime_checkpoint(session, restored_checkpoint)
        self._clear_pending_user_turn(session)
        session.metadata[self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY] = True
        return True

    async def _resume_safe_runtime_checkpoint(self, session: Session) -> dict[str, Any] | None:
        """Execute confirmed safe pending tools from a preserved runtime checkpoint."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return None
        if session.metadata.get(self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY) is not True:
            if not self._restore_runtime_checkpoint(session):
                return None
            checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
            if not isinstance(checkpoint, dict):
                return None

        completed_tool_results = [
            dict(message)
            for message in checkpoint.get("completed_tool_results") or []
            if isinstance(message, dict)
        ]
        pending_tool_calls = [
            dict(tool_call)
            for tool_call in checkpoint.get("pending_tool_calls") or []
            if isinstance(tool_call, dict)
        ]
        completed_tool_call_ids: list[str] = []
        existing_completed_tool_ids: set[str] = set()
        for message in completed_tool_results:
            tool_id = self._tool_result_id(message)
            if tool_id and tool_id not in existing_completed_tool_ids:
                completed_tool_call_ids.append(tool_id)
                existing_completed_tool_ids.add(tool_id)

        resumed_tool_call_ids: list[str] = []
        skipped_tool_call_ids: list[str] = []
        requires_user_tool_call_ids: list[str] = []
        remaining_pending_tool_calls: list[dict[str, Any]] = []
        remaining_retryable_tool_call_ids: list[str] = []
        remaining_requires_user_tool_call_ids: list[str] = []
        remaining_resumable_tool_call_ids: list[str] = []
        remaining_pending_tool_call_ids: list[str] = []
        recovered_completed_tool_results = list(completed_tool_results)

        for tool_call in pending_tool_calls:
            tool_id = self._checkpoint_tool_call_id(tool_call)
            if not tool_id:
                continue
            recovery_strategy = self._checkpoint_tool_recovery_strategy(tool_call)
            resumable = self._checkpoint_tool_is_resumable(tool_call, recovery_strategy)
            if recovery_strategy == "requires_user":
                requires_user_tool_call_ids.append(tool_id)
                remaining_requires_user_tool_call_ids.append(tool_id)
                remaining_pending_tool_calls.append(tool_call)
                remaining_pending_tool_call_ids.append(tool_id)
                continue
            if not resumable:
                skipped_tool_call_ids.append(tool_id)
                if recovery_strategy == "retryable":
                    remaining_retryable_tool_call_ids.append(tool_id)
                remaining_pending_tool_calls.append(tool_call)
                remaining_pending_tool_call_ids.append(tool_id)
                continue

            name = self._checkpoint_tool_name(tool_call) or "tool"
            params = self._checkpoint_tool_arguments(tool_call)
            execute = getattr(self.tools, "execute", None)
            if not callable(execute):
                skipped_tool_call_ids.append(tool_id)
                remaining_pending_tool_calls.append(tool_call)
                remaining_pending_tool_call_ids.append(tool_id)
                remaining_resumable_tool_call_ids.append(tool_id)
                continue

            try:
                result = await execute(name, params)
            except Exception as exc:
                result = f"Error: {type(exc).__name__}: {exc}"

            content = self._checkpoint_tool_result_text(result)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": content,
                "timestamp": datetime.now().isoformat(),
            }
            for index in range(len(session.messages) - 1, -1, -1):
                message = session.messages[index]
                if message.get("role") == "tool" and self._tool_result_id(message) == tool_id:
                    session.messages[index] = tool_message
                    break
            else:
                session.messages.append(tool_message)
            if tool_id not in existing_completed_tool_ids:
                completed_tool_call_ids.append(tool_id)
                recovered_completed_tool_results.append(tool_message)
                existing_completed_tool_ids.add(tool_id)
            resumed_tool_call_ids.append(tool_id)

        resumed_checkpoint = {
            **checkpoint,
            "completed_tool_results": recovered_completed_tool_results,
            "completed_tool_call_ids": completed_tool_call_ids,
            "completed_tool_count": len(recovered_completed_tool_results),
            "executed_tool_call_ids": completed_tool_call_ids,
            "executed_tool_count": len(completed_tool_call_ids),
            "pending_tool_calls": remaining_pending_tool_calls,
            "pending_tool_call_ids": remaining_pending_tool_call_ids,
            "pending_tool_count": len(remaining_pending_tool_calls),
            "compensation_tool_call_ids": remaining_pending_tool_call_ids,
            "compensation_tool_count": len(remaining_pending_tool_call_ids),
            "retryable_tool_call_ids": remaining_retryable_tool_call_ids,
            "retryable_tool_count": len(remaining_retryable_tool_call_ids),
            "requires_user_tool_call_ids": remaining_requires_user_tool_call_ids,
            "requires_user_tool_count": len(remaining_requires_user_tool_call_ids),
            "resumable_tool_call_ids": remaining_resumable_tool_call_ids,
            "resumable_tool_count": len(remaining_resumable_tool_call_ids),
            "recovered_executed_tool_call_ids": resumed_tool_call_ids,
            "recovered_executed_tool_count": len(resumed_tool_call_ids),
            "recovered_skipped_tool_call_ids": skipped_tool_call_ids,
            "recovered_skipped_tool_count": len(skipped_tool_call_ids),
            "recovered_requires_user_tool_call_ids": requires_user_tool_call_ids,
            "recovered_requires_user_tool_count": len(requires_user_tool_call_ids),
            "recovered_pending_tool_count": len(remaining_pending_tool_calls),
            **self._checkpoint_recovery_review(
                remaining_pending_tool_calls,
                include_ids=set(remaining_pending_tool_call_ids),
            ),
        }
        self._remember_restored_runtime_checkpoint(session, resumed_checkpoint)
        self._clear_runtime_checkpoint(session)
        sessions = getattr(self, "sessions", None)
        if sessions is not None:
            sessions.save(session)
        return resumed_checkpoint

    async def _apply_recovery_review_action(
        self,
        session: Session,
        *,
        tool_call_id: str,
        action: str,
        user_input: str | None = None,
    ) -> dict[str, Any] | None:
        """Apply a UI recovery-review action to the latest preserved checkpoint."""
        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return None
        if session.metadata.get(self._RUNTIME_CHECKPOINT_MATERIALIZED_KEY) is not True:
            if not self._restore_runtime_checkpoint(session):
                return None
            checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
            if not isinstance(checkpoint, dict):
                return None

        pending_tool_calls = [
            dict(tool_call)
            for tool_call in checkpoint.get("pending_tool_calls") or []
            if isinstance(tool_call, dict)
        ]
        if not pending_tool_calls:
            return None

        target_index: int | None = None
        target_tool_call: dict[str, Any] | None = None
        for index, tool_call in enumerate(pending_tool_calls):
            if self._checkpoint_tool_call_id(tool_call) == tool_call_id:
                target_index = index
                target_tool_call = tool_call
                break
        if target_index is None or target_tool_call is None:
            return None

        target_name = self._checkpoint_tool_name(target_tool_call) or "tool"
        target_metadata = self._checkpoint_tool_metadata(target_name)
        group = self._checkpoint_recovery_review_group(
            target_name,
            target_tool_call,
            self._checkpoint_tool_recovery_strategy(target_tool_call),
            metadata=target_metadata,
            resumable=self._checkpoint_tool_is_resumable(
                target_tool_call,
                self._checkpoint_tool_recovery_strategy(target_tool_call),
            ),
        )
        normalized_action = action.strip().lower()
        if group == "review_required" and normalized_action != "confirm_retry":
            return None
        if group == "needs_input" and normalized_action != "provide_input":
            return None
        if group not in {"review_required", "needs_input"}:
            return None

        updated_tool_call = dict(target_tool_call)
        review_state = "confirmed" if group == "review_required" else "input_provided"
        updated_tool_call["review_state"] = review_state
        updated_tool_call["review_confirmed"] = True
        updated_tool_call["review_updated_at"] = time.time()
        if group == "review_required":
            updated_tool_call["retryable"] = True
            updated_tool_call["recovery_action"] = "retry"
            updated_tool_call["needs_user_input"] = False
            updated_tool_call["last_review_action"] = "confirm_retry"
        else:
            cleaned_input = (user_input or "").strip()
            if not cleaned_input:
                return None
            updated_tool_call["provided_user_input"] = cleaned_input
            updated_tool_call["needs_user_input"] = False
            updated_tool_call["recovery_action"] = "retry"
            updated_tool_call["retryable"] = True
            updated_tool_call["last_review_action"] = "provide_input"

        pending_tool_calls[target_index] = updated_tool_call
        updated_checkpoint = {
            **checkpoint,
            "pending_tool_calls": pending_tool_calls,
            "pending_tool_count": len(pending_tool_calls),
        }
        updated_checkpoint.update(
            self._checkpoint_recovery_review(
                pending_tool_calls,
                include_ids={
                    self._checkpoint_tool_call_id(tool_call)
                    for tool_call in pending_tool_calls
                    if self._checkpoint_tool_call_id(tool_call)
                },
            )
        )
        self._remember_restored_runtime_checkpoint(session, updated_checkpoint)
        self.sessions.save(session)
        return updated_checkpoint

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )

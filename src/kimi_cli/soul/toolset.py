from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from kosong.tooling import (
    CallableTool,
    CallableTool2,
    HandleResult,
    Tool,
    ToolError,
    ToolOk,
    Toolset,
)
from kosong.tooling.error import (
    ToolNotFoundError,
    ToolParseError,
    ToolRuntimeError,
)
from kosong.utils.typing import JsonType

from kimi_cli import logger
from kimi_cli.exception import InvalidToolError
from kimi_cli.hooks.engine import HookEngine
from kimi_cli.tools import SkipThisTool
from kimi_cli.wire.types import (
    ContentPart,
    TextPart,
    ToolCall,
    ToolCallRequest,
    ToolResult,
    ToolReturnValue,
)

if TYPE_CHECKING:
     from kimi_cli.soul.agent import Runtime

current_tool_call = ContextVar[ToolCall | None]("current_tool_call", default=None)

_current_session_id: ContextVar[str] = ContextVar("_current_session_id", default="")


def set_session_id(sid: str) -> None:
    _current_session_id.set(sid)


def get_session_id() -> str:
    return _current_session_id.get()


def _get_session_id() -> str:
    return _current_session_id.get()


def get_current_tool_call_or_none() -> ToolCall | None:
    """
    Get the current tool call or None.
    Expect to be not None when called from a `__call__` method of a tool.
    """
    return current_tool_call.get()


type ToolType = CallableTool | CallableTool2[Any]


if TYPE_CHECKING:

    def type_check(kimi_toolset: KimiToolset):
        _: Toolset = kimi_toolset


class KimiToolset:
    def __init__(self) -> None:
        self._tool_dict: dict[str, ToolType] = {}
        self._hidden_tools: set[str] = set()
        self._hook_engine: HookEngine = HookEngine()

    def set_hook_engine(self, engine: HookEngine) -> None:
        self._hook_engine = engine

    def add(self, tool: ToolType) -> None:
        self._tool_dict[tool.name] = tool

    def hide(self, tool_name: str) -> bool:
        """Hide a tool from the LLM tool list. Returns True if the tool exists."""
        if tool_name in self._tool_dict:
            self._hidden_tools.add(tool_name)
            return True
        return False

    def unhide(self, tool_name: str) -> None:
        """Restore a hidden tool to the LLM tool list."""
        self._hidden_tools.discard(tool_name)

    @overload
    def find(self, tool_name_or_type: str) -> ToolType | None: ...
    @overload
    def find[T: ToolType](self, tool_name_or_type: type[T]) -> T | None: ...
    def find(self, tool_name_or_type: str | type[ToolType]) -> ToolType | None:
        if isinstance(tool_name_or_type, str):
            return self._tool_dict.get(tool_name_or_type)
        else:
            for tool in self._tool_dict.values():
                if isinstance(tool, tool_name_or_type):
                    return tool
        return None

    @property
    def tools(self) -> list[Tool]:
        return [
            tool.base for tool in self._tool_dict.values() if tool.name not in self._hidden_tools
        ]

    def handle(self, tool_call: ToolCall) -> HandleResult:
        token = current_tool_call.set(tool_call)
        try:
            if tool_call.function.name not in self._tool_dict:
                return ToolResult(
                    tool_call_id=tool_call.id,
                    return_value=ToolNotFoundError(tool_call.function.name),
                )

            tool = self._tool_dict[tool_call.function.name]

            try:
                arguments: JsonType = json.loads(tool_call.function.arguments or "{}", strict=False)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Tool call JSON parse error: {tool_name} (call_id={call_id}): {error}",
                    tool_name=tool_call.function.name,
                    call_id=tool_call.id,
                    error=e,
                )
                return ToolResult(tool_call_id=tool_call.id, return_value=ToolParseError(str(e)))

            async def _call():
                tool_input_dict = arguments if isinstance(arguments, dict) else {}

                # --- PreToolUse ---
                from kimi_cli.hooks import events

                results = await self._hook_engine.trigger(
                    "PreToolUse",
                    matcher_value=tool_call.function.name,
                    input_data=events.pre_tool_use(
                        session_id=_get_session_id(),
                        cwd=str(Path.cwd()),
                        tool_name=tool_call.function.name,
                        tool_input=tool_input_dict,
                        tool_call_id=tool_call.id,
                    ),
                )
                for result in results:
                    if result.action == "block":
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            return_value=ToolError(
                                message=result.reason or "Blocked by PreToolUse hook",
                                brief="Hook blocked",
                            ),
                        )

                # --- Execute tool ---
                t0 = time.monotonic()
                try:
                    ret = await tool.call(arguments)
                except Exception as e:
                    tool_elapsed = time.monotonic() - t0
                    logger.exception(
                        "Tool execution failed: {tool_name} (call_id={call_id})",
                        tool_name=tool_call.function.name,
                        call_id=tool_call.id,
                    )
                    # --- PostToolUseFailure (fire-and-forget) ---
                    _hook_task = asyncio.create_task(
                        self._hook_engine.trigger(
                            "PostToolUseFailure",
                            matcher_value=tool_call.function.name,
                            input_data=events.post_tool_use_failure(
                                session_id=_get_session_id(),
                                cwd=str(Path.cwd()),
                                tool_name=tool_call.function.name,
                                tool_input=tool_input_dict,
                                error=str(e),
                                tool_call_id=tool_call.id,
                            ),
                        )
                    )
                    _hook_task.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() else None
                    )
                    from kimi_cli.telemetry import track

                    _error_type = type(e).__name__
                    track(
                        "tool_error",
                        tool_name=tool_call.function.name,
                        error_type=_error_type,
                    )
                    track(
                        "tool_call",
                        tool_name=tool_call.function.name,
                        success=False,
                        duration_ms=int(tool_elapsed * 1000),
                        error_type=_error_type,
                    )
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=ToolRuntimeError(str(e)),
                    )

                tool_elapsed = time.monotonic() - t0
                logger.info(
                    "Tool {tool_name} completed in {elapsed:.1f}s (call_id={call_id})",
                    tool_name=tool_call.function.name,
                    elapsed=tool_elapsed,
                    call_id=tool_call.id,
                )
                from kimi_cli.telemetry import track as _track_tool_call

                _track_tool_call(
                    "tool_call",
                    tool_name=tool_call.function.name,
                    success=not isinstance(ret, ToolError),
                    duration_ms=int(tool_elapsed * 1000),
                )

                # --- PostToolUse (fire-and-forget) ---
                _hook_task = asyncio.create_task(
                    self._hook_engine.trigger(
                        "PostToolUse",
                        matcher_value=tool_call.function.name,
                        input_data=events.post_tool_use(
                            session_id=_get_session_id(),
                            cwd=str(Path.cwd()),
                            tool_name=tool_call.function.name,
                            tool_input=tool_input_dict,
                            tool_output=str(ret)[:2000],
                            tool_call_id=tool_call.id,
                        ),
                    )
                )
                _hook_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

                return ToolResult(tool_call_id=tool_call.id, return_value=ret)

            return asyncio.create_task(_call())
        finally:
            current_tool_call.reset(token)

    def register_external_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> tuple[bool, str | None]:
        if name in self._tool_dict:
            existing = self._tool_dict[name]
            if not isinstance(existing, WireExternalTool):
                return False, "tool name conflicts with existing tool"
        try:
            tool = WireExternalTool(
                name=name,
                description=description,
                parameters=parameters,
            )
        except Exception as e:
            return False, str(e)
        self.add(tool)
        return True, None

    def load_tools(self, tool_paths: list[str], dependencies: dict[type[Any], Any]) -> None:
        """
        Load tools from paths like `kimi_cli.tools.shell:Shell`.

        Raises:
            InvalidToolError(KimiCLIException, ValueError): When any tool cannot be loaded.
        """

        good_tools: list[str] = []
        bad_tools: list[str] = []

        for tool_path in tool_paths:
            try:
                tool = self._load_tool(tool_path, dependencies)
            except SkipThisTool:
                logger.info("Skipping tool: {tool_path}", tool_path=tool_path)
                continue
            if tool:
                self.add(tool)
                good_tools.append(tool_path)
            else:
                bad_tools.append(tool_path)
        logger.info("Loaded tools: {good_tools}", good_tools=good_tools)
        if bad_tools:
            raise InvalidToolError(f"Invalid tools: {bad_tools}")

    @staticmethod
    def _load_tool(tool_path: str, dependencies: dict[type[Any], Any]) -> ToolType | None:
        logger.debug("Loading tool: {tool_path}", tool_path=tool_path)
        module_name, class_name = tool_path.rsplit(":", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            logger.warning(
                "Tool module import failed: {module_name}: {error}",
                module_name=module_name,
                error=e,
            )
            return None
        tool_cls = getattr(module, class_name, None)
        if tool_cls is None:
            logger.warning(
                "Tool class not found: {class_name} in {module_name}",
                class_name=class_name,
                module_name=module_name,
            )
            return None
        args: list[Any] = []
        if "__init__" in tool_cls.__dict__:
            # the tool class overrides the `__init__` of base class
            for param in inspect.signature(tool_cls).parameters.values():
                if param.kind == inspect.Parameter.KEYWORD_ONLY:
                    # once we encounter a keyword-only parameter, we stop injecting dependencies
                    break
                # all positional parameters should be dependencies to be injected
                if param.annotation not in dependencies:
                    raise ValueError(f"Tool dependency not found: {param.annotation}")
                args.append(dependencies[param.annotation])
        return tool_cls(*args)

    async def cleanup(self) -> None:
        """Cleanup any resources held by the toolset."""
        pass


class WireExternalTool(CallableTool):
    def __init__(self, *, name: str, description: str, parameters: dict[str, Any]) -> None:
        super().__init__(
            name=name,
            description=description or "No description provided.",
            parameters=parameters,
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        tool_call = get_current_tool_call_or_none()
        if tool_call is None:
            return ToolError(
                message="External tool calls must be invoked from a tool call context.",
                brief="Invalid tool call",
            )

        from kimi_cli.soul import get_wire_or_none

        wire = get_wire_or_none()
        if wire is None:
            logger.error(
                "Wire is not available for external tool call: {tool_name}", tool_name=self.name
            )
            return ToolError(
                message="Wire is not available for external tool calls.",
                brief="Wire unavailable",
            )

        external_tool_call = ToolCallRequest.from_tool_call(tool_call)
        wire.soul_side.send(external_tool_call)
        try:
            return await external_tool_call.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("External tool call failed: {tool_name}:", tool_name=self.name)
            return ToolError(
                message=f"External tool call failed: {e}",
                brief="External tool error",
            )

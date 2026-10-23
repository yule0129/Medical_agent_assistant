from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from .schemas import ToolContext, ToolResult


ToolHandler = Callable[[Session, ToolContext], ToolResult]


class ToolRegistry:
    """Allow-list for read-only clinical tools.

    A model receives names from this registry only; it cannot submit SQL,
    filesystem paths, or clinician write operations through this interface.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        if not name or name.startswith("_"):
            raise ValueError("invalid tool name")
        self._handlers[name] = handler

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def call(self, name: str, db: Session, context: ToolContext) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unregistered read tool: {name}")
        return handler(db, context)

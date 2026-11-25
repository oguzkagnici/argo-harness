"""Lightweight async event bus used by the agent loop, CLI, server, and recorder."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Listener = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, history: int = 2000):
        self._listeners: list[Listener] = []
        self.history: deque[Event] = deque(maxlen=history)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def emit(self, type: str, **data: Any) -> None:
        event = Event(type, data)
        self.history.append(event)
        for listener in list(self._listeners):
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                pass

    async def emit_async(self, type: str, **data: Any) -> None:
        event = Event(type, data)
        self.history.append(event)
        for listener in list(self._listeners):
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

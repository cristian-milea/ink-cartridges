"""Maps command names to handlers. Shared by the HTTP webhook and RFCOMM."""
from __future__ import annotations
from typing import Any, Callable, Dict

Handler = Callable[[dict], Any]


class CommandRegistry:
    def __init__(self) -> None:
        self._handlers: Dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"duplicate command: {name}")
        self._handlers[name] = handler

    def has(self, name: str) -> bool:
        return name in self._handlers

    def dispatch(self, name: str, args: dict) -> dict:
        """Run a command. Always returns an envelope dict, never raises."""
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown command: {name}"}
        try:
            data = handler(args or {})
            return {"ok": True, "data": data}
        except Exception as e:  # handlers must not crash the transport
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

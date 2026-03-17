"""WebSocket manager — bridges EventBus events to connected browser clients."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Set

from fastapi import WebSocket

from nexus.logger import get_logger

log = get_logger("websocket")


class WebSocketManager:
    """Manages WebSocket connections and broadcasts EventBus events."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        log.info("WebSocket client connected", clients=len(self._clients))

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        log.info("WebSocket client disconnected", clients=len(self._clients))

    async def broadcast(self, event_type: Any, data: Any = None) -> None:
        """Broadcast an event to all connected WebSocket clients."""
        if not self._clients:
            return

        message = json.dumps(
            {
                "event": event_type.name if hasattr(event_type, "name") else str(event_type),
                "data": _serialize(data),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


def _serialize(obj: Any) -> Any:
    """Convert dataclasses, datetimes, and other types to JSON-safe values."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    # Fallback to string
    return str(obj)

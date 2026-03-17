"""NEXUS Web UI — FastAPI server with WebSocket real-time updates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.logger import get_logger

if TYPE_CHECKING:
    from nexus.engine import NEXUSEngine

log = get_logger("web")


class WebServer:
    """Async web server that runs alongside the NEXUS engine.

    Follows the same lifecycle pattern as TelegramAlerter and NEXUSDashboard:
    start() is a long-running coroutine added to asyncio.gather(*tasks).
    """

    def __init__(self, engine: NEXUSEngine, host: str = "0.0.0.0", port: int = 8080) -> None:  # noqa: S104
        self._engine = engine
        self._host = host
        self._port = port
        self._server = None

    async def start(self) -> None:
        """Start uvicorn serving the FastAPI app in the existing event loop."""
        import uvicorn

        from nexus.web.api import create_app

        app = create_app(self._engine)
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            loop="none",
        )
        self._server = uvicorn.Server(config)
        log.info("Web dashboard starting", host=self._host, port=self._port)
        await self._server.serve()

    async def stop(self) -> None:
        """Signal uvicorn to exit gracefully."""
        if self._server:
            self._server.should_exit = True
            log.info("Web dashboard stopped")

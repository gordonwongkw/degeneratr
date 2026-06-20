"""FastAPI application: serves the JSON API and the static dashboard."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from .routes import router

_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    app = FastAPI(title="degeneratr", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    # When Telegram is enabled, run the market-hours watcher as a background task
    # so `serve` alone delivers alerts (no separate `watch` process needed). Inert
    # otherwise, and guarded so a missing token never blocks startup.
    if settings.telegram_enabled:
        import asyncio

        from ..notify import run_watch_loop

        @app.on_event("startup")
        async def _start_watcher() -> None:  # pragma: no cover - background task
            asyncio.create_task(run_watch_loop(settings))
            logging.getLogger("degeneratr.api").info("Telegram watcher task started")

    # When the data pipeline is enabled (the deployed instance), seed the store
    # from yfinance once, then accrue provider (Tiger) bars + persist the trade
    # log in-process. Guarded so storage/provider issues never block startup.
    if settings.data_pipeline_enabled:
        import asyncio

        from ..storage import run_data_pipeline

        @app.on_event("startup")
        async def _start_pipeline() -> None:  # pragma: no cover - background task
            asyncio.create_task(run_data_pipeline(settings))
            logging.getLogger("degeneratr.api").info("Data pipeline task started")

    # Serve the dashboard at the root. html=True makes "/" return index.html.
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
    return app


app = create_app()

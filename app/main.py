from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.admin import router as admin_router
from app.api.xtream import router as xtream_router
from app.core.config import ConfigManager
from app.core.db import Database
from app.core.events import broker
from app.crawler.worker import CrawlerWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = os.environ.get("PROXY_CONFIG", str(BASE_DIR.parent / "config.yaml"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_mgr = ConfigManager(CONFIG_PATH)
    cfg = config_mgr.get()
    db = Database(cfg["database"]["path"])
    crawler = CrawlerWorker(db, config_mgr)

    app.state.config_mgr = config_mgr
    app.state.db = db
    app.state.crawler = crawler

    # Let the (thread-safe) event broker reach this loop so the crawler
    # thread can push SSE updates to connected dashboards.
    loop = asyncio.get_running_loop()
    broker.bind_loop(loop)

    # End open SSE streams the moment a shutdown signal arrives, *before*
    # uvicorn starts waiting for connections to drain -- otherwise a
    # long-lived /api/events connection holds the graceful-shutdown window
    # open for its full duration. uvicorn installs its own handlers after
    # startup; we chain so its shutdown still runs.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev):
                broker.shutdown()
                if callable(_prev):
                    _prev(signum, frame)

            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not on the main thread / not supported -- fall back to the
            # lifespan-shutdown call below.
            pass

    crawler.start()
    db.log("info", "application started")
    yield
    broker.shutdown()  # also here, for the signal-less shutdown paths
    logging.getLogger("proxy.crawler").info("shutting down: waiting for in-flight probe to finish...")
    finished = crawler.stop(timeout=120)
    if not finished:
        logging.getLogger("proxy.crawler").warning(
            "crawler thread did not stop within timeout -- a probe may still be in flight"
        )


app = FastAPI(title="Xtream Filter Proxy", lifespan=lifespan)
app.include_router(xtream_router)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.admin import router as admin_router
from app.api.xtream import router as xtream_router
from app.core.config import ConfigManager
from app.core.db import Database
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

    crawler.start()
    db.log("info", "application started")
    yield
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

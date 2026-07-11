"""
backend/app/main.py — Contoso MSME RM Assist Tool API entry point.
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.store import DataStore
from app.routes import analysis, rag, briefing, voice, workspace, acs, call_records, rawdata

logging.basicConfig(level=get_settings().log_level,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("contoso.msme.toolapi")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting %s v%s", settings.service_name, settings.service_version)
    store = DataStore().load(settings.data_dir, settings.kb_dir)
    app.state.store = store
    logger.info("Loaded %d tables, %d transactions. Routes ready.",
                len(store.tables), len(store.all("transactions")))
    yield
    logger.info("Shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Contoso MSME RM Assist Tool API",
                  version=settings.service_version, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins_list(),
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.include_router(analysis.router)
    app.include_router(rag.router)
    app.include_router(briefing.router)
    app.include_router(workspace.router)
    app.include_router(voice.router)
    app.include_router(voice.ws_router)
    app.include_router(acs.router)
    app.include_router(acs.ws_router)
    app.include_router(call_records.router)
    app.include_router(rawdata.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"ok": True, "service": settings.service_name, "version": settings.service_version}

    return app


app = create_app()

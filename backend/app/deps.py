"""
backend/app/deps.py

Auth dependencies. Bearer token (injected as a Container App secret) gates the tool/analysis API.
Mirrors the loans POC pattern: a single shared bearer for the POC surface.
"""
from __future__ import annotations
from fastapi import Header, HTTPException, Request, Depends

from app.config import get_settings
from app.store import DataStore


def get_store(request: Request) -> DataStore:
    return request.app.state.store


def require_bearer(authorization: str = Header(default="")) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.toolapi_bearer_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")

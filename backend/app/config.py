"""
backend/app/config.py

Centralized configuration. Values come from environment variables at runtime
(Container App injects KV-referenced secrets as env vars), with dev-friendly
defaults. This module only reads env vars; it never calls the KV SDK directly.
"""
from __future__ import annotations
import os
from pathlib import Path
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Service identity ----
    service_name: str = "contoso-msme-toolapi"
    service_version: str = "1.13.0-explainable-evidence-ledger"
    log_level: str = "INFO"

    # ---- Data paths (baked into image) ----
    data_dir: Path = Field(default=Path("/app/data/csv"), alias="DATA_DIR")
    kb_dir: Path = Field(default=Path("/app/data/knowledge_base"), alias="KB_DIR")
    sop_dir: Path = Field(default=Path("/app/data/sop"), alias="SOP_DIR")

    # ---- Auth secrets (injected from KV at container startup) ----
    toolapi_bearer_token: str = Field(default="dev-bearer-change-me", alias="TOOLAPI_BEARER_TOKEN")
    crm_operator_username: str = Field(default="demo", alias="CRM_OPERATOR_USERNAME")
    crm_operator_password: str = Field(default="dev-password-change-me", alias="CRM_OPERATOR_PASSWORD")
    operator_session_secret: str = Field(default="dev-session-secret-change-me", alias="OPERATOR_SESSION_SECRET")
    operator_cookie_secure: bool = Field(default=True, alias="OPERATOR_COOKIE_SECURE")

    # ---- Azure resource endpoints (injected from KV) ----
    foundry_endpoint: str = Field(default="", alias="FOUNDRY_ENDPOINT")
    foundry_aoai_endpoint: str = Field(default="", alias="FOUNDRY_AOAI_ENDPOINT")
    foundry_chat_deployment: str = Field(default="gpt-4.1-mini", alias="FOUNDRY_CHAT_DEPLOYMENT")
    foundry_embed_deployment: str = Field(default="text-embedding-3-small", alias="FOUNDRY_EMBED_DEPLOYMENT")
    foundry_voicelive_model: str = Field(default="gpt-4.1", alias="FOUNDRY_VOICELIVE_MODEL")
    foundry_voicelive_ws_endpoint: str = Field(default="", alias="FOUNDRY_VOICELIVE_WS_ENDPOINT")
    search_endpoint: str = Field(default="", alias="SEARCH_ENDPOINT")
    search_index_name: str = Field(default="contoso-msme-policy-index", alias="SEARCH_INDEX_NAME")

    # ---- Azure Communication Services phone-call mode ----
    acs_endpoint: str = Field(default="", alias="ACS_ENDPOINT")
    acs_connection_string: str = Field(default="", alias="ACS_CONNECTION_STRING")
    acs_caller_number: str = Field(default="+18662327316", alias="ACS_CALLER_NUMBER")
    acs_default_rm_phone: str = Field(default="+917738506379", alias="ACS_DEFAULT_RM_PHONE")
    acs_default_customer_phone: str = Field(default="+917875316980", alias="ACS_DEFAULT_CUSTOMER_PHONE")
    acs_public_base_url: str = Field(default="", alias="ACS_PUBLIC_BASE_URL")
    acs_transcription_locale: str = Field(default="en-US", alias="ACS_TRANSCRIPTION_LOCALE")
    acs_enable_intermediate_transcripts: bool = Field(default=False, alias="ACS_ENABLE_INTERMEDIATE_TRANSCRIPTS")
    acs_cognitive_services_endpoint: str = Field(default="", alias="ACS_COGNITIVE_SERVICES_ENDPOINT")

    # ---- Behavior flags ----
    # POC: the assistant never auto-approves credit; this flag is a belt-and-braces
    # guard read by the memo/CRM services to refuse any "approved" status writes.
    allow_credit_decisions: bool = Field(default=False, alias="ALLOW_CREDIT_DECISIONS")

    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")

    def cors_origins_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

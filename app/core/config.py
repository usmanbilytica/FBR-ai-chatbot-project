from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)

WEAK_SECRETS = frozenset({"", "change-me", "changeme", "secret", "dev", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # HRMS
    hrms_db_server: str = Field(alias="HRMS_DB_SERVER")
    hrms_db_name: str = Field(alias="HRMS_DB_NAME")
    hrms_db_user: str = Field(alias="HRMS_DB_USER")
    hrms_db_password: str = Field(alias="HRMS_DB_PASSWORD")

    # OCR / Personal Files
    ocr_db_server: str = Field(alias="OCR_DB_SERVER")
    ocr_db_name: str = Field(alias="OCR_DB_NAME")
    ocr_db_user: str = Field(alias="OCR_DB_USER")
    ocr_db_password: str = Field(alias="OCR_DB_PASSWORD")

    # LLM
    llm_api_key: str = Field(alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model_name: str = Field(default="deepseek-v4-pro", alias="LLM_MODEL_NAME")

    hrms_token_passphrase: str = Field(default="", alias="HRMS_TOKEN_PASSPHRASE")
    # Safer default: open Text-to-SQL off unless explicitly enabled (prod prefers tools).
    allow_open_sql: bool = Field(default=False, alias="ALLOW_OPEN_SQL")
    # When true, launch login requires successful token decrypt (fail-closed).
    require_token_decrypt: bool = Field(default=True, alias="REQUIRE_TOKEN_DECRYPT")
    # When true (default in production), refuse hash-embedding fallback.
    require_real_embeddings: bool = Field(default=True, alias="REQUIRE_REAL_EMBEDDINGS")
    session_secret: str = Field(default="change-me", alias="SESSION_SECRET")
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        alias="CORS_ORIGINS",
    )

    # Environment / production gates
    environment: Literal["development", "staging", "production"] = Field(
        default="production", alias="ENVIRONMENT"
    )
    allow_dev_open_access: bool = Field(default=False, alias="ALLOW_DEV_OPEN_ACCESS")
    rate_limit_per_minute: int = Field(default=60, alias="RATE_LIMIT_PER_MINUTE")
    index_pf_limit: int = Field(default=0, alias="INDEX_PF_LIMIT")  # 0 = full / uncapped batching
    index_policy_limit: int = Field(default=0, alias="INDEX_POLICY_LIMIT")  # 0 = all chunks
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    # Escape hatch for local HTTP UAT while ENVIRONMENT=production
    allow_insecure_cookies: bool = Field(default=False, alias="ALLOW_INSECURE_COOKIES")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")

    # Agentic explorer (replaces hard source routing)
    agent_max_steps: int = Field(default=10, alias="AGENT_MAX_STEPS")
    # Allow catalog-validated SELECT from agent tools even if ALLOW_OPEN_SQL=0
    agent_allow_sql: bool = Field(default=True, alias="AGENT_ALLOW_SQL")
    # Use agentic explorer (True) vs legacy route_intent pipeline (False)
    agentic_explorer: bool = Field(default=True, alias="AGENTIC_EXPLORER")

    # Paths
    data_dir: Path = ROOT / "data"
    catalog_dir: Path = ROOT / "data" / "catalog"
    qdrant_path: Path = ROOT / "data" / "qdrant"
    qdrant_pf_path: Path = ROOT / "data" / "qdrant_pf"
    qdrant_policy_path: Path = ROOT / "data" / "qdrant_policy"
    status_map_path: Path = ROOT / "data" / "status_map.json"
    policy_facts_db: Path = ROOT / "data" / "policy_facts.db"
    audit_db: Path = ROOT / "data" / "audit.db"
    jd_authority_path: Path = ROOT / "data" / "jd_authority.json"
    excadre_gazettes_path: Path = ROOT / "data" / "excadre_gazettes.json"
    seed_dir: Path = ROOT / "data" / "seed"
    rag_documents_dir: Path = ROOT / "RAG Documents"

    # Locked business defaults
    ops_min_bps: int = 17
    tps_max_bps: int = 16
    rotation_default_years: float = 3.0
    retirement_age_years: int = 60
    retire_soon_days: int = 365
    additional_charge_days: int = 90
    holding_status_ids: List[int] = Field(default_factory=lambda: [1, 22, 23])
    general_location_id: int = 1
    sql_row_cap: int = 500
    sql_max_repair: int = 3
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    policy_collection: str = "fbr_rules_policies"
    pf_collection: str = "fbr_personal_files"
    odbc_driver: str = "ODBC Driver 18 for SQL Server"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def open_access_allowed(self) -> bool:
        """Only development + explicit flag may skip auth (never in production)."""
        return (not self.is_production) and self.allow_dev_open_access

    @field_validator("session_secret")
    @classmethod
    def _secret_strength(cls, v: str) -> str:
        if v.strip().lower() in WEAK_SECRETS or len(v.strip()) < 24:
            raise ValueError(
                "SESSION_SECRET must be a strong secret (≥24 chars); refuse weak defaults"
            )
        return v

    @model_validator(mode="after")
    def _prod_guards(self) -> "Settings":
        if self.is_production and self.allow_dev_open_access:
            log.warning("ALLOW_DEV_OPEN_ACCESS ignored in production")
            object.__setattr__(self, "allow_dev_open_access", False)
        # Production cookies Secure by default; ALLOW_INSECURE_COOKIES=1 for local HTTP UAT only.
        if self.is_production and not self.allow_insecure_cookies:
            if not self.cookie_secure:
                log.warning("COOKIE_SECURE enabled for production (set ALLOW_INSECURE_COOKIES=1 for local HTTP)")
            object.__setattr__(self, "cookie_secure", True)
        if self.is_production and self.allow_open_sql:
            log.info(
                "ALLOW_OPEN_SQL=1 in production — Text-to-SQL restricted to AccessID 0 (full)"
            )
        return self

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.catalog_dir,
            self.qdrant_path,
            self.qdrant_pf_path,
            self.qdrant_policy_path,
            self.seed_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.ensure_dirs()
    return s

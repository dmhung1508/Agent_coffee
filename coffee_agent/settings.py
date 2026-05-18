from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- LLM / Provider --------------------------------------------------
    openai_api_key: str = Field(default="", description="OpenAI API key.")
    openai_model: str = "gpt-4o-mini"

    # --- Public menu API -------------------------------------------------
    coffee_api_base_url: str = "https://api-coffee.8am.vn"

    # --- Context / summary -----------------------------------------------
    coffee_agent_max_context_chars: int = Field(default=6000, ge=500)
    summary_keep_tail_turns: int = Field(default=4, ge=1)
    summary_threshold_chars: int = Field(default=6000, ge=500)

    # --- Menu cache (clause 2.10) ---------------------------------------
    menu_cache_ttl_seconds: int = Field(default=600, ge=1)
    menu_cache_max_size: int = Field(default=512, ge=8)

    # --- Browse enrichment (clause 2.11) --------------------------------
    browse_enrich_top_n: int = Field(default=3, ge=1, le=20)

    # --- Last catalog invalidation threshold (clause 2.16) --------------
    last_catalog_overlap_threshold: float = Field(default=0.3, ge=0.0, le=1.0)

    # --- Observability (clause 2.15) ------------------------------------
    langsmith_api_key: str | None = None
    langchain_tracing_v2: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # --- Fast-path (clause 2.12) ----------------------------------------
    fast_path_enabled: bool = True

    # --- Session store (design 7.D.2) -----------------------------------
    session_ttl_seconds: int = Field(default=3600, ge=60)
    session_max_count: int = Field(default=1000, ge=10)

    # --- Order log (clause 2.17) ----------------------------------------
    order_log_path: Path = Path("logs/orders.jsonl")

    # --- HTTP server (design 7.D.1) -------------------------------------
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8000, ge=1, le=65535)
    cors_allowed_origins: str = "*"   # comma-separated; parsed into list at consumer

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        if not self.cors_allowed_origins or self.cors_allowed_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

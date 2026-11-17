"""All settings come from environment variables (or a local .env file), validated at startup."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Slack (Socket Mode: bot token + app-level token)
    slack_bot_token: str = Field(..., description="xoxb-... bot token")
    slack_app_token: str = Field(..., description="xapp-... app-level token with connections:write")

    # LLM (Gemini). Model names are configurable so they can follow Google's current releases.
    gemini_api_key: str
    llm_model_strong: str = "gemini-3.8-flash"        # intake, writer, editor
    llm_model_fast: str = "gemini-3.1-flash-lite"     # router, metadata tagging
    # Tried in order when a model is overloaded (503) or too slow; keeps the bot answering during demand spikes
    llm_fallback_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    embed_model: str = "gemini-embedding-001"
    llm_timeout_s: int = 45

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    # Storage
    data_dir: Path = Path("data")
    proposals_dir: Path = Path("data/proposals")
    db_path: Path = Path("var/state.db")
    chroma_path: Path = Path("var/chroma")
    output_dir: Path = Path("var/output")

    # Retrieval
    retrieval_k: int = 6
    retrieval_min_score: float = 0.35          # below this, the filtered search falls back to unfiltered

    log_level: str = "INFO"

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.chroma_path, self.output_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s

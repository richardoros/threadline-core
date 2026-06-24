"""Application configuration.

Settings are read from environment variables with the ``THREADLINE_`` prefix
(e.g. ``THREADLINE_PORT=9000``); anything not set falls back to the defaults
below. Everything lives under one data directory (``~/.threadline`` by
default), and the database/event paths are derived from it so they always
move together.
"""
import functools
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for Threadline, in one object."""

    model_config = SettingsConfigDict(env_prefix="THREADLINE_", extra="ignore")

    data_dir: Path = Path.home() / ".threadline"
    api_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8400
    vault_dir: Path | None = None
    # Idle budget before a never-ended session (crash/kill/reboot left it
    # "active") is reaped. Default 24h — long enough never to reap a live
    # session, short enough that ghosts don't linger. See services/sessions.py.
    session_max_idle_seconds: int = 86400

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_path(self) -> Path:
        """The SQLite database file, always inside ``data_dir``."""
        return self.data_dir / "threadline.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def events_dir(self) -> Path:
        """Directory for the append-only JSONL event log, inside ``data_dir``."""
        return self.data_dir / "events"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton (built once, then cached)."""
    return Settings()

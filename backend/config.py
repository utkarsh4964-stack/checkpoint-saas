"""
Centralized runtime settings for Checkpoint v0.2.

Everything here is overridable via environment variables so the same
image can run locally (SQLite, relaxed limits) and on Render
(Postgres, production limits) without code changes.
"""
from __future__ import annotations

import os
import warnings


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    ENVIRONMENT: str = os.getenv("CHECKPOINT_ENV", "development").strip().lower()
    SOLARI_BASE_URL: str = os.getenv("SOLARI_BASE_URL", "https://api.getsolari.com").rstrip("/")
    SOLARI_API_KEY: str = os.getenv("SOLARI_API_KEY", "").strip()

    # --- Auth --------------------------------------------------------
    JWT_SECRET_KEY: str = os.getenv("CHECKPOINT_JWT_SECRET", "dev-insecure-secret-change-me")
    JWT_EXPIRY_HOURS: int = _int("CHECKPOINT_JWT_EXPIRY_HOURS", 168)  # 7 days
    ALLOW_LOCAL_FALLBACK: bool = os.getenv("CHECKPOINT_ALLOW_LOCAL_FALLBACK", "false" if ENVIRONMENT == "production" else "true").lower() in {"1", "true", "yes"}

    # --- Resource quotas (per the 5-10 user MVP plan) -----------------
    MAX_ACTIVE_SESSIONS_PER_USER: int = _int("CHECKPOINT_MAX_ACTIVE_SESSIONS", 3)
    MAX_ACTIONS_PER_SESSION: int = _int("CHECKPOINT_MAX_ACTIONS_PER_SESSION", 100)
    SESSION_IDLE_TIMEOUT_MINUTES: int = _int("CHECKPOINT_SESSION_IDLE_TIMEOUT_MIN", 30)
    SESSION_MAX_LIFETIME_MINUTES: int = _int("CHECKPOINT_SESSION_MAX_LIFETIME_MIN", 120)
    CLEANUP_INTERVAL_SECONDS: int = _int("CHECKPOINT_CLEANUP_INTERVAL_SEC", 60)
    SOLARI_IDLE_TIMEOUT_MINUTES: int = _int("CHECKPOINT_SOLARI_IDLE_TIMEOUT_MIN", 35)
    MAX_ACTION_INTENT_LENGTH: int = _int("CHECKPOINT_MAX_ACTION_INTENT_LENGTH", 4000)
    MAX_COMMAND_LENGTH: int = _int("CHECKPOINT_MAX_COMMAND_LENGTH", 12000)
    MAX_FILE_CONTENT_LENGTH: int = _int("CHECKPOINT_MAX_FILE_CONTENT_LENGTH", 2_000_000)
    MAX_TARGET_LENGTH: int = _int("CHECKPOINT_MAX_TARGET_LENGTH", 1000)

    # --- Rate limits (in-memory; fine for the single-worker MVP) ------
    LOGIN_RATE_LIMIT_MAX: int = _int("CHECKPOINT_LOGIN_RATE_MAX", 8)
    LOGIN_RATE_LIMIT_WINDOW_SEC: int = _int("CHECKPOINT_LOGIN_RATE_WINDOW_SEC", 300)
    REGISTER_RATE_LIMIT_MAX: int = _int("CHECKPOINT_REGISTER_RATE_MAX", 5)
    REGISTER_RATE_LIMIT_WINDOW_SEC: int = _int("CHECKPOINT_REGISTER_RATE_WINDOW_SEC", 3600)
    GLOBAL_RATE_LIMIT_MAX: int = _int("CHECKPOINT_GLOBAL_RATE_MAX", 120)
    GLOBAL_RATE_LIMIT_WINDOW_SEC: int = _int("CHECKPOINT_GLOBAL_RATE_WINDOW_SEC", 60)


settings = Settings()

if settings.ENVIRONMENT == "production" and settings.ALLOW_LOCAL_FALLBACK:
    warnings.warn(
        "CHECKPOINT_ALLOW_LOCAL_FALLBACK is enabled in production. Set it to false for a public deployment.",
        stacklevel=1,
    )

if settings.ENVIRONMENT == "production":
    if settings.JWT_SECRET_KEY == "dev-insecure-secret-change-me":
        raise RuntimeError("CHECKPOINT_JWT_SECRET must be set in production.")
    if len(settings.JWT_SECRET_KEY.encode("utf-8")) < 32:
        raise RuntimeError("CHECKPOINT_JWT_SECRET must be at least 32 bytes in production.")

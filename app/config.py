"""Loads settings from environment variables (and .env), once, at startup."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Real environment variables win over .env, so tests can override anything.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ConfigError(Exception):
    """Raised when a setting is present but unsafe or malformed."""


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    database_path: Path
    stripe_secret_key: str | None
    stripe_webhook_secret: str | None
    nta_api_key: str | None
    openrouter_api_key: str | None
    openrouter_model: str | None

    def integrations(self) -> dict[str, bool]:
        """Which integrations have their keys set. Booleans only: never expose values."""
        return {
            "stripe": bool(self.stripe_secret_key and self.stripe_webhook_secret),
            "nta": bool(self.nta_api_key),
            "openrouter": bool(self.openrouter_api_key and self.openrouter_model),
        }


def check_stripe_key_is_test(key: str | None) -> None:
    """Hard rule: sandbox only. Refuse to run with a live Stripe key."""
    if key and not key.startswith(("sk_test_", "rk_test_")):
        raise ConfigError(
            "STRIPE_SECRET_KEY must be a sandbox key starting with sk_test_ "
            "(or rk_test_). Live keys are refused."
        )


def load_settings() -> Settings:
    db_path = Path(_env("DATABASE_PATH") or "data/leapplus.db")
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    settings = Settings(
        app_base_url=(_env("APP_BASE_URL") or "http://localhost:8000").rstrip("/"),
        database_path=db_path,
        stripe_secret_key=_env("STRIPE_SECRET_KEY"),
        stripe_webhook_secret=_env("STRIPE_WEBHOOK_SECRET"),
        nta_api_key=_env("NTA_API_KEY"),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        openrouter_model=_env("OPENROUTER_MODEL"),
    )
    check_stripe_key_is_test(settings.stripe_secret_key)
    return settings


settings = load_settings()

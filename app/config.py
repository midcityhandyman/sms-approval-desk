"""Runtime settings, read from environment variables.

Nothing here needs a real secret to run. In the default "dev" environment the
app uses a fake bot token so the whole signature path still executes offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

# Obviously fake. Telegram never issued this token. It only exists so the
# HMAC code path runs end to end in local development, tests and the demo.
FAKE_BOT_TOKEN = "123456789:FAKE-TOKEN-FOR-LOCAL-DEV-ONLY"

VALID_ENVS = ("dev", "prod")


class ConfigError(ValueError):
    """Raised when the environment describes an unsafe or impossible setup."""


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_int(name: str, value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _as_id_set(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    ids = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"STAFF_TELEGRAM_IDS has a non numeric entry: {part!r}")
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/desk.sqlite3"
    app_env: str = "dev"
    bot_token: str = FAKE_BOT_TOKEN
    # Empty means "any Telegram user with a valid signature". Only allowed in dev.
    staff_ids: frozenset[int] = field(default_factory=frozenset)
    init_data_max_age_seconds: int = 3600
    require_second_approver: bool = False
    sms_provider: str = "mock"
    max_body_chars: int = 480

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    def validate(self) -> "Settings":
        if self.app_env not in VALID_ENVS:
            raise ConfigError(f"APP_ENV must be one of {VALID_ENVS}, got {self.app_env!r}")
        if not self.bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is empty")
        if self.sms_provider != "mock":
            raise ConfigError(
                "Only SMS_PROVIDER=mock is implemented. Add a real sender class before changing it."
            )
        if self.init_data_max_age_seconds <= 0:
            raise ConfigError("INIT_DATA_MAX_AGE_SECONDS must be positive")
        if self.max_body_chars <= 0:
            raise ConfigError("MAX_BODY_CHARS must be positive")
        if not self.is_dev:
            if self.bot_token == FAKE_BOT_TOKEN:
                raise ConfigError("APP_ENV=prod refuses to start with the fake bot token")
            if not self.staff_ids:
                raise ConfigError("APP_ENV=prod needs STAFF_TELEGRAM_IDS (an allowlist)")
        return self


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from a mapping (defaults to os.environ) and validate it."""
    env = os.environ if env is None else env
    return Settings(
        db_path=env.get("DESK_DB_PATH") or "data/desk.sqlite3",
        app_env=(env.get("APP_ENV") or "dev").strip().lower(),
        bot_token=(env.get("TELEGRAM_BOT_TOKEN") or FAKE_BOT_TOKEN).strip(),
        staff_ids=_as_id_set(env.get("STAFF_TELEGRAM_IDS")),
        init_data_max_age_seconds=_as_int(
            "INIT_DATA_MAX_AGE_SECONDS", env.get("INIT_DATA_MAX_AGE_SECONDS"), 3600
        ),
        require_second_approver=_as_bool(env.get("REQUIRE_SECOND_APPROVER"), False),
        sms_provider=(env.get("SMS_PROVIDER") or "mock").strip().lower(),
        max_body_chars=_as_int("MAX_BODY_CHARS", env.get("MAX_BODY_CHARS"), 480),
    ).validate()

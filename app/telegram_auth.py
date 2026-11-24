"""Verification of Telegram Mini App init data.

Telegram hands a Mini App a query string (``Telegram.WebApp.initData``) that
includes a ``hash`` field. The backend must recompute that hash before it
trusts anything in the string:

    secret_key        = HMAC_SHA256(key="WebAppData", msg=bot_token)
    data_check_string = "\\n".join(sorted("key=value" for every field but hash))
    expected_hash     = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))

This module is pure standard library and has no I/O, so it is easy to unit
test with a fake bot token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qsl, urlencode

# How far in the future auth_date may be before we call it bogus.
CLOCK_SKEW_SECONDS = 60
# Init data is small. Anything larger than this is not from Telegram.
MAX_INIT_DATA_LENGTH = 4096


class InitDataError(Exception):
    """Init data failed verification. ``code`` is stable and safe to return to clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username or f"user {self.id}"


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def build_data_check_string(fields: Mapping[str, str]) -> str:
    """Sorted ``key=value`` lines for every field except ``hash``."""
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")


def compute_hash(fields: Mapping[str, str], bot_token: str) -> str:
    message = build_data_check_string(fields).encode("utf-8")
    return hmac.new(_secret_key(bot_token), message, hashlib.sha256).hexdigest()


def sign_init_data(fields: Mapping[str, str], bot_token: str) -> str:
    """Produce a signed init data string.

    Telegram does this on its side. We only need it for tests, the offline demo
    and the dev login helper, always with a fake token.
    """
    clean = {key: value for key, value in fields.items() if key != "hash"}
    clean["hash"] = compute_hash(clean, bot_token)
    return urlencode(clean)


def make_init_data(
    bot_token: str,
    user_id: int,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    auth_date: int | None = None,
) -> str:
    """Convenience wrapper around :func:`sign_init_data` for a single user."""
    user = {"id": user_id, "first_name": first_name}
    if last_name:
        user["last_name"] = last_name
    if username:
        user["username"] = username
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": f"AAF{user_id:012d}",
        "user": json.dumps(user, separators=(",", ":")),
    }
    return sign_init_data(fields, bot_token)


def _parse(init_data: str) -> dict[str, str]:
    if not init_data:
        raise InitDataError("missing_init_data", "No init data was supplied")
    if len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InitDataError("malformed_init_data", "Init data is too long")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed_init_data", "Init data is not a query string") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            # A repeated key makes the check string ambiguous. Refuse it.
            raise InitDataError("malformed_init_data", f"Field {key!r} appears more than once")
        fields[key] = value
    return fields


def _parse_user(raw: str | None) -> TelegramUser:
    if not raw:
        raise InitDataError("missing_user", "Init data has no user field")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("malformed_user", "The user field is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InitDataError("malformed_user", "The user field is not an object")
    user_id = data.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise InitDataError("malformed_user", "The user id is missing or not an integer")
    return TelegramUser(
        id=user_id,
        first_name=str(data.get("first_name") or ""),
        last_name=str(data.get("last_name") or ""),
        username=str(data.get("username") or ""),
    )


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 3600,
    now: float | None = None,
) -> TelegramUser:
    """Return the signed-in user, or raise :class:`InitDataError`.

    Order matters: the signature is checked before any field is interpreted, so
    unsigned input never reaches the JSON parser or the freshness check.
    """
    if not bot_token:
        raise InitDataError("server_misconfigured", "No bot token is configured")
    fields = _parse(init_data)

    received = fields.get("hash")
    if not received:
        raise InitDataError("missing_hash", "Init data has no hash field")
    expected = compute_hash(fields, bot_token)
    if not hmac.compare_digest(expected, received.strip().lower()):
        raise InitDataError("bad_signature", "Init data signature does not match")

    raw_auth_date = fields.get("auth_date", "")
    if not raw_auth_date.isdigit():
        raise InitDataError("missing_auth_date", "Init data has no usable auth_date")
    auth_date = int(raw_auth_date)
    current = time.time() if now is None else now
    if auth_date - current > CLOCK_SKEW_SECONDS:
        raise InitDataError("auth_date_in_future", "Init data is dated in the future")
    if current - auth_date > max_age_seconds:
        raise InitDataError("expired", "Init data is too old. Reopen the app.")

    return _parse_user(fields.get("user"))

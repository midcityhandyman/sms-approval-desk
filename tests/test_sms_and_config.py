from __future__ import annotations

import pytest

from app.config import FAKE_BOT_TOKEN, ConfigError, load_settings
from app.sender import build_sender
from app.sms import is_valid_phone, segment_info


@pytest.mark.parametrize(
    "phone, ok",
    [("+15555550101", True), ("+445555550101", True), ("5555550101", False), ("+0555", False),
     ("+1 555 555 0101", False), ("", False), ("+1555555010155555", False)],
)
def test_phone_format(phone, ok):
    assert is_valid_phone(phone) is ok


@pytest.mark.parametrize(
    "body, encoding, units, segments",
    [
        ("", "GSM-7", 0, 0),
        ("a" * 160, "GSM-7", 160, 1),
        ("a" * 161, "GSM-7", 161, 2),
        ("a" * 306, "GSM-7", 306, 2),
        ("a" * 307, "GSM-7", 307, 3),
        ("[ok]", "GSM-7", 6, 1),
        ("ж" * 70, "UCS-2", 70, 1),
        ("ж" * 71, "UCS-2", 71, 2),
        ("\U0001F600", "UCS-2", 2, 1),
    ],
)
def test_segments(body, encoding, units, segments):
    assert segment_info(body) == {"encoding": encoding, "units": units, "segments": segments}


def test_defaults_run_offline_with_fake_token():
    settings = load_settings({})
    assert settings.is_dev and settings.bot_token == FAKE_BOT_TOKEN
    assert settings.sms_provider == "mock" and settings.staff_ids == frozenset()


def test_env_parsing():
    settings = load_settings(
        {
            "APP_ENV": "prod",
            "TELEGRAM_BOT_TOKEN": "222222222:another-fake-token",
            "STAFF_TELEGRAM_IDS": "2001, 2002,",
            "REQUIRE_SECOND_APPROVER": "true",
            "INIT_DATA_MAX_AGE_SECONDS": "600",
        }
    )
    assert settings.staff_ids == frozenset({2001, 2002})
    assert settings.require_second_approver is True
    assert settings.init_data_max_age_seconds == 600


@pytest.mark.parametrize(
    "env",
    [
        {"APP_ENV": "staging"},
        {"APP_ENV": "prod"},
        {"APP_ENV": "prod", "TELEGRAM_BOT_TOKEN": "222222222:another-fake-token"},
        {"APP_ENV": "prod", "STAFF_TELEGRAM_IDS": "2001"},
        {"SMS_PROVIDER": "carrier-x"},
        {"STAFF_TELEGRAM_IDS": "abc"},
        {"INIT_DATA_MAX_AGE_SECONDS": "soon"},
        {"INIT_DATA_MAX_AGE_SECONDS": "0"},
    ],
)
def test_unsafe_config_is_refused(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def test_only_mock_sender_exists():
    assert build_sender("mock").name == "mock"
    with pytest.raises(ValueError):
        build_sender("carrier-x")


@pytest.mark.parametrize("phone", ["+15555550101\n", "+1555555\u0660\u0661\u0660\u0661", "\uff0b15555550101"])
def test_phone_must_be_plain_ascii_digits_with_nothing_after(phone):
    assert is_valid_phone(phone) is False

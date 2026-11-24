"""Init data signature checks, all with a fake bot token."""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qsl, quote, urlencode

import pytest

from app.telegram_auth import (
    InitDataError,
    build_data_check_string,
    compute_hash,
    make_init_data,
    sign_init_data,
    verify_init_data,
)

from .conftest import FAKE_TOKEN

NOW = 1_700_000_100
USER_JSON = '{"id":4242,"first_name":"Alex","last_name":"Sample"}'
FIELDS = {"auth_date": "1700000000", "query_id": "AAFtest", "user": USER_JSON}
# Computed once with hashlib directly from the algorithm in Telegram's docs.
# Pinned so a refactor that changes the signature scheme fails loudly.
PINNED_HASH = "f6afd21e4c234434f6ec8ae9b3be473934a07543c20c2530cca661643e02330e"


def code_of(init_data: str, token: str = FAKE_TOKEN, **kwargs) -> str:
    with pytest.raises(InitDataError) as caught:
        verify_init_data(init_data, token, now=kwargs.pop("now", NOW), **kwargs)
    return caught.value.code


def test_data_check_string_is_sorted_and_skips_hash():
    shuffled = {"user": USER_JSON, "hash": "ignored", "query_id": "AAFtest", "auth_date": "1700000000"}
    assert build_data_check_string(shuffled) == (
        f"auth_date=1700000000\nquery_id=AAFtest\nuser={USER_JSON}"
    )


def test_hash_matches_pinned_vector():
    assert compute_hash(FIELDS, FAKE_TOKEN) == PINNED_HASH


def test_hash_matches_independent_computation():
    secret = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    message = f"auth_date=1700000000\nquery_id=AAFtest\nuser={USER_JSON}".encode()
    assert compute_hash(FIELDS, FAKE_TOKEN) == hmac.new(secret, message, hashlib.sha256).hexdigest()


def test_valid_init_data_returns_user():
    user = verify_init_data(sign_init_data(FIELDS, FAKE_TOKEN), FAKE_TOKEN, now=NOW)
    assert user.id == 4242
    assert user.display_name == "Alex Sample"


def test_hand_built_query_string_verifies():
    # Build the string the way a browser would, without our own signing helper.
    raw = f"user={quote(USER_JSON)}&query_id=AAFtest&auth_date=1700000000&hash={PINNED_HASH}"
    assert verify_init_data(raw, FAKE_TOKEN, now=NOW).id == 4242


def test_uppercase_hash_is_accepted():
    raw = urlencode({**FIELDS, "hash": PINNED_HASH.upper()})
    assert verify_init_data(raw, FAKE_TOKEN, now=NOW).id == 4242


def test_extra_signed_fields_are_part_of_the_signature():
    fields = {**FIELDS, "signature": "abc123", "chat_instance": "-99"}
    signed = sign_init_data(fields, FAKE_TOKEN)
    assert verify_init_data(signed, FAKE_TOKEN, now=NOW).id == 4242
    stripped = urlencode({k: v for k, v in parse_qsl(signed) if k != "chat_instance"})
    assert code_of(stripped) == "bad_signature"


def test_tampered_user_is_rejected():
    signed = dict(parse_qsl(sign_init_data(FIELDS, FAKE_TOKEN)))
    signed["user"] = USER_JSON.replace("4242", "1")
    assert code_of(urlencode(signed)) == "bad_signature"


def test_tampered_auth_date_is_rejected():
    signed = dict(parse_qsl(sign_init_data(FIELDS, FAKE_TOKEN)))
    signed["auth_date"] = str(NOW)
    assert code_of(urlencode(signed)) == "bad_signature"


def test_signature_from_another_bot_is_rejected():
    other = "111111111:ANOTHER-fake-token"
    assert code_of(sign_init_data(FIELDS, other)) == "bad_signature"


def test_missing_hash():
    assert code_of(urlencode(FIELDS)) == "missing_hash"


def test_empty_hash():
    assert code_of(urlencode({**FIELDS, "hash": ""})) == "missing_hash"


@pytest.mark.parametrize("raw", ["", None])
def test_missing_init_data(raw):
    assert code_of(raw) == "missing_init_data"


@pytest.mark.parametrize("raw", ["not a query string", "a=1&&b", "x" * 5000])
def test_malformed_init_data(raw):
    assert code_of(raw) == "malformed_init_data"


def test_duplicate_keys_are_rejected():
    signed = sign_init_data(FIELDS, FAKE_TOKEN)
    assert code_of(signed + "&user=" + quote('{"id":1}')) == "malformed_init_data"


def test_expired_init_data():
    signed = sign_init_data(FIELDS, FAKE_TOKEN)
    assert verify_init_data(signed, FAKE_TOKEN, max_age_seconds=100, now=NOW).id == 4242
    assert code_of(signed, max_age_seconds=99) == "expired"


def test_auth_date_in_future():
    signed = sign_init_data(FIELDS, FAKE_TOKEN)
    assert verify_init_data(signed, FAKE_TOKEN, now=1_700_000_000 - 60).id == 4242
    assert code_of(signed, now=1_700_000_000 - 61) == "auth_date_in_future"


def test_missing_auth_date_even_when_signed():
    signed = sign_init_data({"user": USER_JSON}, FAKE_TOKEN)
    assert code_of(signed) == "missing_auth_date"


def test_missing_user_even_when_signed():
    signed = sign_init_data({"auth_date": "1700000000"}, FAKE_TOKEN)
    assert code_of(signed) == "missing_user"


@pytest.mark.parametrize(
    "user",
    ["{not json", "[1,2]", '{"first_name":"Alex"}', '{"id":"4242"}', '{"id":true}', '{"id":4.5}'],
)
def test_malformed_user_even_when_signed(user):
    signed = sign_init_data({"auth_date": "1700000000", "user": user}, FAKE_TOKEN)
    assert code_of(signed) == "malformed_user"


def test_empty_bot_token_never_verifies():
    assert code_of(sign_init_data(FIELDS, ""), token="") == "server_misconfigured"


def test_signature_is_checked_before_freshness():
    # Old and tampered: the answer must be about the signature, not the age.
    signed = dict(parse_qsl(sign_init_data(FIELDS, FAKE_TOKEN)))
    signed["query_id"] = "changed"
    assert code_of(urlencode(signed), max_age_seconds=1) == "bad_signature"


def test_make_init_data_round_trip():
    raw = make_init_data(FAKE_TOKEN, 77, "Jordan", "Example", "jexample", auth_date=1_700_000_000)
    user = verify_init_data(raw, FAKE_TOKEN, now=NOW)
    assert (user.id, user.first_name, user.last_name, user.username) == (77, "Jordan", "Example", "jexample")
    assert json.loads(dict(parse_qsl(raw))["user"])["id"] == 77


def test_display_name_fallbacks():
    only_username = sign_init_data(
        {"auth_date": "1700000000", "user": '{"id":5,"username":"desk_user"}'}, FAKE_TOKEN
    )
    assert verify_init_data(only_username, FAKE_TOKEN, now=NOW).display_name == "desk_user"
    bare = sign_init_data({"auth_date": "1700000000", "user": '{"id":5}'}, FAKE_TOKEN)
    assert verify_init_data(bare, FAKE_TOKEN, now=NOW).display_name == "user 5"

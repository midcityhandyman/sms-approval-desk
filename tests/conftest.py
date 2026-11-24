"""Shared fixtures. Everything runs against a temp SQLite file and a fake bot token."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, init_db
from app.main import create_app
from app.sender import MockSender
from app.service import Actor
from app.telegram_auth import make_init_data

# Not a real token. Telegram never issued it.
FAKE_TOKEN = "987654321:TEST-fake-token-for-unit-tests"

REVIEWER = Actor(id=2001, name="Robin Reviewer")
SECOND = Actor(id=2002, name="Sky Checker")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "desk.sqlite3"), bot_token=FAKE_TOKEN).validate()


@pytest.fixture
def conn(settings):
    connection = connect(settings.db_path)
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def sender() -> MockSender:
    return MockSender()


@pytest.fixture
def client(settings, sender):
    with TestClient(create_app(settings, sender)) as test_client:
        yield test_client


def auth(user_id: int = 2001, first_name: str = "Robin", last_name: str = "Reviewer", **kwargs) -> dict:
    """Authorization header carrying freshly signed init data for a fake user."""
    init_data = make_init_data(FAKE_TOKEN, user_id, first_name, last_name, **kwargs)
    return {"Authorization": f"tma {init_data}"}


def new_draft(phone: str = "+15555550101", body: str = "Hi Alex, your visit is booked for Tuesday.") -> dict:
    return {
        "recipient_name": "Alex Sample",
        "recipient_phone": phone,
        "purpose": "Appointment reminder",
        "body": body,
    }

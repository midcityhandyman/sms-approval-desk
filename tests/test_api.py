"""HTTP layer: every call must carry verifiable Telegram init data."""

from __future__ import annotations

import dataclasses
import time
from urllib.parse import parse_qsl, urlencode

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sender import MockSender
from app.telegram_auth import make_init_data

from .conftest import FAKE_TOKEN, auth, new_draft


def test_health_is_open(client):
    assert client.get("/api/health").json() == {"ok": True, "env": "dev", "sender": "mock"}


@pytest.mark.parametrize(
    "method, path",
    [("get", "/api/me"), ("get", "/api/drafts"), ("get", "/api/drafts/1"), ("get", "/api/outbox"),
     ("post", "/api/drafts"), ("post", "/api/drafts/1/approve"), ("post", "/api/drafts/1/edit"),
     ("post", "/api/drafts/1/reject"), ("post", "/api/drafts/1/retry")],
)
def test_every_route_needs_init_data(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_init_data"


def test_wrong_scheme_is_refused(client):
    header = auth()["Authorization"].replace("tma ", "Bearer ")
    assert client.get("/api/me", headers={"Authorization": header}).status_code == 401


def test_tampered_init_data_is_refused(client):
    fields = dict(parse_qsl(auth()["Authorization"][4:]))
    fields["user"] = fields["user"].replace("2001", "1")
    response = client.get("/api/me", headers={"Authorization": "tma " + urlencode(fields)})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "bad_signature"


def test_init_data_signed_by_another_bot_is_refused(client):
    forged = make_init_data("111111111:ANOTHER-fake-token", 2001, "Robin")
    response = client.get("/api/me", headers={"Authorization": f"tma {forged}"})
    assert response.json()["error"]["code"] == "bad_signature"


def test_expired_init_data_is_refused(client):
    response = client.get("/api/me", headers=auth(auth_date=int(time.time()) - 7200))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "expired"


def test_me_reports_the_signed_user(client):
    body = client.get("/api/me", headers=auth()).json()
    assert (body["id"], body["name"]) == (2001, "Robin Reviewer")
    assert body["counts"]["pending"] == 0


def test_staff_allowlist(settings, sender):
    locked = dataclasses.replace(settings, staff_ids=frozenset({2001}))
    with TestClient(create_app(locked, sender)) as client:
        assert client.get("/api/me", headers=auth(2001)).status_code == 200
        response = client.get("/api/me", headers=auth(3003, "Sam", "Outsider"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "not_staff"


def test_full_flow_edit_then_approve(client, sender):
    created = client.post("/api/drafts", json=new_draft(), headers=auth())
    assert created.status_code == 201
    draft_id = created.json()["id"]

    listing = client.get("/api/drafts?status=pending", headers=auth()).json()
    assert [d["id"] for d in listing["drafts"]] == [draft_id]
    assert listing["counts"]["pending"] == 1

    edited = client.post(
        f"/api/drafts/{draft_id}/edit",
        json={"body": "Hi Alex, see you Tuesday at 9.", "base_version": 1},
        headers=auth(),
    ).json()
    assert (edited["status"], edited["current_version"]) == ("pending", 2)

    stale = client.post(f"/api/drafts/{draft_id}/approve", json={"version": 1}, headers=auth(2002, "Sky", "Checker"))
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_version"
    assert sender.sent == []

    approved = client.post(
        f"/api/drafts/{draft_id}/approve", json={"version": 2}, headers=auth(2002, "Sky", "Checker")
    ).json()
    assert approved["status"] == "sent"
    assert [e["actor_name"] for e in approved["events"]] == [
        "Robin Reviewer", "Robin Reviewer", "Sky Checker", "Sky Checker",
    ]
    assert sender.sent[0]["body"] == "Hi Alex, see you Tuesday at 9."

    outbox = client.get("/api/outbox", headers=auth()).json()["outbox"]
    assert len(outbox) == 1 and outbox[0]["provider"] == "mock"


def test_reject_flow(client, sender):
    draft_id = client.post("/api/drafts", json=new_draft(), headers=auth()).json()["id"]
    rejected = client.post(
        f"/api/drafts/{draft_id}/reject", json={"version": 1, "reason": "Customer cancelled"}, headers=auth()
    ).json()
    assert rejected["status"] == "rejected"
    assert sender.sent == []
    again = client.post(f"/api/drafts/{draft_id}/reject", json={"version": 1}, headers=auth())
    assert again.status_code == 409


def test_failed_send_and_retry(client, sender):
    draft_id = client.post("/api/drafts", json=new_draft(phone="+15555550199"), headers=auth()).json()["id"]
    failed = client.post(f"/api/drafts/{draft_id}/approve", json={"version": 1}, headers=auth()).json()
    assert failed["status"] == "failed"
    sender.clear_failures()
    retried = client.post(f"/api/drafts/{draft_id}/retry", headers=auth()).json()
    assert retried["status"] == "sent"


def test_validation_errors(client):
    assert client.post("/api/drafts", json=new_draft(phone="555-0101"), headers=auth()).status_code == 422
    assert client.post("/api/drafts/1/approve", json={"version": 0}, headers=auth()).status_code == 422
    assert client.get("/api/drafts/999", headers=auth()).status_code == 404
    assert client.get("/api/drafts?status=bogus", headers=auth()).status_code == 422


def test_dev_helper_signs_data_that_really_verifies(client):
    data = client.get("/api/dev/init-data?user=1002").json()
    me = client.get("/api/me", headers={"Authorization": "tma " + data["init_data"]}).json()
    assert (me["id"], me["name"]) == (1002, "Dev Reviewer Two")
    assert client.get("/api/dev/init-data?user=5").status_code == 404


def test_dev_helper_does_not_exist_in_prod(settings):
    prod = dataclasses.replace(settings, app_env="prod", staff_ids=frozenset({2001})).validate()
    with TestClient(create_app(prod, MockSender())) as client:
        assert client.get("/api/dev/init-data").status_code == 404
        assert client.get("/api/me", headers=auth(2001)).status_code == 200


def test_frontend_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Approval Desk" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_schema_errors_use_the_same_error_envelope(client):
    bad_body = client.post("/api/drafts/1/approve", json={"version": 0}, headers=auth())
    assert bad_body.status_code == 422
    assert bad_body.json()["error"]["code"] == "invalid_request"
    assert "version" in bad_body.json()["error"]["message"]
    bad_path = client.get("/api/drafts/abc", headers=auth())
    assert (bad_path.status_code, bad_path.json()["error"]["code"]) == (422, "invalid_request")


def test_junk_hash_gets_401_not_500(client):
    response = client.get("/api/me", headers={"Authorization": "tma auth_date=1&hash=%C3%A9"})
    assert (response.status_code, response.json()["error"]["code"]) == (401, "bad_signature")

"""Approval rules, tested without HTTP."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading

import pytest

from app import service
from app.db import connect, init_db
from app.sender import MockSender
from app.service import DeskError

from .conftest import REVIEWER, SECOND


def make(conn, settings, phone="+15555550101", body="Hi Alex, your visit is booked for Tuesday."):
    return service.create_draft(conn, settings, "Alex Sample", phone, body, "Reminder")


def error_code(call) -> tuple[int, str]:
    with pytest.raises(DeskError) as caught:
        call()
    return caught.value.status_code, caught.value.code


def test_new_draft_is_pending_at_version_one(conn, settings):
    draft = make(conn, settings)
    assert (draft["status"], draft["current_version"]) == ("pending", 1)
    assert [e["action"] for e in draft["events"]] == ["created"]
    assert draft["segments"]["segments"] == 1


@pytest.mark.parametrize(
    "name, phone, body, code",
    [
        ("", "+15555550101", "hello", "missing_recipient"),
        ("Alex Sample", "555-0101", "hello", "bad_phone"),
        ("Alex Sample", "+15555550101", "   ", "empty_body"),
        ("Alex Sample", "+15555550101", "x" * 481, "body_too_long"),
    ],
)
def test_create_validation(conn, settings, name, phone, body, code):
    assert error_code(lambda: service.create_draft(conn, settings, name, phone, body)) == (422, code)


def test_approve_sends_the_approved_version(conn, settings, sender):
    draft = make(conn, settings)
    result = service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    assert result["status"] == "sent"
    assert sender.sent == [{"to": "+15555550101", "body": draft["body"], "id": sender.sent[0]["id"]}]
    assert [e["action"] for e in result["events"]] == ["created", "approved", "sent"]
    outbox = service.list_outbox(conn)
    assert len(outbox) == 1 and outbox[0]["version_no"] == 1 and outbox[0]["status"] == "sent"


def test_edit_creates_new_version_that_needs_approval(conn, settings, sender):
    draft = make(conn, settings)
    edited = service.edit_draft(conn, settings, draft["id"], "Hi Alex, see you Tuesday at 9.", 1, REVIEWER)
    assert (edited["status"], edited["current_version"]) == ("pending", 2)
    assert edited["body"] == "Hi Alex, see you Tuesday at 9."
    assert [v["version_no"] for v in edited["versions"]] == [1, 2]
    assert edited["versions"][0]["body"] == draft["body"], "the old text is kept, not overwritten"
    assert sender.sent == [], "an edit never sends anything"


def test_approval_of_old_version_is_refused_after_edit(conn, settings, sender):
    draft = make(conn, settings)
    service.edit_draft(conn, settings, draft["id"], "A different text entirely.", 1, SECOND)
    assert error_code(
        lambda: service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    ) == (409, "stale_version")
    assert sender.sent == []
    sent = service.approve_draft(conn, settings, sender, draft["id"], 2, REVIEWER)
    assert sender.sent[0]["body"] == "A different text entirely."
    assert sent["status"] == "sent"


def test_edit_from_stale_version_is_refused(conn, settings):
    draft = make(conn, settings)
    service.edit_draft(conn, settings, draft["id"], "First edit wins.", 1, REVIEWER)
    assert error_code(
        lambda: service.edit_draft(conn, settings, draft["id"], "Second edit loses.", 1, SECOND)
    ) == (409, "stale_version")


def test_edit_with_same_text_is_refused(conn, settings):
    draft = make(conn, settings)
    assert error_code(
        lambda: service.edit_draft(conn, settings, draft["id"], f"  {draft['body']}  ", 1, REVIEWER)
    ) == (422, "no_change")


def test_reject_then_edit_reopens(conn, settings, sender):
    draft = make(conn, settings)
    rejected = service.reject_draft(conn, draft["id"], 1, "Wrong day", REVIEWER)
    assert rejected["status"] == "rejected"
    assert rejected["events"][-1]["note"] == "Wrong day"
    assert error_code(
        lambda: service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    ) == (409, "not_pending")
    reopened = service.edit_draft(conn, settings, draft["id"], "Hi Alex, see you Wednesday.", 1, REVIEWER)
    assert (reopened["status"], reopened["current_version"]) == ("pending", 2)


def test_reject_checks_version_and_state(conn, settings, sender):
    draft = make(conn, settings)
    assert error_code(lambda: service.reject_draft(conn, draft["id"], 7, "", REVIEWER)) == (
        409,
        "stale_version",
    )
    assert error_code(lambda: service.reject_draft(conn, draft["id"], 1, "x" * 201, REVIEWER)) == (
        422,
        "reason_too_long",
    )
    service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    assert error_code(lambda: service.reject_draft(conn, draft["id"], 1, "", REVIEWER)) == (
        409,
        "not_pending",
    )


def test_sent_draft_is_frozen(conn, settings, sender):
    draft = make(conn, settings)
    service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    assert error_code(
        lambda: service.edit_draft(conn, settings, draft["id"], "Too late.", 1, REVIEWER)
    ) == (409, "not_editable")
    assert error_code(
        lambda: service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    ) == (409, "not_pending")
    assert error_code(lambda: service.retry_send(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "not_retryable",
    )
    assert error_code(lambda: service.dispatch(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "already_sent",
    )
    assert len(sender.sent) == 1, "nothing above may cause a second delivery"


def test_failed_send_can_be_retried(conn, settings, sender):
    draft = make(conn, settings, phone="+15555550199")
    failed = service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    assert failed["status"] == "failed"
    assert failed["events"][-1]["action"] == "send_failed"
    sender.clear_failures()
    retried = service.retry_send(conn, sender, draft["id"], SECOND)
    assert retried["status"] == "sent"
    assert [row["status"] for row in service.list_outbox(conn)] == ["failed", "sent"]


def test_failed_send_can_be_edited_back_to_pending(conn, settings, sender):
    draft = make(conn, settings, phone="+15555550199")
    service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    edited = service.edit_draft(conn, settings, draft["id"], "Shorter text.", 1, REVIEWER)
    assert (edited["status"], edited["current_version"]) == ("pending", 2)
    assert error_code(lambda: service.retry_send(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "not_retryable",
    )


def test_sender_exception_marks_draft_failed(conn, settings):
    class BrokenSender:
        name = "broken"

        def send(self, to_phone, body):
            raise RuntimeError("socket closed")

    draft = make(conn, settings)
    result = service.approve_draft(conn, settings, BrokenSender(), draft["id"], 1, REVIEWER)
    assert result["status"] == "failed"
    assert result["events"][-1]["note"] == "socket closed"


def test_dispatch_refuses_a_second_delivery_even_if_status_drifts(conn, settings, sender):
    draft = make(conn, settings)
    service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    # Force the state back by hand, as a buggy code path might.
    conn.execute("UPDATE drafts SET status = 'approved' WHERE id = ?", (draft["id"],))
    assert error_code(lambda: service.dispatch(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "already_sent",
    )
    assert len(sender.sent) == 1, "the sender must not be called again"
    assert service.get_draft(conn, draft["id"])["status"] == "sent", "status is repaired"


def test_outbox_index_allows_one_delivery_row_per_draft(conn, settings, sender):
    draft = make(conn, settings)
    service.approve_draft(conn, settings, sender, draft["id"], 1, REVIEWER)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outbox (draft_id, version_no, to_phone, body, provider, status, created_at)"
            " VALUES (?, 1, '+15555550101', 'again', 'mock', 'sent', '2030-01-01T00:00:00Z')",
            (draft["id"],),
        )


def test_second_approver_rule(conn, settings, sender):
    strict = dataclasses.replace(settings, require_second_approver=True)
    draft = make(conn, strict)
    # Bot drafted text (no human author) can be approved by anyone on staff.
    service.edit_draft(conn, strict, draft["id"], "Edited by the reviewer.", 1, REVIEWER)
    assert error_code(
        lambda: service.approve_draft(conn, strict, sender, draft["id"], 2, REVIEWER)
    ) == (403, "second_approver_required")
    assert service.approve_draft(conn, strict, sender, draft["id"], 2, SECOND)["status"] == "sent"


def test_second_approver_rule_is_off_by_default(conn, settings, sender):
    draft = make(conn, settings)
    service.edit_draft(conn, settings, draft["id"], "Edited by the reviewer.", 1, REVIEWER)
    assert service.approve_draft(conn, settings, sender, draft["id"], 2, REVIEWER)["status"] == "sent"


def test_listing_and_counts(conn, settings, sender):
    first = make(conn, settings)
    make(conn, settings, phone="+15555550102")
    service.approve_draft(conn, settings, sender, first["id"], 1, REVIEWER)
    assert [d["id"] for d in service.list_drafts(conn, "pending")] == [2]
    assert [d["id"] for d in service.list_drafts(conn)] == [1, 2]
    counts = service.status_counts(conn)
    assert (counts["pending"], counts["sent"], counts["rejected"]) == (1, 1, 0)
    assert error_code(lambda: service.list_drafts(conn, "bogus")) == (422, "bad_status")


def test_dispatch_needs_an_approval(conn, settings, sender):
    draft = make(conn, settings)
    assert error_code(lambda: service.dispatch(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "not_sendable",
    )
    assert sender.sent == []


def test_unknown_draft(conn, settings, sender):
    assert error_code(lambda: service.get_draft(conn, 999)) == (404, "not_found")
    assert error_code(
        lambda: service.approve_draft(conn, settings, sender, 999, 1, REVIEWER)
    ) == (404, "not_found")


def test_mock_sender_ids_are_deterministic():
    one, two = MockSender(), MockSender()
    assert one.send("+15555550101", "hi") == two.send("+15555550101", "hi")
    assert one.send("+15555550199", "hi").ok is False


def test_draft_stranded_in_approved_can_be_sent_by_retry(conn, settings, sender):
    # What a stop between "approval saved" and "send claimed" leaves behind.
    draft = make(conn, settings)
    conn.execute("UPDATE drafts SET status = 'approved' WHERE id = ?", (draft["id"],))
    sent = service.retry_send(conn, sender, draft["id"], SECOND)
    assert sent["status"] == "sent"
    assert len(sender.sent) == 1


def test_retry_refuses_a_draft_that_is_mid_send(conn, settings, sender):
    draft = make(conn, settings)
    conn.execute("UPDATE drafts SET status = 'sending' WHERE id = ?", (draft["id"],))
    assert error_code(lambda: service.retry_send(conn, sender, draft["id"], REVIEWER)) == (
        409,
        "not_retryable",
    )
    assert sender.attempts == 0


def test_two_reviewers_approving_at_once_send_one_text(settings, sender):
    """Real concurrency: separate connections on separate threads, same draft."""
    setup = connect(settings.db_path)
    init_db(setup)
    draft = make(setup, settings)
    setup.close()
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def tap(actor):
        own = connect(settings.db_path)
        try:
            barrier.wait(timeout=10)
            service.approve_draft(own, settings, sender, draft["id"], 1, actor)
            outcomes.append("sent")
        except DeskError as exc:
            outcomes.append(exc.code)
        finally:
            own.close()

    threads = [
        threading.Thread(target=tap, args=(REVIEWER if n % 2 else SECOND,)) for n in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert outcomes.count("sent") == 1, outcomes
    assert len(outcomes) == 8 and set(outcomes) <= {"sent", "not_pending"}, outcomes
    assert sender.attempts == 1

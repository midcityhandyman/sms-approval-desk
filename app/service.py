"""The approval rules. No HTTP in here, so the rules are testable on their own.

State machine for a draft:

    pending --approve--> approved --dispatch--> sending --> sent
       |  ^                                        |
       |  +------------- edit (new version) -------+--> failed --retry--> sending
       +--reject--> rejected --edit (new version)--> pending

The two rules that matter:

1. A decision names the version it was made on. If the draft has moved on
   since the reviewer loaded it, the decision is refused (409) instead of
   being applied to text the reviewer never saw.
2. An edit never changes text in place. It appends a new version and puts the
   draft back to pending, so edited text always needs a fresh approval.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .db import transaction
from .sender import Sender, SendResult
from .sms import is_valid_phone, segment_info

STATUSES = ("pending", "approved", "sending", "sent", "failed", "rejected")
EDITABLE = ("pending", "rejected", "failed")
MAX_NOTE_CHARS = 200


class DeskError(Exception):
    """A rule was broken. Carries the HTTP status the API should answer with."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Actor:
    id: int | None
    name: str


SYSTEM = Actor(id=None, name="system")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_body(body: str, settings: Settings) -> str:
    body = (body or "").strip()
    if not body:
        raise DeskError(422, "empty_body", "The text cannot be empty")
    if len(body) > settings.max_body_chars:
        raise DeskError(
            422, "body_too_long", f"The text is over the {settings.max_body_chars} character limit"
        )
    return body


def _event(
    conn: sqlite3.Connection, draft_id: int, version: int, action: str, actor: Actor, note: str = ""
) -> None:
    conn.execute(
        "INSERT INTO events (draft_id, version_no, action, actor_id, actor_name, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (draft_id, version, action, actor.id, actor.name, note, _now()),
    )


def _load(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise DeskError(404, "not_found", f"Draft {draft_id} does not exist")
    return row


def _current_version(conn: sqlite3.Connection, draft: sqlite3.Row) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM draft_versions WHERE draft_id = ? AND version_no = ?",
        (draft["id"], draft["current_version"]),
    ).fetchone()


def _check_version(draft: sqlite3.Row, version: int) -> None:
    if version != draft["current_version"]:
        raise DeskError(
            409,
            "stale_version",
            f"You were looking at version {version} but the draft is now at version"
            f" {draft['current_version']}. Reload and review the latest text.",
        )


def _summary(conn: sqlite3.Connection, draft: sqlite3.Row) -> dict:
    version = _current_version(conn, draft)
    return {
        "id": draft["id"],
        "recipient_name": draft["recipient_name"],
        "recipient_phone": draft["recipient_phone"],
        "purpose": draft["purpose"],
        "source": draft["source"],
        "status": draft["status"],
        "current_version": draft["current_version"],
        "body": version["body"],
        "body_author": version["author_name"],
        "segments": segment_info(version["body"]),
        "created_at": draft["created_at"],
        "updated_at": draft["updated_at"],
    }


# ---------------------------------------------------------------- reads


def list_drafts(conn: sqlite3.Connection, status: str | None = None, limit: int = 100) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise DeskError(422, "bad_status", f"Unknown status {status!r}")
    limit = max(1, min(int(limit), 500))
    if status:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM drafts ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [_summary(conn, row) for row in rows]


def get_draft(conn: sqlite3.Connection, draft_id: int) -> dict:
    draft = _load(conn, draft_id)
    result = _summary(conn, draft)
    result["versions"] = [
        dict(row)
        for row in conn.execute(
            "SELECT version_no, body, author_id, author_name, created_at FROM draft_versions"
            " WHERE draft_id = ? ORDER BY version_no",
            (draft_id,),
        )
    ]
    result["events"] = [
        dict(row)
        for row in conn.execute(
            "SELECT version_no, action, actor_id, actor_name, note, created_at FROM events"
            " WHERE draft_id = ? ORDER BY id",
            (draft_id,),
        )
    ]
    return result


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def list_outbox(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(
        "SELECT id, draft_id, version_no, to_phone, body, provider, provider_message_id,"
        " status, error, created_at FROM outbox ORDER BY id LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------- writes


def create_draft(
    conn: sqlite3.Connection,
    settings: Settings,
    recipient_name: str,
    recipient_phone: str,
    body: str,
    purpose: str = "",
    source: str = "manual",
    actor: Actor = SYSTEM,
) -> dict:
    recipient_name = (recipient_name or "").strip()
    recipient_phone = (recipient_phone or "").strip()
    if not recipient_name:
        raise DeskError(422, "missing_recipient", "A recipient name is required")
    if not is_valid_phone(recipient_phone):
        raise DeskError(422, "bad_phone", "The phone number must be in +15555550100 form")
    body = _clean_body(body, settings)
    now = _now()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO drafts (recipient_name, recipient_phone, purpose, source, status,"
            " current_version, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)",
            (recipient_name, recipient_phone, (purpose or "").strip(), source, now, now),
        )
        draft_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO draft_versions (draft_id, version_no, body, author_id, author_name,"
            " created_at) VALUES (?, 1, ?, ?, ?, ?)",
            (draft_id, body, actor.id, actor.name, now),
        )
        _event(conn, draft_id, 1, "created", actor)
    return get_draft(conn, draft_id)


def edit_draft(
    conn: sqlite3.Connection,
    settings: Settings,
    draft_id: int,
    body: str,
    base_version: int,
    actor: Actor,
) -> dict:
    """Append a new version and send the draft back to pending."""
    body = _clean_body(body, settings)
    with transaction(conn):
        draft = _load(conn, draft_id)
        if draft["status"] not in EDITABLE:
            raise DeskError(
                409, "not_editable", f"A draft that is {draft['status']} cannot be edited"
            )
        _check_version(draft, base_version)
        if body == _current_version(conn, draft)["body"]:
            raise DeskError(422, "no_change", "The text is the same as the current version")
        new_version = draft["current_version"] + 1
        now = _now()
        conn.execute(
            "INSERT INTO draft_versions (draft_id, version_no, body, author_id, author_name,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, new_version, body, actor.id, actor.name, now),
        )
        conn.execute(
            "UPDATE drafts SET status = 'pending', current_version = ?, updated_at = ? WHERE id = ?",
            (new_version, now, draft_id),
        )
        _event(conn, draft_id, new_version, "edited", actor, f"replaces v{base_version}")
    return get_draft(conn, draft_id)


def reject_draft(
    conn: sqlite3.Connection, draft_id: int, version: int, reason: str, actor: Actor
) -> dict:
    reason = (reason or "").strip()
    if len(reason) > MAX_NOTE_CHARS:
        raise DeskError(422, "reason_too_long", f"Keep the reason under {MAX_NOTE_CHARS} characters")
    with transaction(conn):
        draft = _load(conn, draft_id)
        if draft["status"] != "pending":
            raise DeskError(
                409, "not_pending", f"Only pending drafts can be rejected. This one is {draft['status']}"
            )
        _check_version(draft, version)
        conn.execute(
            "UPDATE drafts SET status = 'rejected', updated_at = ? WHERE id = ?", (_now(), draft_id)
        )
        _event(conn, draft_id, version, "rejected", actor, reason)
    return get_draft(conn, draft_id)


def approve_draft(
    conn: sqlite3.Connection,
    settings: Settings,
    sender: Sender,
    draft_id: int,
    version: int,
    actor: Actor,
) -> dict:
    """Approve one exact version, then hand it to the sender."""
    with transaction(conn):
        draft = _load(conn, draft_id)
        if draft["status"] != "pending":
            raise DeskError(
                409, "not_pending", f"Only pending drafts can be approved. This one is {draft['status']}"
            )
        _check_version(draft, version)
        author_id = _current_version(conn, draft)["author_id"]
        if settings.require_second_approver and author_id is not None and author_id == actor.id:
            raise DeskError(
                403,
                "second_approver_required",
                "You wrote this version, so someone else has to approve it",
            )
        conn.execute(
            "UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ?", (_now(), draft_id)
        )
        _event(conn, draft_id, version, "approved", actor)
    return dispatch(conn, sender, draft_id, actor)


def retry_send(conn: sqlite3.Connection, sender: Sender, draft_id: int, actor: Actor) -> dict:
    """Try a failed delivery again. The approval on the current version still stands."""
    draft = _load(conn, draft_id)
    if draft["status"] != "failed":
        raise DeskError(
            409, "not_failed", f"Only failed drafts can be retried. This one is {draft['status']}"
        )
    return dispatch(conn, sender, draft_id, actor)


def dispatch(conn: sqlite3.Connection, sender: Sender, draft_id: int, actor: Actor) -> dict:
    """Send the approved version exactly once.

    The claim is a conditional UPDATE, so of two concurrent callers only one
    gets rowcount 1 and talks to the sender. Before claiming, the outbox is
    checked for an earlier delivery, and a partial unique index on the outbox
    backs that check up at the database level.
    """
    already_sent = False
    with transaction(conn):
        delivered = conn.execute(
            "SELECT 1 FROM outbox WHERE draft_id = ? AND status = 'sent'", (draft_id,)
        ).fetchone()
        if delivered is not None:
            # The status drifted from the outbox. Repair it and refuse to resend.
            _load(conn, draft_id)
            conn.execute(
                "UPDATE drafts SET status = 'sent', updated_at = ? WHERE id = ?", (_now(), draft_id)
            )
            already_sent = True
    if already_sent:
        raise DeskError(409, "already_sent", f"Draft {draft_id} was already delivered")

    with transaction(conn):
        claimed = conn.execute(
            "UPDATE drafts SET status = 'sending', updated_at = ?"
            " WHERE id = ? AND status IN ('approved', 'failed')",
            (_now(), draft_id),
        ).rowcount
        if claimed != 1:
            draft = _load(conn, draft_id)
            raise DeskError(
                409, "not_sendable", f"Draft {draft_id} is {draft['status']} and cannot be sent"
            )
        draft = _load(conn, draft_id)
        version = _current_version(conn, draft)

    # The network call (a mock here) happens outside any transaction.
    try:
        result = sender.send(draft["recipient_phone"], version["body"])
    except Exception as exc:  # a sender bug must not strand the draft in "sending"
        result = SendResult(ok=False, provider=getattr(sender, "name", "unknown"), error=str(exc))

    with transaction(conn):
        now = _now()
        outcome = "sent" if result.ok else "failed"
        conn.execute(
            "INSERT INTO outbox (draft_id, version_no, to_phone, body, provider,"
            " provider_message_id, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                version["version_no"],
                draft["recipient_phone"],
                version["body"],
                result.provider,
                result.provider_message_id,
                outcome,
                result.error,
                now,
            ),
        )
        conn.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?", (outcome, now, draft_id)
        )
        _event(
            conn,
            draft_id,
            version["version_no"],
            "sent" if result.ok else "send_failed",
            actor,
            result.provider_message_id if result.ok else result.error,
        )
    return get_draft(conn, draft_id)
